#!/usr/bin/env python3
"""End-to-end: `dyak process` runs for real, against fake HTTP services (#122).

Every other suite in this directory replaces functions *in process*: `runner.run`,
`urllib.request.urlopen`, `llm.chat`. That is the right shape for asserting a contract,
and it is exactly why the bugs this job exists for were invisible to them. `publish`
*moves* the file it publishes, so a stage that reads a work-dir path after publishing it
reads nothing -- and a stubbed pipeline never moves anything. #82, #89, #121 and the
frame-ordering bug in `pipeline._stages` were all found by a human doing a real run, and
all of them are invisible to a suite that never leaves one process.

So this one leaves it:

  * a fixture video is **generated** with ffmpeg (lavfi + a real h264 encode), never
    committed as a binary;
  * three `http.server` services stand in for whisper.cpp (`/health`, `/inference`), the
    vision model and the text model (`/models`, `/chat/completions`), each on its own
    ephemeral port so a request reaching the wrong endpoint is a failed check rather than
    a coincidence;
  * `dyak process` runs as a **subprocess** with `DYAK_*` scrubbed and only the three
    fake URLs set, so nothing in this file can stub anything the child does;
  * the assertions are about the exit code and the bundle on disk -- the same two things a
    human checks after a real run.

Hermetic by construction: the child's environment is built from scratch (no `DYAK_*`, no
inherited `HOME`), the vault export is off, and every endpoint is a loopback port this
process opened. Nothing here reaches the network or needs an API key.

`ffmpeg` is the one real dependency, and it is not installed for the fixture's sake --
the pipeline itself shells out to it. When it is absent the job skips cleanly instead of
failing: a missing tool on a developer's box is not a regression in `dyak`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else repr(detail)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + text if text else ''}")
    if not cond:
        failures.append(name)


def _checkout():
    """This checkout's helper module and package, imported after `sys.path` is extended.

    The imports live in a function because they have to run *after* the two path inserts,
    and a module-level import that follows a statement is E402. Every other suite in this
    directory silences that with a `# noqa: E402`; doing it this way keeps the file clean
    without a directive that a newer ruff reports as unused.
    """
    for path in (str(HERE), str(REPO / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import _env

    # Before the package is imported, so nothing it reads at import time sees this shell's
    # DYAK_* values. The child's environment is built from scratch regardless.
    saved = _env.isolate()
    from dyak import audit, caption, frames, quality, report, stages, synth

    return _env, saved, audit, caption, frames, quality, report, stages, synth


_env, SAVED_ENV, dyau, dyca, dyfr, dyqu, dyro, dysg, dysy = _checkout()


# --- the fake services -------------------------------------------------------
# One handler class, three roles. `log` is the shared call record the assertions read:
# a request that reaches the wrong endpoint is visible as a row in the wrong role, which
# is the failure mode a single shared fake could not show.
LOG: list[dict] = []

# The fixture is 8 s long with a hard cut every 2 s, so scene detection selects four
# frames: three sharp ones and one the gate rejects. The count is never pinned -- that is
# ffmpeg's answer, and pinning it would make an encoder change look like a pipeline
# regression -- but "at least one frame passed and at least one was gated out" is pinned,
# because those two halves are what the run has to prove.
FIXTURE_SECONDS = 8
CUT_PERIOD = 4.0
CUT_HALF = 2.0
#: `[DARK_FROM, DARK_TO)` is painted black, which is the frame stage 5 rejects. A dark
#: canvas is the one failure the gate treats as non-fatal (`FrameQuality.fatal`), so the
#: run still has to reach exit 0 -- and stage 6 still has to leave that frame out. A
#: fixture where every frame passes would exercise neither.
DARK_FROM = 4.0
DARK_TO = 6.0

# 1600 px wide so stage 4 does not rescale it, and a 25 px grid over `testsrc2` so the
# frames carry enough edge energy to clear stage 5's gate with room to spare. Measured
# through the real pipeline: MEGE ~1.7e5-2.2e5 against a threshold of ~7.5e4. A flat
# `testsrc2` still lands at ~2.4e4 against the 2e4 floor, which is a gate that would
# flake on the next encoder.
STILL_FILTER = "testsrc2=size=1600x900:rate=1,drawgrid=w=25:h=25:t=2:c=white@1.0"

# In order. libx264 is what a real lecture looks like; mpeg4 is built into every ffmpeg
# and is the fallback for a build without the external encoder.
VIDEO_ENCODERS: tuple[tuple[str, ...], ...] = (
    ("-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"),
    ("-c:v", "mpeg4", "-q:v", "3"),
)

STT_SEGMENTS = (
    "Первая часть лекции про оптимальное управление.",
    "Вторая часть про функцию Беллмана.",
    "Третья часть про уравнение Гамильтона.",
    "Четвёртая часть про численные методы.",
)

CAPTION_TEXT = "На слайде — сетка с подписями и координатной осью."

#: What `caption.align` marks a frame stage 5 rejected with. A literal in `caption.py`
#: rather than an exported constant, so it is repeated here rather than imported.
GATED_OUT = "GATED_OUT"


def _transcript_payload() -> dict:
    """A whisper.cpp `verbose_json` body whose timings line up with the fixture's cuts.

    Word timings are not decoration: stage 6 binds each frame to the words spoken while it
    was on screen, and a payload without them is rejected by stage 3 before it reaches the
    bundle.
    """
    segments = []
    for index, text in enumerate(STT_SEGMENTS):
        start = float(index * CUT_HALF)
        segments.append(
            {
                "id": index,
                "start": start,
                "end": start + CUT_HALF,
                "text": text,
                "words": [
                    {
                        "word": word,
                        "start": start + 0.1 + 0.1 * position,
                        "end": start + 0.2 + 0.1 * position,
                    }
                    for position, word in enumerate(text.split())
                ],
            }
        )
    return {
        "task": "transcribe",
        "language": "ru",
        "duration": float(FIXTURE_SECONDS),
        "text": " ".join(STT_SEGMENTS),
        "segments": segments,
    }


def _section_body(number: int) -> str:
    """What the text model writes for section `number` of eight.

    Plain prose with no `$`, no `[[`, no filler words and no `в лекции не затрагивается`
    sentinel: stage 8 lints every one of those, and the point of this job is the wiring,
    not a second test of the lint rules. `section-N` is the marker the assertions look for
    in the finished note, so a stage that drops or reorders a body is caught.
    """
    title = dysy.SECTIONS[number - 1][1]
    return (
        f"Раздел {number} — «{title}». Материал стенограммы изложен без пропусков, "
        f"определения приведены по тексту лекции. Маркер синтеза: section-{number}."
    )


def _completion(text: str, model: str) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 64, "completion_tokens": 16},
    }


class FakeService(BaseHTTPRequestHandler):
    """One role's HTTP surface. `role` is set by the subclass `_serve` builds."""

    role = "llm"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        """Silence the access log: the call record above is the evidence, not stderr."""

    def _reply(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        LOG.append({"role": self.role, "method": "GET", "path": self.path})
        if self.path == "/health":
            self._reply({"status": "ok"})
        elif self.path == "/models":
            self._reply({"object": "list", "data": [{"id": f"{self.role}-model"}]})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        entry: dict = {
            "role": self.role,
            "method": "POST",
            "path": self.path,
            "bytes": length,
            "image": False,
            "system": "",
            "riff": b"RIFF" in raw,
        }
        LOG.append(entry)

        if self.path == "/inference":
            self._reply(_transcript_payload())
            return
        if self.path != "/chat/completions":
            self._reply({"error": "not found"}, 404)
            return

        payload = json.loads(raw.decode("utf-8"))
        messages = payload.get("messages") or []
        system = messages[0].get("content") if messages else ""
        entry["system"] = system if isinstance(system, str) else ""
        entry["image"] = any(isinstance(m.get("content"), list) for m in messages)

        if entry["image"]:
            self._reply(_completion(CAPTION_TEXT, "fake-vlm"))
            return
        if system == dyau.CRITIC_SYSTEM:
            # A clean audit: stage 9 reads the literal `OK` as "nothing found", and a
            # finding would be an ERROR that takes the run off exit 0.
            self._reply(_completion("OK", "fake-llm"))
            return
        self._reply(_completion(_section_body(_section_number(messages)), "fake-llm"))


def _section_number(messages: list[dict]) -> int:
    """`Раздел N из 8` from the synthesis prompt, or 1 when it is not there."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            if line.startswith("Раздел "):
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    return int(parts[1])
    return 1


def _serve(role: str) -> ThreadingHTTPServer:
    """A server on an ephemeral port, already listening when this returns.

    No sleep and no readiness poll: `HTTPServer.__init__` binds and listens before the
    thread starts, so a connection cannot arrive before the socket accepts one.
    """
    handler = type(f"Fake_{role}", (FakeService,), {"role": role})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- the fixture -------------------------------------------------------------


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fixture(work: Path) -> Path | None:
    """Generate the input video, or None when no encoder on this ffmpeg can write one."""
    still = work / "still.png"
    made = _ffmpeg("-f", "lavfi", "-i", STILL_FILTER, "-frames:v", "1", str(still))
    if made.returncode != 0 or not still.is_file():
        return None

    fixture = work / "fixture.mp4"
    for encoder in VIDEO_ENCODERS:
        made = _ffmpeg(
            "-loop",
            "1",
            "-i",
            str(still),
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=16000:duration={FIXTURE_SECONDS}",
            # The two sources of scene changes. A hard cut between a frame and its own
            # negation scores far above `select`'s 0.3, and negation leaves every gradient
            # magnitude untouched, so the frames the gate sees are equally sharp. The black
            # window is the frame the gate rejects, and it cuts both ways: into it at
            # DARK_FROM and out of it at DARK_TO.
            "-vf",
            (
                f"negate=enable='lt(mod(t\\,{CUT_PERIOD:g})\\,{CUT_HALF:g})',"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=black@1.0:t=fill:"
                f"enable='between(t\\,{DARK_FROM:g}\\,{DARK_TO:g})'"
            ),
            "-t",
            str(FIXTURE_SECONDS),
            *encoder,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-shortest",
            str(fixture),
        )
        if made.returncode == 0 and fixture.is_file() and fixture.stat().st_size > 0:
            return fixture
    return None


# --- the run -----------------------------------------------------------------


def _child_env(ports: dict[str, int], home: Path) -> dict[str, str]:
    """The child's environment, built rather than inherited.

    `PATH` because the pipeline runs ffmpeg, `HOME` because the vault default resolves
    under it, and nothing else. In particular no `DYAK_*` from this shell: the three
    endpoints below are the whole configuration, and a leaked `DYAK_LLM_ENDPOINT` would
    send the run at a real gateway while the fakes sat idle.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DYAK_STT": "whispercpp",
        "DYAK_WHISPERCPP_URL": f"http://127.0.0.1:{ports['stt']}",
        "DYAK_VLM_ENDPOINT": f"http://127.0.0.1:{ports['vlm']}",
        "DYAK_VLM_MODEL": "fake-vlm",
        "DYAK_LLM_ENDPOINT": f"http://127.0.0.1:{ports['llm']}",
        "DYAK_LLM_MODEL": "fake-llm",
    }


def _process(
    env: dict[str, str], fixture: Path, bundle: Path, *, overwrite: bool
) -> subprocess.CompletedProcess:
    argv = [
        sys.executable,
        "-m",
        "dyak",
        "process",
        str(fixture),
        "--output-dir",
        str(bundle),
        # The vault is a user's directory and this job has no business writing into one.
        # `--no-vault-export` also keeps the run from writing a settings file under HOME.
        "--no-vault-export",
    ]
    if overwrite:
        argv.append("--overwrite")
    return subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _calls(role: str, *, method: str | None = None) -> list[dict]:
    return [
        entry
        for entry in LOG
        if entry["role"] == role and (method is None or entry["method"] == method)
    ]


def _manifest_rows(bundle: Path) -> list[str]:
    manifest = bundle / dyfr.MANIFEST
    if not manifest.is_file():
        return []
    lines = manifest.read_text(encoding="utf-8").splitlines()[1:]
    return [line for line in lines if line.strip()]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="dyak-e2e-"))
    home = work / "home"
    home.mkdir()
    bundle = work / "bundle"
    servers = {role: _serve(role) for role in ("stt", "vlm", "llm")}
    ports = {role: server.server_address[1] for role, server in servers.items()}
    env = _child_env(ports, home)

    try:
        fixture = _fixture(work)
        check("the fixture video is generated with ffmpeg", fixture is not None, str(fixture))
        if fixture is None:
            print(f"\nfixture directory kept for inspection: {work}")
            return 1

        cold = _process(env, fixture, bundle, overwrite=True)
        print(cold.stdout, end="")
        if cold.stderr:
            print(cold.stderr, end="", file=sys.stderr)

        check(
            "`dyak process` exits 0 on a complete run",
            cold.returncode == 0,
            f"exit {cold.returncode}",
        )
        if cold.returncode != 0:
            print(f"\nbundle kept for inspection: {bundle}")
            return 1

        # -- what the fakes were asked for ------------------------------------
        inference = _calls("stt", method="POST")
        check(
            "stage 3 posted exactly one transcription to the STT endpoint",
            len(inference) == 1 and inference[0]["path"] == "/inference",
            str([(e["path"], e["bytes"]) for e in inference]),
        )
        check(
            "the transcription request carried a real wav, not an empty body",
            bool(inference) and inference[0]["riff"] and inference[0]["bytes"] > 44,
            str(inference[0] if inference else None),
        )

        rows = _manifest_rows(bundle)
        check(
            "stage 4 detected at least two frames in the fixture",
            len(rows) >= 2,
            f"{len(rows)} rows",
        )

        # -- the gate, and what stage 6 does with its verdict -------------------
        # `quality.json` is the only record of the per-frame verdict, and stage 6 is handed
        # the *bundle's* copy. #82: it used to be handed the work-dir path, which
        # `Bundle.publish` had already moved; `align` treats an unreadable report as "no
        # gate information", so stage 5 became a no-op and the rejected frame was captioned
        # anyway. No in-process test can see that -- the fake never moves a file. The checks
        # below can, and the black frame in the fixture is what gives them something to see.
        quality_frames = json.loads((bundle / dyqu.REPORT_NAME).read_text(encoding="utf-8"))[
            "frames"
        ]
        passed = [f for f in quality_frames if f["quality_gate"] == "PASS"]
        gated = [f for f in quality_frames if f["quality_gate"] != "PASS"]
        check(
            "stage 5 measured exactly the frames stage 4 put in the manifest",
            [f["name"] for f in quality_frames] == [Path(row.split("\t")[-1]).name for row in rows],
            f"{len(quality_frames)} measured, {len(rows)} in the manifest",
        )
        check(
            "stage 5 rejected the black frame as a dark canvas, not as a fatal loss",
            len(gated) == 1 and gated[0]["content_type"] == dyqu.CONTENT_DARK,
            str([(f["name"], f["content_type"], f["reason"]) for f in gated]),
        )
        check(
            "a non-fatal rejection still leaves a run that can finish",
            len(passed) >= 1,
            f"{len(passed)} passed, {len(gated)} rejected",
        )

        vision = [e for e in _calls("vlm", method="POST") if e["image"]]
        check(
            "stage 6 sent one vision request per *passed* frame to the VLM endpoint",
            len(vision) == len(passed) and len(passed) > 0,
            f"{len(vision)} vision calls, {len(passed)} frames passed the gate",
        )
        check(
            "no image ever reached the text endpoint",
            not any(e["image"] for e in _calls("llm", method="POST")),
            str([e["path"] for e in _calls("llm", method="POST") if e["image"]]),
        )

        text_calls = _calls("llm", method="POST")
        synth_calls = [e for e in text_calls if e["system"] == dysy.SYSTEM]
        critic_calls = [e for e in text_calls if e["system"] == dyau.CRITIC_SYSTEM]
        check(
            "stage 7 made one request per section with its own system prompt",
            len(synth_calls) == len(dysy.SECTIONS),
            f"{len(synth_calls)} of {len(dysy.SECTIONS)}",
        )
        check(
            "stage 9 made one critic request per section with its own system prompt",
            len(critic_calls) == len(dysy.SECTIONS),
            f"{len(critic_calls)} of {len(dysy.SECTIONS)}",
        )

        # -- the bundle on disk ------------------------------------------------
        required = sorted(dysy.NOTE_NAME if key == "note" else key for key in dyro.REQUIRED)
        absent = [name for name in required if not (bundle / name).is_file()]
        empty = [
            name
            for name in required
            if (bundle / name).is_file() and not (bundle / name).stat().st_size
        ]
        check(
            "every REQUIRED artefact is on disk and non-empty",
            not absent and not empty,
            f"absent={absent} empty={empty}",
        )

        verdict = json.loads((bundle / dyro.REPORT_NAME).read_text(encoding="utf-8"))
        check(
            "stage 12's verdict agrees with the exit code: ok, and the frames on disk are the"
            " ones the gate passed",
            verdict["ok"] is True
            and verdict["frames_expected"] == verdict["frames_found"] == len(passed)
            and not verdict["missing"],
            f"ok={verdict['ok']} frames={verdict['frames_found']}/{verdict['frames_expected']}"
            f" missing={verdict['missing']}",
        )
        check(
            "stage 12 attributed the verification to this run",
            bool(verdict["run_id"]) and bool(verdict["started_at"]),
            f"run_id={verdict['run_id']!r}",
        )

        note = (bundle / dysy.NOTE_NAME).read_text(encoding="utf-8")
        headings = [line.strip() for line in note.splitlines() if line.startswith("## ")]
        expected_headings = [f"## {number}. {title}" for number, title, _ in dysy.SECTIONS]
        check(
            "the finished note carries the eight canonical section headings",
            headings == expected_headings,
            str(headings[:3]),
        )
        markers = [f"section-{number}" for number, _, _ in dysy.SECTIONS]
        check(
            "every section's synthesised body survived stages 8-11 into note.md",
            all(marker in note for marker in markers),
            str([m for m in markers if m not in note]),
        )

        transcript = (bundle / "transcript.txt").read_text(encoding="utf-8")
        check(
            "the STT payload reached the transcript artefact",
            "функцию Беллмана" in transcript,
            transcript.splitlines()[2] if len(transcript.splitlines()) > 2 else "",
        )
        transcript_json = json.loads((bundle / "transcript.json").read_text(encoding="utf-8"))
        check(
            "stage 3 kept every segment and its word timings",
            transcript_json["segment_count"] == len(STT_SEGMENTS)
            and transcript_json["timed_words"] > 0,
            f"segments={transcript_json['segment_count']} words={transcript_json['timed_words']}",
        )

        captions = json.loads((bundle / dyca.REPORT_NAME).read_text(encoding="utf-8"))
        described = [
            entry for entry in captions["captions"].values() if entry.get("text") == CAPTION_TEXT
        ]
        check(
            "the vision model's answer reached captions.json for every captioned frame",
            len(described) == len(passed) and len(passed) > 0,
            f"{len(described)} of {len(passed)}",
        )
        considered = [
            alignment
            for alignment in captions["alignments"]
            if alignment["caption_status"] != GATED_OUT
        ]
        check(
            "stage 6 aligned every frame but captioned only the ones the gate passed (#82)",
            len(captions["alignments"]) == len(quality_frames)
            and len(considered) == len(passed)
            and all(alignment["quality_gate"] == "PASS" for alignment in considered),
            f"{len(considered)} considered of {len(captions['alignments'])} aligned",
        )
        check(
            "stage 6 recorded each caption as a call, not as a silent omission",
            all(alignment["caption_trace"].get("source") == "called" for alignment in considered),
            str([a["caption_trace"].get("source") for a in considered]),
        )

        run_state = json.loads((bundle / "run-state.json").read_text(encoding="utf-8"))
        check(
            "run-state.json records a completed run, not a failed or interrupted one",
            run_state["outcome"] == "completed"
            and run_state["stages_completed"] == list(range(1, dysg.IMPLEMENTED + 1)),
            f"outcome={run_state['outcome']!r} stages={run_state['stages_completed']}",
        )

        stage_names = (
            "probe",
            "audio",
            "stt",
            "frames",
            "quality",
            "caption",
            "synth",
            "lint",
            "audit",
            "repair",
            "link",
            "report",
        )
        missing_lines = [
            f"[{index}/{dysg.IMPLEMENTED}] {name}"
            for index, name in enumerate(stage_names, start=dysg.FIRST_STAGE)
            if f"[{index}/{dysg.IMPLEMENTED}] {name}" not in cold.stdout
        ]
        check("every stage reported itself in the run log", not missing_lines, str(missing_lines))
        numbered = sorted(
            {
                int(found.group(1))
                for found in re.finditer(rf"\[(\d+)/{dysg.IMPLEMENTED}\]", cold.stdout)
            }
        )
        check(
            "the run log numbers stages 1..N and nothing else (#101)",
            numbered == list(range(dysg.FIRST_STAGE, dysg.IMPLEMENTED + 1)),
            str(numbered),
        )

        # -- the warm run: the cache has to be wired to the bundle, not the scratch dir
        # #121/#83: `caption.run` takes `cache_source` from the *bundle*, and the scratch
        # directory it writes into is empty on every run. A cache read from the scratch
        # dir re-calls every frame while reporting success -- invisible without a second
        # real run, which is what this is.
        before = len(_calls("vlm", method="POST"))
        warm = _process(env, fixture, bundle, overwrite=False)
        check(
            "a second run of the same source exits 0",
            warm.returncode == 0,
            f"exit {warm.returncode}",
        )
        replayed = len(_calls("vlm", method="POST")) - before
        check(
            "the second run reuses the bundle's caption cache instead of calling the VLM again",
            replayed == 0,
            f"{replayed} vision calls on the warm run",
        )
        warm_captions = json.loads((bundle / dyca.REPORT_NAME).read_text(encoding="utf-8"))
        warm_considered = [
            alignment
            for alignment in warm_captions["alignments"]
            if alignment["caption_status"] != GATED_OUT
        ]
        warm_gated = [
            alignment
            for alignment in warm_captions["alignments"]
            if alignment["caption_status"] == GATED_OUT
        ]
        check(
            "the warm run's alignments say `cached`, so the bundle shows why no call was made",
            len(warm_considered) == len(passed)
            and all(a["caption_trace"].get("source") == "cached" for a in warm_considered)
            and len(warm_gated) == len(gated),
            str([a["caption_trace"].get("source") for a in warm_considered]),
        )

        if failures:
            print(f"\nbundle kept for inspection: {bundle}")
        return 1 if failures else 0
    finally:
        for server in servers.values():
            server.shutdown()
        _env.restore(SAVED_ENV)


if __name__ == "__main__":
    # A developer without ffmpeg is not a regression, and the pipeline cannot run at all
    # without it -- the skip is the honest outcome, and it is loud about why.
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("SKIP  ffmpeg/ffprobe are not on PATH, so the fixture cannot be generated")
        print("all e2e checks skipped (ffmpeg unavailable)")
        raise SystemExit(0)
    code = main()
    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        raise SystemExit(1)
    print("all e2e checks passed")
    raise SystemExit(code)
