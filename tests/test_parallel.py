#!/usr/bin/env python3
"""Fan-out must not change what a stage produces, only how long it takes (#355, #356).

Four claims:

  * `parallel.map_ordered` returns results in input order and raises the failure the
    sequential loop would have raised, whatever order the units finish in.
  * `DYAK_FFMPEG_JOBS` / `DYAK_STT_CONCURRENCY` refuse a non-positive count instead of clamping.
  * stages 4 and 5 write the same manifest, frames and `quality.json` at 1 and at 4 jobs
    (real ffmpeg, a synthetic video).
  * the hosted STT route splices the same transcript when its chunks come back out of order,
    and its counter still ends at N/N.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()

from dyak import frames, parallel, quality, runner  # noqa: E402
from dyak import stt as dystt  # noqa: E402
import dyak.stt.openai_compat as dyso  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dyak-parallel-"))

# --- map_ordered -----------------------------------------------------------------------


def _slow_reverse(x: int) -> int:
    # The first unit finishes last: completion order is the reverse of input order.
    time.sleep(0.02 * (5 - x))
    return x * 10


_finished: list[int] = []
_lock = threading.Lock()


def _tracked(x: int) -> int:
    value = _slow_reverse(x)
    with _lock:
        _finished.append(x)
    return value


check(
    "map_ordered keeps input order when completion order is reversed",
    parallel.map_ordered(_tracked, range(5), 5) == [0, 10, 20, 30, 40]
    and _finished != sorted(_finished),
    f"finished in {_finished}",
)


def _fails_on(bad: set[int]):
    def fn(x: int) -> int:
        time.sleep(0.01 * (5 - x))
        if x in bad:
            raise ValueError(f"unit {x}")
        return x

    return fn


try:
    parallel.map_ordered(_fails_on({1, 3}), range(5), 4)
    raised = None
except ValueError as exc:
    raised = str(exc)
check(
    "the failure raised is the first in input order, as the sequential loop raises it",
    raised == "unit 1",
    f"raised {raised!r} (unit 3 finished first)",
)

_in_flight = 0
_peak = 0


def _counted(x: int) -> int:
    global _in_flight, _peak
    with _lock:
        _in_flight += 1
        _peak = max(_peak, _in_flight)
    time.sleep(0.02)
    with _lock:
        _in_flight -= 1
    return x


parallel.map_ordered(_counted, range(12), 3)
check("never more than `workers` units in flight", _peak == 3, f"peak {_peak}")

# --- the knobs -------------------------------------------------------------------------

os.environ.pop("DYAK_FFMPEG_JOBS", None)
check(
    "DYAK_FFMPEG_JOBS defaults to DEFAULT_FFMPEG_JOBS",
    parallel.ffmpeg_jobs() == parallel.DEFAULT_FFMPEG_JOBS,
    str(parallel.ffmpeg_jobs()),
)
for bad in ("0", "-2", "four"):
    os.environ["DYAK_FFMPEG_JOBS"] = bad
    try:
        parallel.ffmpeg_jobs()
        code = None
    except runner.DependencyError as exc:
        code = exc.code
    check(f"DYAK_FFMPEG_JOBS={bad!r} is exit 1, not a clamp", code == 1, f"code={code}")
os.environ.pop("DYAK_FFMPEG_JOBS", None)

os.environ["DYAK_STT_CONCURRENCY"] = "0"
try:
    dyso.OpenAICompat("http://127.0.0.1:9").concurrency()
    code = None
except runner.DependencyError as exc:
    code = exc.code
check("DYAK_STT_CONCURRENCY=0 is exit 1, not a clamp", code == 1, f"code={code}")
os.environ.pop("DYAK_STT_CONCURRENCY", None)

# --- stages 4 and 5 with real ffmpeg ---------------------------------------------------


def _digest_dir(path: Path, pattern: str) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.glob(pattern))}


if shutil.which("ffmpeg") is None:
    print("SKIP  stages 4/5 determinism: ffmpeg not on PATH")
else:
    video = tmp / "slides.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=5:duration=16",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
    )  # fmt: skip
    settings = frames.Settings(mode="interval", every=2.0, width=640)
    outcomes = {}
    for jobs in ("1", "4"):
        os.environ["DYAK_FFMPEG_JOBS"] = jobs
        out = tmp / f"frames-{jobs}"
        began = time.monotonic()
        manifest, produced = frames.run(video, out, settings=settings)
        qwork = tmp / f"quality-{jobs}"
        qreport = quality.run(produced, qwork, stream=open(os.devnull, "w"))
        quality.write(qreport, qwork / quality.REPORT_NAME, quality.Settings())
        verdict = json.loads((qwork / quality.REPORT_NAME).read_text(encoding="utf-8"))
        outcomes[jobs] = {
            "seconds": time.monotonic() - began,
            "manifest": manifest.read_text(encoding="utf-8"),
            "frames": _digest_dir(out, "f_*.png"),
            "enhanced": _digest_dir(qwork / "enhanced", "*"),
            # Paths differ between the two work dirs by construction; the verdicts must not.
            "verdict": json.dumps(verdict, sort_keys=True).replace(f"frames-{jobs}", "frames-N"),
        }
    os.environ.pop("DYAK_FFMPEG_JOBS", None)
    one, four = outcomes["1"], outcomes["4"]
    check(
        "stage 4 extracts more than one frame from the fixture",
        len(one["frames"]) >= 4,
        f"{len(one['frames'])} frames",
    )
    check("stage 4's manifest is identical at 1 and 4 jobs", one["manifest"] == four["manifest"])
    check("stage 4's frames are byte-identical at 1 and 4 jobs", one["frames"] == four["frames"])
    check(
        "stage 5's enhanced frames are byte-identical at 1 and 4 jobs",
        one["enhanced"] == four["enhanced"],
        f"{len(one['enhanced'])} enhanced",
    )
    check(
        "stage 5's quality.json verdicts agree at 1 and 4 jobs", one["verdict"] == four["verdict"]
    )
    print(f"      timing: 1 job {one['seconds']:.2f}s, 4 jobs {four['seconds']:.2f}s")

# --- hosted STT, chunks answered out of order ------------------------------------------

RATE = 16000
wav_path = tmp / "lecture.wav"
with wave.open(str(wav_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    # Every sample distinct-ish, so a chunk can be recognised by its own first bytes.
    w.writeframes(b"".join((i % 30000).to_bytes(2, "little") for i in range(RATE)))
pcm = wav_path.read_bytes()[44:]


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, n: int = -1) -> bytes:
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _ChunkEndpoint:
    """Answers each chunk with a segment naming its data offset; early chunks answer last."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def __call__(self, request, timeout=None):
        if request.data is None:
            return _Response(b'{"data": []}')
        body = request.data
        start = body.index(b"RIFF") + 44
        offset = pcm.index(body[start : start + 64])
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(max(0.0, 0.08 - offset / len(pcm) * 0.08))
        with self.lock:
            self.in_flight -= 1
        word = {"word": f" at{offset}", "start": 0.0, "end": 0.05, "probability": 0.9}
        segment = {"id": 0, "text": f"at{offset}", "start": 0.0, "end": 0.05, "words": [word]}
        return _Response(json.dumps({"segments": [segment]}).encode())


real_urlopen = dyso.urllib.request.urlopen
os.environ["DYAK_OPENAI_URL"] = "http://127.0.0.1:10302"
os.environ["DYAK_OPENAI_MAX_UPLOAD_BYTES"] = "9000"
texts = {}
progress = {}
for concurrency in ("1", "4"):
    os.environ["DYAK_STT_CONCURRENCY"] = concurrency
    endpoint = _ChunkEndpoint()
    dyso.urllib.request.urlopen = endpoint
    seen: list[tuple[int, int]] = []
    transcript = dystt.transcribe(
        wav_path,
        audio_duration=1.0,
        backend="openai",
        on_progress=lambda done, total, seen=seen: seen.append((done, total)),
    )
    texts[concurrency] = [s.text for s in transcript.segments]
    progress[concurrency] = (seen, endpoint.peak)
dyso.urllib.request.urlopen = real_urlopen
for name in ("DYAK_OPENAI_URL", "DYAK_OPENAI_MAX_UPLOAD_BYTES", "DYAK_STT_CONCURRENCY"):
    os.environ.pop(name, None)

seen1, peak1 = progress["1"]
seen4, peak4 = progress["4"]
check("the wav is sliced into several chunks", len(seen1) >= 4, f"{seen1}")
check("at concurrency 1 one chunk is in flight at a time", peak1 == 1, f"peak {peak1}")
check("at concurrency 4 chunks overlap", peak4 > 1, f"peak {peak4}")
check(
    "the spliced transcript is identical when chunks return out of order",
    texts["1"] == texts["4"] and len(texts["1"]) >= 2,
    f"{texts['4']}",
)
check(
    "the counter counts completed uploads 0..N, ending at N/N",
    [d for d, _ in seen4] == list(range(len(seen4))) and seen4[-1][0] == seen4[-1][1],
    f"{seen4}",
)

shutil.rmtree(tmp, ignore_errors=True)
_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAIL  {len(failures)} parallelism check(s) failed:")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("all parallelism checks passed")
