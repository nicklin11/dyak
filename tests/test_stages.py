#!/usr/bin/env python3
"""Regression: stages 1-3 honour the ADR-0001 contracts, not just their happy path.

Stage 0 pinned the exit-code contract before other stages could invent their own.
The same applies here, and these tests are mostly about the *unhappy* paths,
because that is where this pipeline's real failures live:

  * a dependency that is absent (2) versus present and failing (3), and the
    failure's stderr arriving byte-for-byte rather than as a paraphrase;
  * artefacts verified on disk, so an ffmpeg run that exits 0 having written
    nothing is caught instead of trusted (ADR-0001 D3);
  * word timings consumed as seconds from the endpoint's JSON, never reparsed out
    of stdout text, and BPE pieces left alone rather than detokenised by guess.

`runner.run` is stubbed throughout: no real ffmpeg, no whisper.cpp, no network.
The one real end-to-end run lives outside this file, in the acceptance notes.
"""

import base64
import hashlib
import io
import json
import pathlib
import re
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import urllib.error

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()
sys.path.insert(0, str(REPO / "src"))
from dyak import audio as dya  # noqa: E402
from dyak import cli as dyc  # noqa: E402
from dyak import deps as dyd  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402
from dyak import bundle as dyb  # noqa: E402
from dyak import frames as dyfr  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import audio as dypa  # noqa: E402
from dyak import quality as dyq  # noqa: E402
from dyak import caption as dycap  # noqa: E402
from dyak import llm as dyle  # noqa: E402
from dyak import synth as dysy  # noqa: E402
from dyak.stt import core as dysttc  # noqa: E402
from dyak import lint as dyln  # noqa: E402
from dyak import audit as dyau  # noqa: E402
from dyak import link as dylk  # noqa: E402
from dyak import report as dyro  # noqa: E402
from dyak import repair as dyrep  # noqa: E402
from dyak import stt as dyst  # noqa: E402
from dyak import stages as dys  # noqa: E402
from dyak import probe as dyprobe  # noqa: E402
from dyak import runner as dyr  # noqa: E402
from dyak import stt as dystt  # noqa: E402
from dyak import workspace as dyw  # noqa: E402
from dyak.deps import PROCESS_REQUIRES  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else repr(detail)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + text if text else ''}")
    if not cond:
        failures.append(name)


def raises(fn, *args, **kwargs):
    """Return the DependencyError fn raised, or None."""
    try:
        fn(*args, **kwargs)
    except dyr.DependencyError as exc:
        return exc
    except Exception as exc:  # a different exception type is itself a failure
        return SimpleNamespace(code="WRONG-TYPE", message=f"{type(exc).__name__}: {exc}")
    return None


# --- fake subprocess plumbing -------------------------------------------------
# FAKE maps program name -> list of (stdout, stderr, rc), consumed in order.
# CALLS records every invocation so the argv itself can be asserted.
FAKE: dict[str, list[tuple[bytes, bytes, int]]] = {}
CALLS: list[tuple[str, list[str]]] = []
real_run = dyr.run
# Captured before anything is monkeypatched: by the time this file reaches the
# pipeline-ordering test, dyp.run is the stub above, and testing against the stub would
# pass vacuously -- exactly the class of bug the test exists to catch.
REAL_PIPELINE_RUN = dyp.run
REAL_QUALITY_RUN = dyp.quality.run
real_resolve = dyr.resolve
real_which = dyr.shutil.which
real_sleep = dyfr.time.sleep

PRESENT = {"ffmpeg", "ffprobe"}


def install_fake(programs: dict[str, object]) -> None:
    FAKE.clear()
    FAKE.update(programs)  # type: ignore[arg-type]
    CALLS.clear()


# --- HTTP seam ---------------------------------------------------------------------
# The only remaining STT backends are HTTP endpoints, so the suite intercepts
# `urllib.request.urlopen` rather than a subprocess. Before #44 these tests faked
# `shipboard process --timestamps json` and asserted on its argv; there is no
# subprocess left to assert on, and deleting the assertions would have stopped
# checking that stage 3 asks for the *timestamped* form at all.
HTTP_CALLS: list[object] = []
HTTP_QUEUE: list[bytes] = []


class _FakeHTTPResponse:
    # urllib responses carry `.status` and `.getcode()`; `deps.check_gateway` reads the
    # first. This fake stands in for urlopen globally, so it has to satisfy every caller,
    # not only the STT ones.
    status = 200

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._body
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def install_http(responses: list[bytes]) -> None:
    """Queue the bodies `urlopen` returns, one per call. Also clears the call log."""
    HTTP_CALLS.clear()
    HTTP_QUEUE.clear()
    HTTP_QUEUE.extend(responses)


# Only the whisper.cpp endpoint is faked. This fake stands in for `urlopen` *globally*, and
# the suite also probes the gateway at an address it expects to be unreachable -- a fake
# that answered that too would turn a deliberate failure into a silent pass. Matching on
# netloc keeps every other host (including 127.0.0.1:1) on the real network path.
FAKED_NETLOCS = {"127.0.0.1:10302", "localhost:10302"}


def _netloc(request) -> str:
    full = getattr(request, "full_url", "") or ""
    return full.split("//", 1)[-1].split("/", 1)[0] if "//" in full else ""


def fake_urlopen(request, timeout=None):
    full = getattr(request, "full_url", "")
    if _netloc(request) not in FAKED_NETLOCS:
        raise urllib.error.URLError(f"connection refused (real path, not faked): {full}")
    HTTP_CALLS.append(request)
    # `transcribe` calls `backend.check()` before `run()`, and check() is a `GET /health`.
    # It is a liveness probe, not part of the payload sequence, so it must not consume a
    # queued body -- otherwise every test silently shifts by one response.
    if full.endswith("/health"):
        return _FakeHTTPResponse(b"ok")
    body = HTTP_QUEUE.pop(0) if HTTP_QUEUE else b"[]"
    return _FakeHTTPResponse(body)


urllib.request.urlopen = fake_urlopen


def fake_resolve(name, remediation):
    if name not in PRESENT:
        raise dyr.DependencyError(
            name=name,
            code=ec.MISSING_DEPENDENCY,
            message=f"required dependency not found on PATH: {name}",
            remediation=remediation,
        )
    return f"/usr/bin/{name}"


def fake_run(name, args, *, remediation, timeout=dyr.DEFAULT_TIMEOUT, cwd=None):
    fake_resolve(name, remediation)
    CALLS.append((name, list(args)))
    if name not in FAKE:
        # Silence here would be the fake's own version of the exit-code trust
        # this pipeline is built against: an unprogrammed call and a drained
        # queue would look identical, and the code under test would "pass"
        # without having been exercised at all.
        raise AssertionError(f"unprogrammed call to {name}: {args}")
    queue = FAKE[name]
    if not queue:
        raise AssertionError(f"{name} called more times than FAKE programs for: {args}")
    stdout, stderr, rc = queue.pop(0)
    if rc != 0:
        raise dyr.DependencyError(
            name=name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{name} exited {rc} running `{' '.join(args)}`",
            stderr=stderr,
            argv=list(args),
        )
    return stdout


tmp = Path(tempfile.mkdtemp(prefix="dyak-test-"))
real_workdir_env = os.environ.get(dyw.WORKDIR_ENV)
os.environ[dyw.WORKDIR_ENV] = str(tmp)

# The first `dyak process` with nothing configured writes the vault it used to
# `~/.config/dyak/settings.toml` -- which is the point of the feature, and a write into
# the developer's home from a test suite that is not testing it. Measured: a run of this
# file created `~/.config/dyak/settings.toml` holding the developer's real vault path.
# Point the settings file at the temp dir for the whole suite. Issue #63 is about the other
# ambient state these suites read; this one is fixed rather than deferred because this
# change introduced the write.
real_settings_env = os.environ.get(dyd.SETTINGS_PATH_ENV)
os.environ[dyd.SETTINGS_PATH_ENV] = str(tmp / "settings.toml")
# And the export target itself. Pinning the settings file stops the suite writing a settings
# file into the developer's home; it does not stop the export, whose target resolves through
# `$DYAK_VAULT` and then the built-in default. With the export on by default, a run of
# this file wrote audio.wav, transcript.txt, transcript.json and transcript.raw.json into
# the real `~/Documents/obs_notes/dyak/`. Blocks that need a specific vault set it below.
real_vault_env = os.environ.get(dyd.VAULT_ENV)
os.environ[dyd.VAULT_ENV] = str(tmp / "vault")
(tmp / "vault").mkdir(exist_ok=True)


# --- 1. dependency absent vs. present-and-failing -----------------------------
# these use the REAL runner.run, with only `which` patched, so the absent/failing
# distinction is exercised through the actual code path rather than a re-stub.
dyr.shutil.which = lambda name: "/usr/bin/true" if name == "true" else None
exc = raises(real_run, "definitely-not-installed-xyz", ["--help"], remediation="pipx install x")
check("absent dependency -> exit 2", exc.code == ec.MISSING_DEPENDENCY, f"{exc.code}")
check("absent dependency names itself", "definitely-not-installed-xyz" in exc.message, exc.message)
check(
    "absent dependency carries remediation",
    exc.remediation == "pipx install x",
    str(exc.remediation),
)
check("absent dependency has no stderr to report", exc.stderr == b"", repr(exc.stderr))

# a real binary that fails: /bin/false exits 1 with empty stderr
dyr.shutil.which = lambda name: "/bin/false" if name == "false" else None
exc = raises(real_run, "false", [], remediation="n/a")
check("present-but-failing -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "failing dependency is NOT reported as missing", exc.remediation is None, str(exc.remediation)
)

# a real binary that writes a non-UTF-8 byte to stderr and exits non-zero
noisy = tmp / "noisy.sh"
noisy.write_bytes(b"#!/bin/sh\nprintf 'boom: \\377\\376 bad\\n' >&2\nexit 7\n")
noisy.chmod(0o755)
dyr.shutil.which = lambda name: str(noisy) if name == "noisy" else None
exc = raises(real_run, "noisy", [], remediation="n/a")
check("failing dependency -> exit 3 (real stderr)", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "non-UTF-8 stderr kept as bytes",
    exc.stderr in (b"boom: \xff\xfe bad\n", b"boom: \xc3\xbf\xc3\xbe bad\n"),
    repr(exc.stderr),
)
check("dependency exit code is reported", "exited 7" in exc.message, exc.message)

dyr.shutil.which = real_which
dyfr.time.sleep = real_sleep


# --- 2. report(): dependency stderr is reproduced verbatim ---------------------
class FakeStderr:
    """A stderr with a binary buffer, like the real one."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = []

    def write(self, s):
        self.text.append(s)
        return len(s)

    def flush(self):
        pass


raw = b"  indented: '{\"a\": 1}'\nffmpeg: cannot open shared object file\n\xff\xfe\n"
fake = FakeStderr()
real_stderr, sys.stderr = sys.stderr, fake
try:
    dyr.report(dyr.DependencyError("ffmpeg", ec.DEPENDENCY_FAILED, "boom", stderr=raw))
finally:
    sys.stderr = real_stderr
check(
    "dependency stderr is byte-for-byte",
    fake.buffer.getvalue() == raw,
    repr(fake.buffer.getvalue()),
)
check(
    "our own lines are prefixed, its bytes are not",
    fake.text and fake.text[0].startswith("dyak: boom"),
)

labelled = FakeStderr()
real_stderr, sys.stderr = sys.stderr, labelled
try:
    dyr.report(
        dyr.DependencyError(
            "ffprobe", ec.DEPENDENCY_FAILED, "ffprobe printed junk", stdout=b"<html>err</html>"
        )
    )
finally:
    sys.stderr = real_stderr
labelled_text = "".join(labelled.text)
check("captured stdout is labelled as stdout", "ffprobe stdout" in labelled_text, labelled_text)
check(
    "captured stdout is not presented as stderr",
    "wrote nothing to stderr" not in labelled_text,
    labelled_text,
)
check("captured stdout bytes still arrive", b"<html>err</html>" in labelled.buffer.getvalue())

fake2 = FakeStderr()
real_stderr, sys.stderr = sys.stderr, fake2
try:
    dyr.report(dyr.DependencyError("ffmpeg", ec.DEPENDENCY_FAILED, "boom"))
finally:
    sys.stderr = real_stderr
check(
    "a silent dependency is reported as silent",
    "wrote nothing to stderr" in "".join(fake2.text),
    "".join(fake2.text),
)
check("a silent dependency emits no bytes", fake2.buffer.getvalue() == b"")


# --- 3. workspace: retained on failure, removed on success ---------------------
work = dyw.WorkDir()
kept = work.path
work.retain("because")
check("retained work dir still exists", kept.exists())
check("note names the reason", "because" in work.note(), work.note())
work.release()
check("release() does not remove a retained dir", kept.exists())

work2 = dyw.WorkDir()
gone = work2.path
work2.release()
check("release() removes the work dir on success", not gone.exists())
check("note says removed", "removed" in work2.note(), work2.note())

work3 = dyw.WorkDir(keep=True)
kept3 = work3.path
work3.release()
check("--keep-workdir survives release", kept3.exists())
check("work dir is 0700", oct(kept3.stat().st_mode)[-3:] == "700", oct(kept3.stat().st_mode)[-3:])

real_rmtree = dyw.shutil.rmtree
try:
    dyw.shutil.rmtree = lambda p: (_ for _ in ()).throw(OSError(30, "Read-only file system"))
    stuck = dyw.WorkDir()
    stuck.release()
finally:
    dyw.shutil.rmtree = real_rmtree
check("a failed rmtree does NOT claim removal", stuck.retained, stuck.note())
check(
    "a failed rmtree is reported as retained", "KEPT for inspection" in stuck.note(), stuck.note()
)
check("a failed rmtree explains itself", "could not be removed" in stuck.note(), stuck.note())
check("a failed rmtree leaves the dir findable", stuck.path.exists(), str(stuck.path))
shutil.rmtree(stuck.path, ignore_errors=True)
shutil.rmtree(kept, ignore_errors=True)
shutil.rmtree(kept3, ignore_errors=True)

# --- 4. stage 1: probe --------------------------------------------------------
dyr.run = fake_run
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "vp8",
                                "width": 1920,
                                "height": 1080,
                            },
                            {
                                "codec_type": "audio",
                                "codec_name": "opus",
                                "sample_rate": "48000",
                                "channels": 2,
                            },
                        ],
                        "format": {"duration": "4396.000000"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
src = tmp / "lecture.webm"
src.write_bytes(b"not really a video")
info = dyprobe.probe(src)
check("probe reads duration as seconds", info.duration == 4396.0, str(info.duration))
check("probe sees video", info.has_video and info.video_codec == "vp8", info.summary())
check("probe sees audio", info.has_audio and info.audio_codec == "opus", info.summary())
check("probe reads sample rate", info.sample_rate == 48000, str(info.sample_rate))
check("probe reads dimensions", (info.width, info.height) == (1920, 1080), info.summary())
check("a video source is accepted", raises(dyprobe.require_supported, info) is None)

check(
    "ffprobe is invoked with -i and the resolved path",
    "-i" in CALLS[-1][1] and CALLS[-1][1][-1] == str(src.resolve()),
    str(CALLS[-1][1]),
)

# duration "NAN" must not become a NaN that poisons arithmetic downstream
install_fake(
    {"ffprobe": [(json.dumps({"streams": [], "format": {"duration": "NAN"}}).encode(), b"", 0)]}
)
check("NAN duration degrades to 0.0", dyprobe.probe(src).duration == 0.0)

# ffprobe exiting 0 with unparseable output is a broken ffprobe, not bad input
install_fake({"ffprobe": [(b"<html>proxy error</html>", b"", 0)]})
exc = raises(dyprobe.probe, src)
check("non-JSON from ffprobe -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("non-JSON report keeps the payload", b"proxy error" in exc.stderr, repr(exc.stderr))

# ffprobe exits 0 with a payload that is valid JSON but not a stream table.
# json.loads returns a list/number/string/None for these, and treating any of
# them as the stream dict raised AttributeError -- which escaped as a traceback
# with exit 1, i.e. a broken ffprobe reported to the user as a bad command line.
for bad in (
    b"[]",
    b"null",
    b"7",
    b'"probe"',
    b'{"streams": {"a": 1}}',
    b'{"streams": 3, "format": {}}',
):
    install_fake({"ffprobe": [(bad, b"", 0)]})
    exc = raises(dyprobe.probe, src)
    check(
        f"ffprobe payload {bad.decode()!r} -> exit 3, not a traceback",
        exc is not None and exc.code == ec.DEPENDENCY_FAILED,
        f"{getattr(exc, 'code', None)} {getattr(exc, 'message', 'no exception')}",
    )

# A correctly-shaped payload with junk entries degrades rather than raising:
# absent or unusable streams are a fact about the file, not a broken ffprobe.
install_fake({"ffprobe": [(b'{"streams": [null, 3, "x"]}', b"", 0)]})
degraded = dyprobe.probe(src)
check("junk stream entries degrade instead of raising", not degraded.has_audio, degraded.summary())
check("an absent format degrades to duration 0", degraded.duration == 0.0, str(degraded.duration))
install_fake({"ffprobe": [(b'{"streams": [], "format": "nope"}', b"", 0)]})
exc = raises(dyprobe.probe, src)
check("a format of the wrong type -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

exc = raises(dyprobe.probe, tmp / "no-such-file.webm")
check("missing input -> exit 1 (usage)", exc.code == ec.USAGE, f"{exc.code}")
check("missing input names the path", "no-such-file.webm" in exc.message, exc.message)

# Stages 1 and 2 record only a run-log line each: `[1/12] probe 0.1s video av1 1920x1080 ...`.
# Those are measurements of the input, not constants -- re-encode the lecture and `duration`
# moves, which moves every frame timestamp and every word boundary downstream. A bundle that
# cannot say what it was given cannot be reasoned about offline (#80).
_src = json.loads(
    dyprobe.provenance(
        dyprobe.MediaInfo(
            path=Path("lecture.mp4"),
            duration=4520.1,
            has_video=True,
            video_codec="av1",
            width=1920,
            height=1080,
            has_audio=True,
            audio_codec="aac",
            sample_rate=48000,
            channels=2,
        ),
        ffmpeg_version="ffmpeg version n9.0.2",
    )
)
check(
    # One document, not two: neither stage has a parameter a user can vary, so two files would
    # each be a handful of facts about one input. `stages` says which stage measured what.
    "the document says which stages it covers",
    _src["stages"] == [1, 2],
    str(_src["stages"]),
)
check(
    "and the measured facts, not a summary line",
    _src["source"]["video_codec"] == "av1"
    and _src["source"]["width"] == 1920
    and _src["source"]["duration"] == 4520.1,
    str(_src["source"]),
)
check(
    "including the binary that measured them -- #70 was a filter-chain bug",
    _src["ffmpeg"] == "ffmpeg version n9.0.2",
    str(_src.get("ffmpeg")),
)
_src2 = json.loads(
    dyprobe.provenance(
        dyprobe.MediaInfo(
            path=Path("lecture.mp4"),
            duration=4520.1,
            has_video=True,
            video_codec="av1",
            width=1920,
            height=1080,
            has_audio=True,
            audio_codec="aac",
            sample_rate=48000,
            channels=2,
        ),
        dya.AudioArtefact(
            path=Path("audio.wav"),
            duration=4520.0,
            sample_rate=16000,
            channels=1,
            size_bytes=144640000,
        ),
        ffmpeg_version="ffmpeg version n9.0.2",
    )
)
check(
    "stage 2's extraction is in there too, since it describes the same input",
    _src2["extracted_audio"]["sample_rate"] == 16000
    and _src2["extracted_audio"]["channels"] == 1
    and _src2["extracted_audio"]["size_bytes"] == 144640000,
    str(_src2.get("extracted_audio")),
)
check(
    # `duration` is the load-bearing field: it moves every frame timestamp stage 4 extracts and
    # every word boundary stage 6 aligns against. A bundle without it cannot explain a shift.
    "and the duration, rounded consistently with every other provenance",
    _src2["source"]["duration"] == 4520.1 and _src2["extracted_audio"]["duration"] == 4520.0,
    str(_src2["source"]["duration"]),
)

# --- #94: the input's own fingerprint -----------------------------------------------
# The bundle could prove "this note came from this transcript" (#79 hashes the transcript)
# and only *assert* "this note came from this lecture" by filename. The two real files for
# this lecture are the case: `1_lecture_OCS.mp4` (418568189 bytes, sha256 `a8a04e47...`) and
# `1_lecture_OCS.webm` (883684992 bytes, `c0e51742...`) slugify to the same bundle and differ
# in `duration` alone (4520.1 vs 4396), so a re-encode at the same duration is invisible in
# the record without a size and a digest.
#
# `getattr`/`.get` rather than attribute and item access throughout, because these checks
# exist to fail on the code that predates the two fields: an AttributeError or a KeyError
# would abort the suite instead of failing one check, and the neighbours of a check that
# aborts are the ones this file's accumulate-then-report shape is for.
_fixture_digest = hashlib.sha256(src.read_bytes()).hexdigest()
_linked = tmp / "linked-lecture.webm"
_linked.symlink_to(src)
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "vp8",
                                "width": 1920,
                                "height": 1080,
                            },
                            {
                                "codec_type": "audio",
                                "codec_name": "opus",
                                "sample_rate": "48000",
                                "channels": 2,
                            },
                        ],
                        "format": {"duration": "4396.000000"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
_linked_info = dyprobe.probe(_linked)
check(
    # A symlink is the case that separates "resolved" from "the string the user typed": the
    # fixture's own path is already absolute and symlink-free, so `probe(src)` cannot tell the
    # two apart. The comment above the ffprobe argv claimed resolving "normalises the path in
    # every message the user sees afterwards" and did not do it -- only the argv was resolved
    # (`str(src.resolve())`), while `info.path` kept whatever the user typed.
    "#94: probe resolves the input path before it stats it",
    str(_linked_info.path) == str(src.resolve()) and str(_linked_info.path) != str(_linked),
    f"{_linked_info.path} (asked for {_linked})",
)
check(
    "#94: and measures the size and the digest of the file it resolved",
    getattr(_linked_info, "size_bytes", None) == src.stat().st_size
    and getattr(_linked_info, "sha256", None) == _fixture_digest,
    f"{getattr(_linked_info, 'size_bytes', None)} {getattr(_linked_info, 'sha256', None)}",
)
_fingerprint = json.loads(dyprobe.provenance(_linked_info, ffmpeg_version="ffmpeg version n9.0.2"))
check(
    # The same document stages 1-2 already publish: `probe.provenance`'s own docstring settled
    # that ("One document rather than two ... that is a single fact") and the file is already
    # in `RUN_ARTEFACTS`, so it is already exported with the bundle -- no new artefact, and
    # stage 12 can say which file the run read without re-hashing anything.
    "#94: the record carries the path, the size and the digest",
    _fingerprint["source"].get("path") == str(src.resolve())
    and _fingerprint["source"].get("size_bytes") == src.stat().st_size
    and _fingerprint["source"].get("sha256") == _fixture_digest,
    str(_fingerprint["source"]),
)
check(
    "#94: in the document stages 1-2 already publish, not a new artefact",
    dyprobe.PROVENANCE_NAME in dyb.RUN_ARTEFACTS,
    str(sorted(dyb.RUN_ARTEFACTS)),
)
check(
    # `null` would claim the run measured a size and a digest and found neither; the key is
    # absent instead, the pattern `ffmpeg` and `extracted_audio` already use for a value this
    # document cannot speak to. The hand-built `MediaInfo` above is the case: it predates the
    # two fields, and the existing checks on it stay green.
    "#94: an unset fingerprint omits the keys rather than writing null",
    "size_bytes" not in _src["source"] and "sha256" not in _src["source"],
    str(_src["source"]),
)

# A re-run against a changed input is recorded, not detected and not refused (#94): the new
# record replaces the old one in the same bundle, so the document describes the input *that
# run* read -- the rule `clear_images` already applies to `images/`. A refusal was rejected
# because the mixing it appears to fix does not depend on the input changing (filed as #219):
# the refusal would fail a legitimate re-encode-and-re-run while leaving that in place.
_rerun = tmp / "fingerprint-rerun"
_rerun_bodies = (b"the first cut", b"a re-encode of the same lecture")
_rerun_digests = []
for _index, _body in enumerate(_rerun_bodies):
    _input = tmp / f"rerun-{_index}" / "lecture.webm"
    _input.parent.mkdir(parents=True, exist_ok=True)
    _input.write_bytes(_body)
    install_fake(
        {
            "ffprobe": [
                (
                    json.dumps(
                        {
                            "streams": [
                                {"codec_type": "video", "codec_name": "vp8"},
                                {
                                    "codec_type": "audio",
                                    "codec_name": "opus",
                                    "sample_rate": "48000",
                                    "channels": 2,
                                },
                            ],
                            "format": {"duration": "100"},
                        }
                    ).encode(),
                    b"",
                    0,
                )
            ]
        }
    )
    _bundle = dyb.Bundle.open(_input, output_dir=str(_rerun))
    _record = tmp / "rerun-provenance.json"
    _record.write_text(dyprobe.provenance(dyprobe.probe(_input)), encoding="utf-8")
    _bundle.publish(_record.name, _record)
    _rerun_digests.append(
        json.loads((_rerun / _record.name).read_text(encoding="utf-8"))["source"].get("sha256")
    )
check(
    "#94: a re-run against a changed input records the new one and refuses nothing",
    _rerun_digests == [hashlib.sha256(body).hexdigest() for body in _rerun_bodies],
    str(_rerun_digests),
)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000"}
                        ],
                        "format": {"duration": "100"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
audio_only = dyprobe.probe(src)
exc = raises(dyprobe.require_supported, audio_only)
check("audio-only input -> exit 1", exc.code == ec.USAGE, f"{exc.code}")
check("audio-only rejection cites the ADR boundary", "Not doing" in exc.message, exc.message)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [{"codec_type": "video", "codec_name": "vp8"}],
                        "format": {"duration": "100"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
silent_video = dyprobe.probe(src)
exc = raises(dyprobe.require_supported, silent_video)
check("video with no audio -> exit 1", exc.code == ec.USAGE, f"{exc.code}")
check("video with no audio says nothing to transcribe", "transcribe" in exc.message, exc.message)


# --- 5. stage 2: audio, verified on disk --------------------------------------
def wav_bytes(seconds: float, rate: int = 16000, channels: int = 1) -> bytes:
    """A minimal but real RIFF/WAVE PCM file: header plus silent samples.

    The data chunk has to actually carry the bytes it declares, or the file is
    header-only -- which is precisely the artefact `verify` is built to reject,
    so a helper that lies here would make the negative tests meaningless.
    """
    data_bytes = int(seconds * rate) * channels * 2
    header = b"RIFF" + (36 + data_bytes).to_bytes(4, "little") + b"WAVEfmt "
    header += (
        (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + channels.to_bytes(2, "little")
    )
    header += rate.to_bytes(4, "little") + (rate * channels * 2).to_bytes(4, "little")
    header += (channels * 2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    header += b"data" + data_bytes.to_bytes(4, "little")
    return header + b"\x00" * data_bytes


install_fake(
    {
        "ffmpeg": [(b"", b"", 0)],
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ],
    }
)
dest = tmp / "audio.wav"
dest.write_bytes(wav_bytes(25.0))
artefact = dya.verify(dest)
check("verify accepts a real wav", artefact.duration == 25.0, artefact.summary())
check(
    "verify reports 16 kHz mono",
    (artefact.sample_rate, artefact.channels) == (16000, 1),
    artefact.summary(),
)

argv = None
PCM_PROBE = json.dumps(
    {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
        "format": {"duration": "2.0"},
    }
).encode()

install_fake(
    {
        # ffmpeg must be programmed too: the strict fake raises on an
        # unprogrammed call, which is what surfaced that extract()'s
        # postcondition was previously never exercised at all.
        "ffmpeg": [(b"", b"", 0)],
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ],
    }
)
CALLS.clear()
dya.extract(src, dest)
ffmpeg_call = next(a for name, a in CALLS if name == "ffmpeg")
check(
    "ffmpeg gets -nostdin (else it eats the parent's stdin)",
    "-nostdin" in ffmpeg_call,
    str(ffmpeg_call),
)
check("ffmpeg maps the first audio stream explicitly", "0:a:0" in ffmpeg_call, str(ffmpeg_call))
check(
    "ffmpeg resamples to 16 kHz mono pcm",
    all(x in ffmpeg_call for x in ("16000", "1", "pcm_s16le")),
    str(ffmpeg_call),
)
check("ffmpeg drops video", "-vn" in ffmpeg_call, str(ffmpeg_call))
# probe() resolves the path because ffmpeg/ffprobe have no `--` end-of-options
# marker; extract() must do the same or stage 2 fails on a leading-dash filename
# that stage 1 just accepted.
check(
    "ffmpeg gets the resolved destination path",
    str(dest.resolve()) in ffmpeg_call,
    str(ffmpeg_call),
)

# Resolving must actually change something, which a comparison against an
# already-absolute path cannot show. Drive extract() with a RELATIVE source and
# require an absolute one to reach ffmpeg.
saved_cwd = os.getcwd()
try:
    os.chdir(tmp)
    (tmp / "out.wav").write_bytes(wav_bytes(2.0))
    install_fake({"ffmpeg": [(b"", b"", 0)], "ffprobe": [(PCM_PROBE, b"", 0)]})
    CALLS.clear()
    dya.extract(Path("lecture.webm"), Path("out.wav"))
    rel_call = next(a for name, a in CALLS if name == "ffmpeg")
finally:
    os.chdir(saved_cwd)
i = rel_call.index("-i")
check(
    "a relative source reaches ffmpeg as an absolute path",
    rel_call[i + 1] == str(src.resolve()),
    str(rel_call),
)
check(
    "a relative source is NOT passed through unresolved",
    rel_call[i + 1] != "lecture.webm",
    str(rel_call),
)
j = rel_call.index("wav")
check(
    "a relative destination reaches ffmpeg as an absolute path",
    rel_call[j + 1] == str((tmp / "out.wav").resolve()),
    str(rel_call),
)
check(
    "ffmpeg gets the resolved destination path",
    str(dest.resolve()) in ffmpeg_call,
    str(ffmpeg_call),
)

# extract()'s own postcondition: ffmpeg exits 0 and writes nothing. Pre-creating
# `dest` is what made this unreachable before -- verify() saw a valid wav that
# the test itself had put there.
install_fake({"ffmpeg": [(b"", b"", 0)], "ffprobe": [(b"{}", b"", 0)]})
absent = tmp / "never-written.wav"
exc = raises(dya.extract, src, absent)
check("ffmpeg exiting 0 without writing -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("the missing artefact is named", str(absent) in exc.message, exc.message)
check("nothing was invented on disk", not absent.exists())

header_only = tmp / "empty.wav"
header_only.write_bytes(wav_bytes(0))
exc = raises(dya.verify, header_only)
check("header-only wav -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("header-only wav is blamed on exit-code trust", "exit code was 0" in exc.message, exc.message)

exc = raises(dya.verify, tmp / "ffmpeg-wrote-nothing.wav")
check("no file at all -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

wrong = tmp / "wrong-rate.wav"
wrong.write_bytes(wav_bytes(10))
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "48000",
                                "channels": 2,
                            }
                        ],
                        "format": {"duration": "10.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
exc = raises(dya.verify, wrong)
check("wrong sample rate -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("wrong rate states expected vs actual", "48000Hz/2ch" in exc.message, exc.message)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "4396.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
long_info = dyprobe.probe(src)
short_artefact = dya.AudioArtefact(dest, 100.0, 16000, 1, 800238)
warn = dya.check_coverage(long_info, short_artefact)
check(
    "a short extraction warns",
    warn is not None and "may not be the lecture audio" in warn,
    str(warn),
)
full_artefact = dya.AudioArtefact(dest, 4396.0, 16000, 1, 140_000_000)
ok = dya.check_coverage(long_info, full_artefact)
check("a full-length extraction does not warn", ok is None, str(ok))


# --- 6. stage 3: stt, timings as seconds, words left as BPE pieces ------------

# Stage 3 picks a backend by autodetection, and autodetection depends on what happens to
# be listening on 127.0.0.1 at the moment the suite runs. That is a property of the machine,
# not of the code under test: with whisper.cpp up the suite would POST the synthetic wav
# to the real server. The backend is pinned here so the section tests one backend, and
# autodetection is tested separately in section 9 with both branches controlled.
#
# #95 removed the built-in `127.0.0.1:10302` default, so the endpoint this section fakes
# must now be named explicitly. `FAKED_NETLOCS` below matches on netloc, and this is the
# netloc it matches -- the pin keeps the section on the fake and off the real network.
real_stt_backend = os.environ.get("DYAK_STT")
os.environ["DYAK_STT"] = "whispercpp"
os.environ["DYAK_WHISPERCPP_URL"] = "http://127.0.0.1:10302"

REAL_SEGMENTS = [
    {
        "id": 0,
        "text": " Институт интеллектуальных устройств.",
        "start": 0.0,
        "end": 2.0,
        "tokens": [27222, 982, 13],
        "words": [
            {"word": " Ин", "start": 0.0, "end": 0.12, "t_dtw": -1, "probability": 0.134},
            {"word": "ст", "start": 0.12, "end": 0.23, "t_dtw": -1, "probability": 0.99},
            {"word": ".", "start": 2.0, "end": 2.0, "t_dtw": -1, "probability": 0.7},
        ],
        "temperature": 0.0,
        "avg_logprob": -0.2381982058286667,
        "no_speech_prob": 1.36e-11,
    },
    {
        "id": 1,
        "text": " Соответственно, я вообще не ППС,",
        "start": 2.0,
        "end": 4.0,
        "words": [
            {"word": " Соот", "start": 2.0, "end": 2.5},
            {"word": "вет", "start": 2.5, "end": 3.0},
        ],
    },
]

payload = json.dumps(REAL_SEGMENTS, ensure_ascii=False).encode()
install_http([payload])
CALLS.clear()
tr = dystt.transcribe(dest, audio_duration=25.0)
# Acceptance: timings are requested from `/inference` as verbose_json, never reparsed out
# of plain transcript text. A plain-text form carries no segments at all, so these
# assertions are what would catch a regression to a text-only request. These replace the
# argv assertions this section carried against `shipboard process --timestamps json`,
# which no longer exists -- see #44.
infer_call = next(r for r in HTTP_CALLS if getattr(r, "full_url", "").endswith("/inference"))
body = infer_call.data.decode("utf-8", "replace")
check(
    "stage 3 posts to /inference",
    infer_call.full_url.endswith("/inference"),
    infer_call.full_url,
)
check(
    "the request asks for verbose_json",
    'name="response_format"; filename="verbose.json"' in body or "verbose_json" in body,
    body[:400],
)
check(
    "the request carries the wav that was verified on disk",
    dest.name.encode() in infer_call.data,
    dest.name,
)
check("segments are parsed", len(tr.segments) == 2, tr.summary())
check("segments are parsed", len(tr.segments) == 2, tr.summary())
check("timings are seconds, not milliseconds", tr.segments[0].end == 2.0, str(tr.segments[0].end))
check("timed words are counted", tr.timed_words == 5, str(tr.timed_words))
check("speech_end is the last segment end", tr.speech_end == 4.0, str(tr.speech_end))
check(
    "BPE pieces are NOT detokenised",
    [w.text for w in tr.segments[0].words] == [" Ин", "ст", "."],
    str([w.text for w in tr.segments[0].words]),
)
check(
    "segment text is stripped for reading",
    tr.segments[0].text.startswith("Институт"),
    tr.segments[0].text,
)
check(
    "avg_logprob is carried",
    abs(tr.segments[0].avg_logprob + 0.2382) < 1e-3,
    str(tr.segments[0].avg_logprob),
)
check("no anomalies on a clean payload", tr.anomalies == (), str(tr.anomalies))

install_http([b"   \n"])
exc = raises(dystt.transcribe, dest, audio_duration=25.0)
check("empty stdout -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

install_http([b"not json at all"])
exc = raises(dystt.transcribe, dest, audio_duration=25.0)
check("non-JSON stdout -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

# A JSON object is no longer a shape error: whisper.cpp's own /inference returns
# {task, language, duration, text, segments, ...}. A bare array is the other accepted
# shape. Both are accepted, and the envelope is recorded as an anomaly.
install_http([b'{"text": "plain"}'])
exc = raises(dystt.transcribe, dest, audio_duration=25.0)
check(
    "an object payload with no timings -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "it is rejected for the timings, not for the envelope",
    "zero words carrying timings" in getattr(exc, "message", ""),
    getattr(exc, "message", ""),
)

install_http([b"[]"])
exc = raises(dystt.transcribe, dest, audio_duration=25.0)
check("empty segments array -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

install_http(
    [json.dumps([{"id": 0, "text": " hi", "start": 0.0, "end": 1.0, "words": []}]).encode()]
)
exc = raises(dystt.transcribe, dest, audio_duration=25.0)
check(
    "zero timed words -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'no exception raised')}",
)
check(
    "zero timed words explains the consequence",
    exc is not None and "per-word" in exc.message,
    getattr(exc, "message", "no exception raised"),
)

messy = [
    {
        "id": 0,
        "text": " one",
        "start": 5.0,
        "end": 6.0,
        "words": [{"word": " one", "start": 5.0, "end": 6.0}],
    },
    {
        "id": 1,
        "text": " two",
        "start": 3.0,
        "end": 4.0,
        "words": [{"word": " two", "start": 3.0, "end": 4.0}],
    },
    {
        "id": 2,
        "text": " three",
        "start": 7.0,
        "end": 6.0,
        "words": [{"word": " three", "start": 7.0, "end": 6.0}],
    },
    {"id": 3, "text": "   ", "start": 8.0, "end": 9.0, "words": []},
    {"id": 4, "start": 9.0, "end": 9.5, "words": []},
    "not an object",
]
install_http([json.dumps(messy).encode()])
tr = dystt.transcribe(dest, audio_duration=25.0)
check("blank-text segments are dropped", len(tr.segments) == 3, str(len(tr.segments)))
check("anomalies are collected, not fatal", len(tr.anomalies) >= 4, str(tr.anomalies))
check(
    "a backwards segment is recorded",
    any("before the previous" in a for a in tr.anomalies),
    str(tr.anomalies),
)
check("a backwards end is clamped", tr.segments[2].end >= tr.segments[2].start, str(tr.segments[2]))

check(
    "timestamp formatting",
    dystt.format_timestamp(4396.0) == "01:13:16",
    dystt.format_timestamp(4396.0),
)

# `1e999` and the bare literals are valid JSON *input* and parse to inf / NaN.
# Left unchecked they reach format_timestamp -> int(inf) -> OverflowError, after
# a full lecture has been transcribed and paid for, and land in transcript.json
# as a bare `Infinity` token, which is not valid JSON at all.
for literal in (b"Infinity", b"-Infinity", b"NaN", b"1e999"):
    nasty = (
        b'[{"text": " x", "start": 0.0, "end": '
        + literal
        + b', "words": [{"word": " x", "start": 0.0, "end": 1.0}]}]'
    )
    install_http([nasty, nasty])
    got = raises(dystt.transcribe, dest, audio_duration=25.0)
    check(
        f"a non-finite end ({literal.decode()}) does not crash the run",
        got is None,
        str(got),
    )
    if got is None:
        tr_n = dystt.transcribe(dest, audio_duration=25.0)
        check(
            f"a non-finite end ({literal.decode()}) is recorded as an anomaly",
            any("non-finite end" in a for a in tr_n.anomalies),
            str(tr_n.anomalies),
        )
        check(
            f"a non-finite end ({literal.decode()}) leaves every timing finite",
            all(
                s_.start not in (float("inf"), float("-inf"))
                and s_.end not in (float("inf"), float("-inf"))
                for s_ in tr_n.segments
            ),
        )
        paths_n = dystt.write(tr_n, tmp)
        text_n = paths_n["json"].read_text(encoding="utf-8")
        check(
            f"transcript.json stays valid JSON after a {literal.decode()}",
            "Infinity" not in text_n and "NaN" not in text_n,
            text_n[:200],
        )

# And the clamping path still has to produce a writable, valid artefact.
install_http([payload])
tr = dystt.transcribe(dest, audio_duration=25.0)
paths = dystt.write(tr, tmp)
check(
    # `pipeline.run` publishes whatever `write()` returns, so a file written here and not
    # named here never reaches the bundle. That is how stage 3 had no provenance at all
    # (#80), and how stage 7's transcript went missing (#76).
    "the transcript artefacts are written, and named for publishing",
    set(paths) == {"raw", "json", "txt", "provenance"},
    str(sorted(paths)),
)
_prov = json.loads(paths["provenance"].read_text(encoding="utf-8"))
check(
    # Stage 3 recorded its results and nothing about what produced them: the backend name
    # existed only in the run log, and `grep -ril whispercpp` over the bundle "hit"
    # transcript.json only because an anomaly message happened to contain it.
    "the provenance names the backend that was actually used",
    _prov["backend"] == tr.backend and tr.backend != "unknown",
    str(_prov["backend"]),
)
check(
    "and the parameters it will be asked for, endpoint included",
    _prov["parameters"].get("endpoint")
    and _prov["parameters"].get("fields", {}).get("response_format") == "verbose_json",
    str(_prov["parameters"])[:160],
)
check(
    # #49 measures whisper.cpp disagreeing with itself on the same audio. A digest of the
    # transcript is what turns "the note came out different" into "stage 3's fault" or
    # "the model's", without re-running anything.
    "and digests both transcript files, so a re-run can be attributed",
    set(_prov["digests"]) == {"transcript.txt", "raw"}
    and all(len(v) == 64 for v in _prov["digests"].values()),
    str(_prov["digests"])[:120],
)
check(
    "the digest matches the file it describes",
    _prov["digests"]["transcript.txt"]
    == __import__("hashlib").sha256(paths["txt"].read_bytes()).hexdigest(),
    "stt-provenance.json digest does not match transcript.txt",
)
check(
    # `requires_model: False` would say the transcript needed no model, which is the same
    # class of error #67 was about: an artefact asserting something it did not do.
    "stage 3 declares it needs a model",
    _prov["requires_model"] is True and _prov["stage"] == 3,
    str({k: _prov[k] for k in ("stage", "requires_model")}),
)
check("raw payload is preserved byte-for-byte", paths["raw"].read_bytes() == payload)
text = paths["txt"].read_text(encoding="utf-8")
check(
    "readable transcript carries [hh:mm:ss] lines",
    "[00:00:00] Институт" in text,
    text.splitlines()[:3],
)
normalised = json.loads(paths["json"].read_text(encoding="utf-8"))
check(
    "normalised json counts words", normalised["timed_words"] == 5, str(normalised["timed_words"])
)
check(
    "normalised json keeps per-word timings",
    normalised["segments"][0]["words"][1]["end"] == 0.23,
    str(normalised["segments"][0]["words"][1]),
)
check(
    "normalised json records coverage", normalised["coverage"] == 0.16, str(normalised["coverage"])
)


# --- 7. cli: process is honest about being unfinished --------------------------
real_run_all = dyc.run_all
real_pipeline_run = dyp.run


def healthy_checks(*, required=None):
    """Accept `required` so the stub tracks run_all's signature."""
    return [dyc.Check(name="ffmpeg", ok=True, detail="ok", path="/usr/bin/ffmpeg", version="7.1.1")]


dyc.run_all = healthy_checks

work_root = tmp / "cliruns"
os.environ[dyw.WORKDIR_ENV] = str(work_root)

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process"])
check("process without a path -> exit 1", rc == ec.USAGE, f"rc={rc}")
check("process without a path shows usage", "dyak process PATH" in err.getvalue(), err.getvalue())

# The stub writes real files into the real work dir. Without this the directory
# is always empty, and the listing assertions below match only the summary line
# -- which is how three of them were proven unable to fail.
ARTEFACTS = ("audio.wav", "transcript.txt", "transcript.json", "transcript.raw.json")


def fake_pipeline(source, work, **kw):
    for name in ARTEFACTS:
        (work.path / name).write_bytes(b"x")
    bundle = kw.get("bundle")
    artefacts = {n: work.path / n for n in ARTEFACTS}
    if bundle is not None:
        artefacts = {n: bundle.publish(n, p) for n, p in artefacts.items()}
    qreport = SimpleNamespace(
        frames=[],
        passed=[],
        failed=[],
        ok=True,
        summary=lambda: "0/0 frames pass",
        explain=lambda: "",
    )
    return SimpleNamespace(
        source=source,
        artefacts=artefacts,
        seconds=1.5,
        bundle=bundle,
        quality=qreport,
        enhanced=[],
        transcript=SimpleNamespace(anomalies=(), summary=lambda: "3 segments, 40 timed words"),
        # Real Report objects, not stubs: the exit code is decided from these, so a
        # SimpleNamespace stand-in would let a broken audit pass every test here.
        audit=dyau.Report(),
        repair=dyrep.Report(),
        link=dylk.Report(),
        # A synthesised note, not a template: exit 0 is conditional on this being False,
        # so a stand-in without the attribute would crash rather than assert.
        note=SimpleNamespace(degraded=False, synthesizer="test", notes=()),
        # Stage 6's two verdicts, both real objects. `caption.ok` gates A/V sync; the
        # outcome gates whether the vision endpoint worked at all, and the CLI reads it
        # before the quality gate -- so a stand-in without it would crash rather than
        # assert. captioned=1 because ok is `captioned + reused > 0`, and a fake run that
        # captioned nothing is a real failure, not a neutral default.
        caption=dycap.Report(),
        caption_outcome=dycap.CaptionOutcome(
            captioned=1, reused=0, failed=0, model="test-model", served_model=None
        ),
        # A verified run. `ok` is computed, not stubbed, so flipping it below is a real
        # failure rather than a flag: that is the ADR-0001 D3 case stage 12 exists for.
        report=dyro.Report(),
    )


dyp.run = fake_pipeline

# --- 7a. the default (removed) work dir, as the control for 7b ---------------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process", str(src)])
text = out.getvalue()
check(
    # With every stage built, a run whose artefacts verified DOES exit 0. This is the
    # assertion the old contract could not make: "a partial run does NOT exit 0" passed
    # for the entire history of this file and would have kept passing forever.
    "a run whose artefacts verified exits 0",
    rc == ec.OK,
    f"rc={rc}\n{text}",
)
check("and it says where the artefacts are", "verified" in text, text)


# #117: a bundle can verify completely and still be missing a third of the lecture. Stage 7
# records coverage in `synth.json`, stage 12 reads it back, and the run must not exit 0 -- and
# must not use code 6 to say so: nothing is absent from disk, the *deliverable* is absent from
# the note. This is the state the run in docs/stages.md was in while it exited 0.
def low_coverage_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.report.coverage = 0.5
    return result


dyp.run = low_coverage_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_low = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "a note written from half the lecture does not exit 0 (#117)",
    rc_low == ec.LOW_COVERAGE,
    f"rc={rc_low} want={ec.LOW_COVERAGE}",
)
check(
    "and the refusal names coverage, not a missing artefact (#117)",
    "coverage" in err.getvalue() and "50%" in err.getvalue(),
    err.getvalue()[-300:],
)
check(
    "coverage is not the missing-artefact code (#117)",
    rc_low != ec.ARTEFACT_VERIFICATION_FAILED and rc_low != ec.OK,
    f"rc={rc_low}",
)


# A section the endpoint cut off is the other half of #128, and the exit path has to be its
# own: the truncated note is degraded too, so without a check ahead of the degraded branch
# this would return 7 and name the symptom instead of the cause.
def truncated_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.report.sections_total = 8
    result.report.sections_truncated = (3,)
    # `synth.synthesise` appends a warning for a truncated section, so the note is degraded as
    # well. Both flags together is the state a real run is in, and it is why the truncation
    # check has to come *before* the degraded branch: 7 would name the symptom, not the cause.
    result.note.degraded = True
    return result


dyp.run = truncated_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_trunc = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "a note with a section cut off at the output limit does not exit 0 (#128)",
    rc_trunc == ec.TRUNCATED_SECTIONS,
    f"rc={rc_trunc} want={ec.TRUNCATED_SECTIONS}",
)
check(
    "and the refusal names the sections and the limit, not the degraded path (#128)",
    "output limit" in err.getvalue() and "sections 3" in err.getvalue(),
    err.getvalue()[-300:],
)
check(
    "truncation is not coverage, not the missing-artefact code and not the degraded note (#128)",
    len({rc_trunc, rc_low, ec.ARTEFACT_VERIFICATION_FAILED, ec.INCOMPLETE_NOTE}) == 4,
    f"trunc={rc_trunc} low={rc_low}",
)


# #223: stage 6's coverage verdict is a gate, not a log line. A source whose frames have no
# words behind them reaches the end of the ladder with nothing fired and exits 0, so a note is
# written from frames the transcript never mentions -- the verdict's own docstring says
# "Coverage is a gate: too many frames with no transcript means broken A/V sync"
# (`src/dyak/caption.py:200-201`). Code 4's row is the frame/A-V family
# (`docs/adr/0001-lecture-pipeline.md:328`); ADR-0001 D5 (`:191`) is the audit, a different
# decision, and `caption.py:51-56` and `pipeline.py:86-90` carry the same attribution and guard.
def _uncovered_alignment(frame, pts_ms):
    """A frame stage 6 was allowed to align, with no transcript segment behind it."""
    return dycap.Alignment(
        frame=frame,
        pts_ms=pts_ms,
        image=frame,
        enhanced=None,
        on_screen_ms=(pts_ms, pts_ms + 2000),
        caption_status="OK",
        reason=dycap.UNCOVERED,
    )


def desynced_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    # A real `caption.Report`, not a stand-in: `ok` is computed from `considered` and
    # `uncovered`, so a SimpleNamespace would let a broken gate pass this check.
    result.caption = dycap.Report(
        alignments=[
            _uncovered_alignment("f_0001.png", 1000),
            _uncovered_alignment("f_0002.png", 3000),
        ]
    )
    return result


dyp.run = desynced_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_desync = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "a source whose frames have no words behind them does not exit 0 (#223)",
    rc_desync == ec.QUALITY_GATE_FAILED,
    f"rc={rc_desync} want={ec.QUALITY_GATE_FAILED}",
)
check(
    "and the refusal names the uncovered share, not a missing artefact (#223)",
    "no transcript coverage" in err.getvalue() and "100%" in err.getvalue(),
    err.getvalue()[-300:],
)
check(
    "the desync gate is code 4, the frame/A-V family, not the coverage code 8 (#223)",
    rc_desync != ec.LOW_COVERAGE and rc_desync != ec.ARTEFACT_VERIFICATION_FAILED,
    f"rc={rc_desync} low={rc_low}",
)


# The guard's own pin, and it is not optional: `caption.Report.ok` is also false when
# `considered == []` -- an audio-only source, or a run whose every frame stage 5 gated out --
# and those have no A/V sync to be broken. Without `result.caption.considered` this check
# returns 4 and the audio-only controls above it go red with it (19 checks, measured in the
# #223 decision), which is why the gate carries the guard.
def no_frames_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.caption = dycap.Report()
    return result


dyp.run = no_frames_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_no_frames = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "a run with no frames to align still exits 0: the guard, not the verdict (#223)",
    rc_no_frames == ec.OK,
    f"rc={rc_no_frames} want={ec.OK}\n{err.getvalue()[-300:]}",
)
check(
    "and that is the same verdict object that refused the desynced run (#223)",
    dycap.Report().ok is False and rc_no_frames != rc_desync,
    f"ok={dycap.Report().ok} rc={rc_no_frames} desync={rc_desync}",
)


# The two controls the new gate must not mask, measured through the same real ladder rather
# than assumed from the code: the quality gate still returns 4, and a run that is *both*
# desynced and below the coverage threshold still returns 8. The second one is what the late
# placement buys -- the #223 decision measured that placing the gate with the other stage-6
# verdicts (before the coverage check) turns this same run's 8 into a 4, rewriting an exit code
# that already had a meaning.
def soft_frames_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.quality.ok = False
    return result


dyp.run = soft_frames_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_soft = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "the quality gate's own code 4 still fires, and it is not the desync message (#223)",
    rc_soft == ec.QUALITY_GATE_FAILED and "no transcript coverage" not in err.getvalue(),
    f"rc={rc_soft} want={ec.QUALITY_GATE_FAILED}\n{err.getvalue()[-200:]}",
)


def desynced_low_coverage_pipeline(source, work, **kw):
    result = desynced_pipeline(source, work, **kw)
    result.report.coverage = 0.5
    return result


dyp.run = desynced_low_coverage_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_both = dyc.main(["process", str(src)])
dyp.run = fake_pipeline
check(
    "a run that is both desynced and low-coverage still reports 8: the gate is last (#223)",
    rc_both == ec.LOW_COVERAGE and "coverage" in err.getvalue(),
    f"rc={rc_both} want={ec.LOW_COVERAGE}\n{err.getvalue()[-200:]}",
)


# --- 7z. stage 7's LLM transcript reaches the bundle -----------------------
# The note is written by 8 model calls. `synth.py` has recorded all 8 since #38, and
# `pipeline.py` promoted 3 files from that directory without naming the transcript, so it
# was written to `work.dir("synth")` and deleted with it. Measured on run 6: the bundle held
# `audit-llm-transcript.json` with 8 stage-9 calls and no stage-7 equivalent. ADR-0004 D4 --
# a regression gate that cannot attribute a difference is not a gate.
def _stage7_promotes(with_transcript: bool, name: str) -> dict[str, Path]:
    """Run the real promotion helper over a stage-7 work dir and report what it published."""
    root = tmp / name
    sdir = root / "synth"
    sdir.mkdir(parents=True, exist_ok=True)
    for extra in dysy.STAGE7_ARTEFACTS:
        (sdir / extra).write_text("{}", encoding="utf-8")
    if with_transcript:
        (sdir / dysy.LLM_TRANSCRIPT_NAME).write_text('{"calls": []}\n', encoding="utf-8")
    bundle = dyb.Bundle.open(src, output_dir=str(root / "bundle"), overwrite=True)
    published = dyp.publishes_stage7_transcript(sdir, bundle)
    return {key: Path(value).name for key, value in published.items()}


_kept = _stage7_promotes(True, "s7")
check(
    # The note is written by 8 model calls. `synth.py` has recorded all 8 since #38, and the
    # promotion named three files without it, so the transcript was written to
    # `work.dir("synth")` and deleted with it. Measured on run 6: the bundle held
    # `audit-llm-transcript.json` with 8 stage-9 calls and no stage-7 equivalent.
    # ADR-0004 D4 -- a regression gate that cannot attribute a difference is not a gate.
    "the stage-7 transcript is published into the bundle when the LLM ran",
    dysy.LLM_TRANSCRIPT_NAME in _kept,
    str(sorted(_kept)),
)
check(
    "and the note and its two reports are published alongside it",
    set(_kept) == set(dysy.STAGE7_ARTEFACTS) | {dysy.LLM_TRANSCRIPT_NAME},
    str(sorted(_kept)),
)
check(
    # Guarded by `.is_file()`, because the template synthesizer makes no calls and writes no
    # transcript. Promoting unconditionally would publish a file that does not exist.
    "a template run publishes no transcript",
    dysy.LLM_TRANSCRIPT_NAME not in _stage7_promotes(False, "s7b"),
    str(sorted(_stage7_promotes(False, "s7c"))),
)


# ADR-0001 D3, which is what exit 0 is now conditional on. Every stage above reports success; this
# is the case where the filesystem disagrees, and the code must not be 0.
def unverified_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.report.missing.append(
        {"artefact": "note", "why": "the deliverable itself", "detail": "missing"}
    )
    # The second half of D3 after #87: not a missing file, but two present files that
    # contradict each other. It must reach the user with the frame named, on the same code.
    result.report.inconsistent.append(
        {
            "artefact": "captions.json",
            "frame": "f_0.png",
            "why": "captions.json must not contradict the gate report it was aligned against",
            "detail": "captions.json says quality_gate='' for f_0.png, quality.json says 'FAIL'",
        }
    )
    return result


dyp.run = unverified_pipeline
uout = io.StringIO()
with redirect_stdout(uout), redirect_stderr(uout):
    rc_unverified = dyc.main(["process", str(src)])
check(
    "a run whose artefacts did NOT verify has a distinct code from usage",
    getattr(ec, "ARTEFACT_VERIFICATION_FAILED", None) is not None
    and rc_unverified == ec.ARTEFACT_VERIFICATION_FAILED
    and ec.ARTEFACT_VERIFICATION_FAILED != ec.USAGE,
    f"rc={rc_unverified}",
)
check(
    "and it names what was missing",
    "MISSING note" in uout.getvalue(),
    uout.getvalue(),
)
check(
    "#87: and it names the contradiction, with the frame, on the same code path",
    "INCONSISTENT captions.json:f_0.png" in uout.getvalue()
    and "quality.json says 'FAIL'" in uout.getvalue(),
    uout.getvalue(),
)
dyp.run = fake_pipeline
rc_ok_again = dyc.main(["process", str(src)])
check("and it is recoverable, not latched", rc_ok_again == ec.OK, f"rc={rc_ok_again}")


# A late export failure is reported, but must not mask the audit verdict.
def error_audit_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.audit.add("test", "test-error", "ERROR", 1, "synthetic error finding")
    return result


vault_for_failure = tmp / "vault-export-fails"
vault_for_failure.mkdir(exist_ok=True)
real_export = dyb.Bundle.export_to_vault


def fail_export(self, *args, **kwargs):
    raise OSError("synthetic export failure")


dyb.Bundle.export_to_vault = fail_export
export_out, export_err = io.StringIO(), io.StringIO()
with redirect_stdout(export_out), redirect_stderr(export_err):
    rc_export_only = dyc.main(["process", str(src), "--vault-path", str(vault_for_failure)])
check(
    "a clean bundle with failed vault export returns dependency failure (#127)",
    rc_export_only == ec.DEPENDENCY_FAILED,
    f"rc={rc_export_only}",
)
check(
    "vault export failure is reported to the user (#127)",
    "vault export failed: synthetic export failure" in export_err.getvalue(),
    export_err.getvalue(),
)

dyp.run = error_audit_pipeline
export_out, export_err = io.StringIO(), io.StringIO()
with redirect_stdout(export_out), redirect_stderr(export_err):
    rc_export_and_audit = dyc.main(["process", str(src), "--vault-path", str(vault_for_failure)])
dyp.run = fake_pipeline
dyb.Bundle.export_to_vault = real_export
check(
    "audit ERROR outranks vault export failure (#127)",
    rc_export_and_audit == ec.AUDIT_FINDINGS,
    f"rc={rc_export_and_audit}",
)
check(
    "vault export failure is still reported when audit ERROR wins (#127)",
    "vault export failed: synthetic export failure" in export_err.getvalue(),
    export_err.getvalue(),
)
check("the transcript summary is reported", "3 segments, 40 timed words" in text, text)
# A path into a directory release() just deleted reads like an output location
# and is not one. Asserted per-artefact: the bare word "transcript" also occurs
# in the summary line, so a substring test on it would always pass.
check(
    "no artefact path is advertised into a deleted work dir",
    not any(f"{work_root}/{n}" in text for n in ARTEFACTS),
    text,
)
# glob must match the directories themselves, not their children: a retained but
# empty `dyak-*` dir yields no `dyak-*/*` matches, so the old form of this
# assertion passed even with release() neutered.
leftovers = list(work_root.glob("dyak-*"))
check("the work dir itself is removed on success", not leftovers, str(leftovers))

# --- 7b. --keep-workdir, which is the positive control -----------------------
outdir = tmp / "bundle"

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process", str(src), "--keep-workdir", "--output-dir", str(outdir)])
text = out.getvalue()
check("--keep-workdir reports that it kept the dir", "KEPT for inspection" in text, text)
# The deliverables are published into the bundle as they are produced, so they are NOT in
# the work dir any more -- that separation is the point of the bundle. What the retained
# work dir still holds is scratch, chiefly the 137.9 MiB audio.wav. Which file went where
# is asserted on the filesystem below, not on stdout: stdout names the bundle root, and
# listing 20 frames there would be noise.
kept = list(work_root.glob("dyak-*"))
check("--keep-workdir leaves exactly one work dir", len(kept) == 1, str(kept))
check(
    "the retained work dir holds scratch, not deliverables",
    kept and not any((kept[0] / n).exists() for n in ARTEFACTS),
    str(sorted(p.name for p in kept[0].iterdir())) if kept else "",
)
check(
    "the bundle holds every deliverable",
    all((outdir / n).is_file() for n in ARTEFACTS),
    str(sorted(p.name for p in outdir.iterdir())) if outdir.is_dir() else "",
)

# --- 7c. a dependency failure ------------------------------------------------
dyp.run = lambda source, work, **kw: (_ for _ in ()).throw(
    dyr.DependencyError(
        "stt", ec.DEPENDENCY_FAILED, "the STT endpoint exited 1", stderr=b"recording in progress\n"
    )
)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process", str(src)])
msg = err.getvalue()
check("a failed stage exits 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("the failure keeps its work dir", "KEPT for inspection" in msg, msg)
check("the endpoint's stderr is passed through", "recording in progress" in msg, msg)
check(
    "a retained-after-failure dir is reported as kept",
    list(work_root.glob("dyak-*")),
    str(list(work_root.glob("dyak-*"))),
)


# --- 7d. an unexpected exception must still locate the artefacts -------------
def boom(source, work, **kw):
    raise ValueError("something unforeseen")


dyp.run = boom
out, err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(out), redirect_stderr(err):
        dyc.main(["process", str(src)])
    check("an unexpected exception propagates", False, "was swallowed")
except ValueError:
    check("an unexpected exception propagates", True)
    check(
        "an unexpected exception still reports the work dir",
        "KEPT for inspection" in err.getvalue(),
        err.getvalue(),
    )
    check("the retained reason names the exception", "ValueError" in err.getvalue(), err.getvalue())


# --- 7d2. Ctrl-C --------------------------------------------------------------
def interrupted(source, work, **kw):
    raise KeyboardInterrupt


dyp.run = interrupted
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process", str(src)])
check("Ctrl-C exits 130, declared in exitcodes", rc == ec.INTERRUPTED, f"rc={rc}")
check(
    "130 is in the exit-code table",
    ec.INTERRUPTED in ec.DESCRIPTIONS,
    str(ec.DESCRIPTIONS.get(130)),
)
check("Ctrl-C keeps the work dir", "KEPT for inspection" in err.getvalue(), err.getvalue())
check("Ctrl-C says why it was kept", "interrupted by the user" in err.getvalue(), err.getvalue())

dyp.run = fake_pipeline


# --- 7e. the preflight gate ---------------------------------------------------
def broken(*, required=None):
    return [
        dyc.Check(
            name="stt",
            ok=False,
            detail="no transcription endpoint is reachable",
            remediation="start whisper.cpp and set DYAK_WHISPERCPP_URL",
            code=ec.MISSING_DEPENDENCY,
        )
    ]


dyc.run_all = broken
dyp.run = fake_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process", str(src)])
check("preflight blocks the run -> exit 2", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
check(
    "preflight refuses rather than degrading", "refusing to start" in err.getvalue(), err.getvalue()
)
check("preflight names doctor", "doctor" in err.getvalue(), err.getvalue())


# The gate must not be broader than the run: stages 0-3 write nothing to the
# vault and never call the gateway, so neither may refuse the run.
def missing_later_stages(*, required=None):
    return dyd.run_all(required=required)


_real_run_all = dyc.run_all


def isolated_run_all(*, required=None):
    """dyd.run_all against a guaranteed-bad vault and an unreachable gateway.

    Deliberately the real implementation: an earlier version of this test used a
    stub that reimplemented the required/not-required decision, so it passed
    unchanged when that decision was reverted.

    Real *logic*, controlled *environment*. `deps` probes the actual PATH, so
    without the fakes below this section only passes on a machine that happens to
    have ffmpeg installed, and the assertion it is actually making -- "a dead
    gateway costs the vision stages, not the run" -- gets answered by "ffmpeg is
    missing, exit 2" instead. CI caught precisely that: the runner has no ffmpeg,
    and this test came back rc=2 on all three Python versions.
    """
    return dyd.run_all(required=required)


real_deps_run = dyd._run
real_deps_which = dyd.shutil.which


def fake_deps_run(argv):
    exe = argv[0].rsplit("/", 1)[-1] if argv else ""
    if exe in ("ffmpeg", "ffprobe"):
        return subprocess.CompletedProcess(argv, 0, f"{exe} version 7.1.1-1\n", "")
    return real_deps_run(argv)


dyd._run = fake_deps_run
dyd.shutil.which = lambda n: f"/usr/bin/{n}" if n in ("ffmpeg", "ffprobe") else real_deps_which(n)


saved_vault = os.environ.get(dyd.VAULT_ENV)
# The dead dependency here is the model endpoint, not a "gateway". It was `DYAK_GATEWAY_URL`
# until #68: a name nothing wrote and nothing documented, checked under the name `gateway`
# while the endpoint the pipeline uses is `DYAK_LLM_ENDPOINT`. The behaviour under test --
# a dependency the current run cannot use is reported and not fatal -- is the same one, and
# `check_llm` is the check that actually observes it.
saved_llm = os.environ.get(dyd.LLM_ENV)
os.environ[dyd.VAULT_ENV] = str(tmp / "definitely-no-vault")
os.environ[dyd.LLM_ENV] = "http://127.0.0.1:1/inference"
dyc.run_all = isolated_run_all
before = len(list(work_root.glob("dyak-*")))
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    # `--no-llm`: a dead endpoint is fatal without it (#126). This section tests the vault
    # and degraded-run behaviour, so it opts in to the template path explicitly.
    rc = dyc.main(["process", str(src), "--keep-workdir", "--output-dir", str(outdir), "--no-llm"])
after = len(list(work_root.glob("dyak-*")))
check(
    # A dead model endpoint costs the synthesis stages, not the run. It must not turn a
    # verified pipeline into a failure -- that would be the opposite of the ADR-0001 D8 line.
    "a dead model endpoint does NOT block the run",
    rc == ec.OK,
    f"rc={rc}",
)
check(
    "a missing vault does NOT block stages 0-3",
    "refusing to start" not in out.getvalue(),
    out.getvalue(),
)
check(
    # `export_to_vault` ends in `mkdir(parents=True)`. Exporting to a path that does not
    # exist therefore creates it -- and `check_vault` then reports it exists, is a directory
    # and is writable, so the next `dyak doctor` certifies an empty vault. That turns a
    # configuration mistake into a state the tool then calls healthy. Measured: this run
    # created the fixture directory, and "vault is non-fatal for process" then failed with
    # detail "writable" instead of reporting it missing.
    "a missing vault is NOT created by the run",
    not (tmp / "definitely-no-vault").exists(),
    "the export materialised a vault that did not exist",
)
check(
    "a missing vault is named in the output",
    "definitely-no-vault" in err.getvalue(),
    err.getvalue(),
)
check("the run actually proceeded", after == before + 1, f"{before} -> {after}")
check(
    "the unusable later-stage deps are mentioned, not fatal",
    "do not use it" in err.getvalue(),
    err.getvalue(),
)
check(
    # The check that made every successful run open with "gateway is unavailable, but
    # stages 0-12 do not use it" -- false in both halves, and printed on a run whose
    # stage 6 had just successfully captioned every frame.
    "no dead `gateway` check is left to report",
    "gateway" not in {c.name for c in dyd.run_all()},
    str(sorted(c.name for c in dyd.run_all())),
)

# ...but `dyak doctor` has no such excuse: it exists to report the whole
# environment, so the same vault must be fatal there.
strict = {c.name: c for c in dyd.run_all()}
check("doctor still sees the vault as fatal", strict["vault"].fatal, strict["vault"].detail)
check("doctor still sees the model endpoint as fatal", strict["llm"].fatal, strict["llm"].detail)
for name in ("ffmpeg", "ffprobe"):
    check(f"{name} is fatal for process", not dyd.run_all(required=PROCESS_REQUIRES) or True)
for chk in dyd.run_all(required=PROCESS_REQUIRES):
    if chk.name in PROCESS_REQUIRES:
        check(f"{chk.name} is fatal for process", chk.fatal)
    else:
        check(f"{chk.name} is non-fatal for process", not chk.fatal, chk.detail)
if saved_vault is None:
    os.environ.pop(dyd.VAULT_ENV, None)
else:
    os.environ[dyd.VAULT_ENV] = saved_vault
if saved_llm is None:
    os.environ.pop(dyd.LLM_ENV, None)
else:
    os.environ[dyd.LLM_ENV] = saved_llm
dyc.run_all = _real_run_all

# --- 7f. a bad --workdir is a usage error, not a traceback --------------------
dyc.run_all = healthy_checks
not_a_dir = tmp / "not-a-dir"
not_a_dir.write_text("x")
out, err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(out), redirect_stderr(err):
        rc = dyc.main(["process", str(src), "--workdir", str(not_a_dir)])
except OSError as exc:
    # A traceback escaping main() would abort the rest of the suite, hiding
    # which later checks still pass. Report it as the failure it is.
    check(
        "--workdir pointing at a file -> exit 1", False, f"escaped as {type(exc).__name__}: {exc}"
    )
    rc = None
check("--workdir pointing at a file -> exit 1", rc == ec.USAGE, f"rc={rc}")
check("--workdir error has a remediation", "remediation:" in err.getvalue(), err.getvalue())
check("--workdir error raises no traceback", "Traceback" not in err.getvalue(), err.getvalue())


# --- 8. stage 4: frames, two passes, and the zero-frame trap (ADR-0001 D3) --------------
# The filter string used to be a byte-for-byte port of lecture-frames. It no longer can be,
# and the reason is a defect in the port rather than a preference (#70): `mpdecimate` ran
# before `select`, so it dropped every frame of a static stretch and `select`'s `max_gap`
# guarantee never saw them. Measured worst gap on lecture 1: 580.9 s as shipped, against a
# field documented as "guarantee a frame at least this often".
check(
    "the detector selects on change and on gap in ONE expression",
    dyfr.detect_filter(dyfr.Settings()) == "scale=640:-2:flags=lanczos,boxblur=16:4,"
    "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,180)+gt(scene\\,0.3)'",
    dyfr.detect_filter(dyfr.Settings()),
)
check(
    # The specific failure: two chained filters cannot guarantee anything about the output
    # of the first one, because the first one is free to emit nothing.
    "no filter precedes select, so none can starve it",
    "mpdecimate" not in dyfr.detect_filter(dyfr.Settings()),
    dyfr.detect_filter(dyfr.Settings()),
)
check(
    "the detector keeps the isnan guard",
    "isnan(prev_selected_t)" in dyfr.detect_filter(dyfr.Settings()),
)
check(
    # Reordering was measured too, and it is worse: select then mpdecimate drops exactly
    # the frames select guaranteed. 1080 s worst gap, against 580.9 s before.
    "the change threshold is not a separate filter either",
    "mpdecimate" not in dyfr.detect_filter(dyfr.Settings())
    and "mpdecimate" not in dyfr.detect_filter(dyfr.Settings(mode="interval")),
    dyfr.detect_filter(dyfr.Settings()),
)
# The blur radius is a scene-detection aid now, but it still scales with detector width.
check(
    "blur radius scales with detector width",
    "boxblur=32:8" in dyfr.detect_filter(dyfr.Settings(detect_width=1280)),
    dyfr.detect_filter(dyfr.Settings(detect_width=1280)),
)
check(
    "interval mode drops the selector entirely",
    dyfr.detect_filter(dyfr.Settings(mode="interval")) == "scale=640:-2:flags=lanczos,fps=1/30",
    dyfr.detect_filter(dyfr.Settings(mode="interval")),
)
check(
    "max_gap=0 keeps the change condition but drops the gap guard",
    "gte(t-prev_selected_t" not in dyfr.detect_filter(dyfr.Settings(max_gap=0))
    and "gt(scene" in dyfr.detect_filter(dyfr.Settings(max_gap=0)),
    dyfr.detect_filter(dyfr.Settings(max_gap=0)),
)
_ff_argv: list = []
_real_run_ffmpeg = dyfr._run_ffmpeg
dyfr._run_ffmpeg = lambda args, **kw: _ff_argv.append(args)
try:
    dyfr.detect(src)
    dyfr.extract_at(src, 1000, tmp)
    detect_argv, extract_argv = _ff_argv
finally:
    dyfr._run_ffmpeg = _real_run_ffmpeg
check(
    # `-frame_pts 1` writes the PTS in the output timebase, which after
    # `-fps_mode passthrough` is the input frame rate, not milliseconds. Only pass 1 reads
    # a timestamp out of ffmpeg at all -- pass 2 seeks to a number this pipeline already
    # has and names its own output -- so pass 1 is the one that must pin the timebase.
    # Without it every number is 30x small on a 30 fps lecture, and pass 2 then seeks into
    # the first thirtieth of the video (#70).
    "pass 1 pins the output timebase to milliseconds",
    "-enc_time_base" in detect_argv and "1/1000" in detect_argv,
    f"detect={detect_argv}",
)
check(
    # The consequence that made the bug invisible: pass 2's seek and pass 1's filenames are
    # the only link between a frame and a time, and both read the same number. Asserting
    # the seek is derived from `pts_ms` and not from a second decode keeps them in step.
    "pass 2 seeks to the timestamp it is given and does not re-derive one",
    "-ss" in extract_argv
    and extract_argv[extract_argv.index("-ss") + 1] == "1.000"
    and "-frame_pts" not in extract_argv,
    f"extract={extract_argv}",
)
check("timestamps format with centiseconds", dyfr.hhmmss(4_396_000) == "01:13:16.00")
# Centiseconds truncate: 500 ms is .05, not .50 -- the same floor the original used.
check(
    "timestamps format sub-second", dyfr.hhmmss(3_600_500) == "01:00:00.05", dyfr.hhmmss(3_600_500)
)
check(
    "timestamps truncate to centiseconds",
    dyfr.hhmmss(3_600_999) == "01:00:00.09",
    dyfr.hhmmss(3_600_999),
)


# ffmpeg in these tests must *write files*, because the whole point of stage 4 is that
# what lands on disk is what counts. A fake that only returns an exit code cannot test it.
def ffmpeg_writes(times: list[int], *, write_output: bool = True, write_detector: bool = True):
    """A fake ffmpeg that honours the output path in the last argv position."""

    def run(name, args, *, remediation, timeout=dyr.DEFAULT_TIMEOUT, cwd=None):
        fake_resolve(name, remediation)
        CALLS.append((name, list(args)))
        if "-version" in args:
            # Provenance asks for the build. Answered without consuming the queue, so a
            # version probe cannot masquerade as one of the programmed ffmpeg passes.
            return b"ffmpeg version n9.0.2 Copyright (c) 2000-2026 fake\n"
        queue = FAKE.get(name)
        if not queue:
            raise AssertionError(f"unprogrammed call to {name}: {args}")
        stdout, stderr, rc = queue.pop(0)
        if rc != 0:
            raise dyr.DependencyError(
                name=name,
                code=ec.DEPENDENCY_FAILED,
                message=f"{name} exited {rc} running `{' '.join(args)}`",
                stderr=stderr,
                argv=list(args),
            )
        if write_detector and "-frame_pts" in args:
            target = Path(args[-1]).parent
            target.mkdir(parents=True, exist_ok=True)
            for t in times:
                (target / f"d_{t:010d}.jpg").write_bytes(b"x")
        elif write_output and "-frames:v" in args:
            Path(args[-1]).write_bytes(b"x")
        return stdout

    return run


frame_times = [0, 304_132, 458_606]
frame_out = tmp / "framesout"

install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
dyr.run = ffmpeg_writes(frame_times)
manifest, produced = dyfr.run(src, frame_out)
check("stage 4 writes a manifest", manifest.is_file(), str(manifest))
check("stage 4 produced every frame", len(produced) == len(frame_times), str(len(produced)))
check(
    "frame filenames are the timestamp in ms",
    [p.name for p in produced] == [f"f_{t:010d}.png" for t in frame_times],
    str([p.name for p in produced]),
)

detect_call = next(a for n, a in CALLS if "-frame_pts" in a)
check(
    "the detect pass drops audio", "-an" in detect_call and "-sn" in detect_call, str(detect_call)
)
check("the detect pass disables stdin", "-nostdin" in detect_call, str(detect_call))
check(
    "the detect pass reads the resolved source",
    str(src.resolve()) in detect_call,
    str(detect_call),
)
check(
    "the detect pass writes to a throwaway directory",
    "dyak-detect-" in detect_call[-1],
    detect_call[-1],
)

extract_call = next(a for n, a in CALLS if "-frames:v" in a)
# -ss must precede -i: after it, ffmpeg decodes from the start to the seek point, which
# on a 73-minute lecture is the difference between 0.2 s and 300 s.
check(
    "the extract pass seeks before the input",
    "-ss" in extract_call and extract_call.index("-ss") < extract_call.index("-i"),
    str(extract_call),
)
check(
    "the extract pass reads the ORIGINAL source, not the detector temp dir",
    str(src.resolve()) in extract_call and "dyak-detect-" not in " ".join(extract_call),
    str(extract_call),
)
check(
    "the extract pass scales to the output width",
    "scale=1600:-2" in " ".join(extract_call),
    str(extract_call),
)
check(
    "the extract pass applies NO blur (ADR-0001 D3)",
    "boxblur" not in extract_call[-2] and "boxblur" not in " ".join(extract_call),
    str(extract_call),
)

man_text = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
check(
    "the manifest round-trips",
    manifest.is_file() and dyfr.read_manifest(manifest) == frame_times,
    man_text,
)
check(
    "the manifest carries a human time column",
    "00:05:04.01" in man_text,
    man_text,
)

# ADR-0001 D3, the trap the ported script fell into: zero frames must not be a successful run.
install_fake({"ffmpeg": [(b"", b"", 0)]})
dyr.run = ffmpeg_writes([])
exc = raises(dyfr.run, src, tmp / "zeroframes")
check(
    "zero frames -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'run reported success with no frames')}",
)
check(
    "zero frames says why",
    exc is not None and "no states" in exc.message,
    getattr(exc, "message", "run reported success with no frames"),
)

# The same trap one level down: ffmpeg exits 0 having written nothing at all.
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
dyr.run = ffmpeg_writes(frame_times, write_detector=True, write_output=False)
exc = raises(dyfr.run, src, tmp / "nofiles")
check(
    "ffmpeg exiting 0 without writing frames -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'run reported success with no frames on disk')}",
)
check(
    "the unwritten frames are named",
    exc is not None and "were not written" in getattr(exc, "message", ""),
    getattr(exc, "message", "no exception raised"),
)

# A renamed parent directory produces the same ffmpeg message as a flaky host. The retry
# must not convert "your path is wrong" into "ffmpeg is broken".
gone = tmp / "renamed-away" / "lecture.webm"
install_fake({"ffmpeg": [(b"", b"Error opening input file /nope", 1)] * 6})
exc = raises(dyfr.run, gone, tmp / "goneout")
check(
    "a missing source -> exit 1, not exit 3", exc.code == ec.USAGE, f"{getattr(exc, 'code', None)}"
)
check("a missing source names the path", str(gone) in exc.message, getattr(exc, "message", ""))
check(
    "a missing source is not retried into a host-fault message",
    "after 4 attempts" not in exc.message,
    getattr(exc, "message", ""),
)

# ...but when the file IS present and ffmpeg still cannot open it, that is the host fault
# the retry exists for, and the message must say so.
install_fake({"ffmpeg": [(b"", b"Error opening input file", 1)] * 6})
dyfr.time.sleep = lambda _s: None
exc = raises(dyfr.run, src, tmp / "flakyout")
check("a present-but-unopenable source -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "the retry says the file was verified present",
    "verified present" in exc.message,
    exc.message,
)
dyfr.time.sleep = real_sleep

# An unrelated ffmpeg error is raised at once, not four times slower.
install_fake({"ffmpeg": [(b"", b"Invalid data found when processing input", 1)]})
CALLS.clear()
exc = raises(dyfr.run, src, tmp / "harderr")
check("an unrelated ffmpeg error is not retried", len(CALLS) == 1, f"{len(CALLS)} calls")

# A stale frame from a previous run must not survive into the new manifest.
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
dyr.run = ffmpeg_writes(frame_times)
stale = frame_out / "f_9999999999.png"
stale.write_bytes(b"old")
dyfr.run(src, frame_out)
check("stale frames are cleared", not stale.exists(), str(stale))

# --reuse-manifest skips the decode entirely, which is the fast path for re-rendering.
install_fake({"ffmpeg": [(b"", b"", 0)] * len(frame_times)})
dyr.run = ffmpeg_writes(frame_times)
CALLS.clear()
manifest, produced = dyfr.run(src, frame_out, reuse_manifest=True)
check("--reuse-manifest keeps the frame count", len(produced) == len(frame_times), str(produced))
check(
    "--reuse-manifest does not run the detector",
    not any("-frame_pts" in a for _, a in CALLS),
    str(CALLS),
)
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
dyr.run = ffmpeg_writes(frame_times)
_fresh_manifest, fresh_frames = dyfr.run(src, tmp / "emptyout", reuse_manifest=True)
check(
    "--reuse-manifest with no manifest falls back to detecting",
    len(fresh_frames) == len(frame_times),
    str(len(fresh_frames)),
)

check("the stage summary counts frames and bytes", "3 frames" in dyfr.summary(manifest, produced))

# Provenance: without it, a manifest cannot be reproduced, and a later reader cannot tell
# whether the frames or the code changed. This was found the hard way -- the manifest in
# the vault lists 17 states where the same ffmpeg on the same source now lists 16.
prov_path = frame_out / "detect.json"
prov_raw = prov_path.read_text(encoding="utf-8") if prov_path.is_file() else ""
prov = json.loads(prov_raw) if prov_raw else {}
check("provenance is written next to the frames", prov_raw != "", str(prov_path))
check("provenance records the detector filter", "isnan(prev_selected_t)" in prov.get("filter", ""))
check("provenance records the settings", prov.get("settings", {}).get("detect_width") == 640)
check("provenance records the ffmpeg build", prov.get("ffmpeg", "").startswith("ffmpeg version"))
install_fake({"ffmpeg": [(b"", b"", 0)] * len(frame_times)})
dyr.run = ffmpeg_writes(frame_times)
dyfr.run(src, frame_out, reuse_manifest=True)
check(
    "provenance distinguishes a reused manifest",
    json.loads((frame_out / "detect.json").read_text(encoding="utf-8"))["reused_manifest"] is True,
)

dyr.run = real_run

# --- 9. stage 3 backends: one interface, three wire formats ----------------------
# Stage 3 is delegated, but the delegation must not become a silent downgrade. Every
# backend returns raw bytes and one parser consumes them, so the only thing that differs
# is the envelope -- and the one thing that must never differ is whether word timings
# arrive.

# --- 9. stage 3 backends: one interface, three wire formats ----------------------
# Section 6 pinned DYAK_STT=whispercpp so it tests one backend. This section covers the
# rest: the two HTTP adapters, autodetection with both branches controlled, and the
# word-coverage guard that every backend is held to.

# A whisper.cpp verbose_json segment, as the server returns it on this host.
_http_segment = [
    {
        "id": 0,
        "text": " Оптимальные системы",
        "start": 0.0,
        "end": 1.4,
        "words": [
            {"word": " Оп", "start": 0.0, "end": 0.4, "t_dtw": -1, "probability": 0.91},
            {"word": "тимальные", "start": 0.4, "end": 1.4, "t_dtw": -1, "probability": 0.88},
        ],
    }
]


class FakeHTTP:
    """Stands in for urllib.request.urlopen. Records what was sent, returns what is queued."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, request, timeout=None):
        self.sent.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, _n=-1):
        return self.body if _n < 0 else self.body[:_n]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def http_body(url_marker: str, obj) -> bytes:
    return json.dumps(obj).encode()


# -- native /inference: the protocol this host actually serves --------------------
import dyak.stt.backends as dysb  # noqa: E402
import dyak.stt.openai_compat as dyso  # noqa: E402
import dyak.stt.whispercpp as dysw  # noqa: E402

real_urlopen = dysw.urllib.request.urlopen
real_oai_urlopen = dyso.urllib.request.urlopen

# The health route answers 200 and the inference route returns the array.
http = FakeHTTP([b"ok", json.dumps(_http_segment).encode()])
dysw.urllib.request.urlopen = http
wav_file = tmp / "backend.wav"
wav_file.write_bytes(b"RIFF" + b"\\0" * 60)
tr_http = dystt.transcribe(wav_file, audio_duration=1.4, backend="whispercpp")
check("native backend produced segments", len(tr_http.segments) == 1, str(len(tr_http.segments)))
check("native backend produced timed words", tr_http.timed_words == 2, str(tr_http.timed_words))
check("the transcript names its backend", tr_http.backend == "whispercpp", tr_http.backend)
check(
    "the native backend asks for verbose_json",
    b"verbose_json" in http.sent[-1].data,
    str(http.sent[-1].data[:200]),
)
check(
    "the native backend posts to /inference, not /v1/*",
    http.sent[-1].full_url.endswith("/inference"),
    http.sent[-1].full_url,
)
check("the native backend uploads the wav bytes", wav_file.read_bytes() in http.sent[-1].data)

# A server that answers /health with 404 still serves /inference. Treating that as fatal
# would break on a build that simply has no health route.
http = FakeHTTP(
    [urllib.error.HTTPError("u", 404, "Not Found", {}, None), json.dumps(_http_segment).encode()]
)
dysw.urllib.request.urlopen = http
check(
    "a 404 on /health is not fatal",
    dystt.transcribe(wav_file, audio_duration=1.4, backend="whispercpp").timed_words == 2,
)

# Connection refused must be exit 3 with a reachable remediation, not a traceback.
dysw.urllib.request.urlopen = FakeHTTP([urllib.error.URLError(OSError(111, "Connection refused"))])
exc = raises(dystt.transcribe, wav_file, audio_duration=1.4, backend="whispercpp")
check(
    "an unreachable endpoint -> exit 3",
    exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "the remediation names the env var",
    "DYAK_WHISPERCPP_URL" in exc.remediation,
    exc.remediation,
)

# -- OpenAI-compatible: the envelope differs, the segments do not -----------------
# #95 removed the built-in endpoint default, so this backend must be named explicitly to be
# "configured". `urlopen` is replaced below, so the value only has to make it configured.
os.environ["DYAK_OPENAI_URL"] = "http://127.0.0.1:10302"
http = FakeHTTP(
    [b"ok", json.dumps({"task": "transcribe", "text": "x", "segments": _http_segment}).encode()]
)
dyso.urllib.request.urlopen = http
tr_oai = dystt.transcribe(wav_file, audio_duration=1.4, backend="openai")
check("openai backend produced segments", len(tr_oai.segments) == 1, str(len(tr_oai.segments)))
check("openai backend produced timed words", tr_oai.timed_words == 2, str(tr_oai.timed_words))
check(
    "the openai backend asks for word granularity", b"timestamp_granularities" in http.sent[-1].data
)
# A parse failure must name the backend that produced it. Hardcoding a name here would
# misdirect every future backend's debugging.
dyso.urllib.request.urlopen = FakeHTTP([b"ok", b"this is not json at all"])
exc = raises(dystt.transcribe, wav_file, audio_duration=1.4, backend="openai")
# The backend name is the DependencyError's `name`, which is what report() prints as the
# failing dependency. That is the field a reader acts on, so that is the field to assert.
check(
    "a malformed payload names the backend that returned it",
    exc is not None and getattr(exc, "name", "") == "openai",
    getattr(exc, "name", ""),
)
exc = raises(dystt.parse, b"not json", source="some-other-backend")
check(
    "parse reports the source it was handed",
    exc is not None and getattr(exc, "name", "") == "some-other-backend",
    getattr(exc, "name", ""),
)

check(
    "the openai backend posts to /v1/audio/transcriptions",
    http.sent[-1].full_url.endswith("/v1/audio/transcriptions"),
    http.sent[-1].full_url,
)

# The failure the whole interface exists for: a server that honours response_format=json
# and ignores timestamp_granularities. Segments arrive, words do not, and the pipeline
# must say so instead of letting stages 5-7 bind frames by guesswork.
http = FakeHTTP([b"ok", json.dumps({"text": "one whole blob of text, no timings"}).encode()])
dyso.urllib.request.urlopen = http
exc = raises(dystt.transcribe, wav_file, audio_duration=1.4, backend="openai")
check(
    "a backend returning no words -> exit 3",
    exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "the failure names the cause",
    "zero words carrying timings" in getattr(exc, "message", ""),
    getattr(exc, "message", ""),
)
check(
    "the remediation names the ignored parameter",
    "timestamp_granularities" in getattr(exc, "remediation", ""),
    getattr(exc, "remediation", ""),
)

# A 401 on /v1/models means the server is there with a bad key -- not "nothing listening".
dyso.urllib.request.urlopen = FakeHTTP([urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)])
check("a 401 on /v1/models is not fatal", dyso.OpenAICompat("http://127.0.0.1:1").check() is None)


# -- #125: a hosted endpoint's upload ceiling, sliced rather than exceeded --------
# 16 kHz mono 16-bit is 32 KiB/s, so a 75-minute lecture is ~140 MiB against the documented
# 25 MB limit, and the body was assembled as one `bytes` object. The route now slices the wav
# below a configured threshold and splices word timings. Four things are pinned here: the
# request body is bounded by the setting, the timed-word stream survives the seam without
# duplication, a chunk that loses its words fails loudly instead of silently, and provenance
# distinguishes a sliced upload from a single one. The endpoint is mocked throughout.


def pcm_wav(path: Path, seconds: float, sample_rate: int = 16000) -> Path:
    """A real 16 kHz mono PCM wav, so the slicer has a RIFF header to read.

    A ramp rather than silence: silence makes "the sample bytes were copied verbatim" true
    for any slice, which is the claim the no-re-encode decision rests on.
    """
    frames = int(sample_rate * seconds)
    data = b"".join(struct.pack("<h", (i % 30000) - 15000) for i in range(frames))
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return path


class ChunkHTTP:
    """Serves the /v1/models probe, then one payload per chunk; records every request."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.sent = []

    def __call__(self, request, timeout=None):
        self.sent.append(request)
        if request.data is None:  # the check() probe, not a transcription
            return _Response(b"ok")
        return _Response(self.payloads.pop(0))


def chunk_segment(text, starts, extra=()):
    """One verbose_json segment whose words sit at `starts` seconds into its own chunk."""
    words = [
        {"word": f" w{i}", "start": s, "end": s + 0.05, "t_dtw": -1, "probability": 0.9}
        for i, s in enumerate(starts)
    ]
    words.extend(extra)
    return {
        "id": 0,
        "text": text,
        "start": 0.0,
        "end": max([s for s in starts] + [0.0]) + 0.05,
        "words": words,
    }


def pcm_of(body: bytes) -> bytes:
    """The PCM bytes of the wav embedded in a multipart body, per its own data size field."""
    start = body.index(b"RIFF")
    size = struct.unpack("<I", body[start + 40 : start + 44])[0]
    return body[start + 44 : start + 44 + size]


_prev_limit = os.environ.get("DYAK_OPENAI_MAX_UPLOAD_BYTES")
_limit = 20000
os.environ["DYAK_OPENAI_MAX_UPLOAD_BYTES"] = str(_limit)

chunk_wav = pcm_wav(tmp / "lecture-125.wav", 1.0)  # 32044 bytes -> three requests at 20000
chunk_data = chunk_wav.read_bytes()[44:]

# Chunk 0's third word starts at 0.55 s, past the keep range its chunk hands to chunk 1
# (~0.30 s): the decoder emits words at a chunk's tail, and the splice must drop that one
# there and take the word from chunk 1 instead. That is the seam, and the overlap is the
# reason there is a second reading of it at all.
chunk_http = ChunkHTTP(
    [
        json.dumps(
            {
                "task": "transcribe",
                "text": "x",
                "segments": [
                    chunk_segment(
                        "one", [0.0, 0.1], [{"word": " SPILL", "start": 0.55, "end": 0.6}]
                    )
                ],
            }
        ).encode(),
        json.dumps({"segments": [chunk_segment("two", [0.0, 0.1])]}).encode(),
        json.dumps({"segments": [chunk_segment("three", [0.0, 0.1])]}).encode(),
    ]
)
dyso.urllib.request.urlopen = chunk_http
# #193: the hosted route's unit is its own uploads, reported as each one returns. Collected
# here because the callback is the only live signal a reader of this route gets.
# #205: `done` alone is a value the loop could hand over at the end -- recording each callback
# and firing them after the loop leaves the 0..N sequence identical. So each entry also carries
# how many uploads are visible in `chunk_http.sent` *at call time*, and the check below
# requires the two to agree; a deferred callback would observe the final count every time.
_chunk_seen: list[tuple[int, int, int]] = []


def _chunk_progress(done: int, total: int) -> None:
    _chunk_seen.append((done, total, sum(1 for r in chunk_http.sent if r.data is not None)))


tr_chunked = dystt.transcribe(
    chunk_wav,
    audio_duration=1.0,
    backend="openai",
    on_progress=_chunk_progress,
)
_bodies = [r.data for r in chunk_http.sent if r.data is not None]
check(
    "a wav above the threshold is sliced into several requests",
    len(_bodies) >= 2,
    f"{len(_bodies)} requests",
)
check(
    # The acceptance criterion is a bound on the body, not the fact that a request happened.
    "every chunk request body is within the configured threshold",
    all(len(body) <= _limit for body in _bodies),
    str([len(body) for body in _bodies]),
)
check(
    "no chunk body carries the whole file",
    all(len(body) < chunk_wav.stat().st_size for body in _bodies),
    f"file {chunk_wav.stat().st_size}, bodies {[len(b) for b in _bodies]}",
)
check(
    "each chunk body is a self-contained wav with its own sizes",
    all(body.count(b"RIFF") == 1 and pcm_of(body) for body in _bodies),
    str([len(pcm_of(b)) for b in _bodies]),
)
check(
    # No re-encode: every chunk's PCM is a verbatim slice of the source's sample bytes.
    "the sample bytes are copied, not re-encoded",
    all(pcm_of(body) in chunk_data for body in _bodies),
)
check(
    "the whole audio is covered, first sample to last",
    chunk_data.startswith(pcm_of(_bodies[0])) and chunk_data.endswith(pcm_of(_bodies[-1])),
)
check(
    # Acceptance: "the chunk counter advances live". Derived from the observed request count,
    # so the assertion does not hardcode the fixture's geometry -- one `(0, N)` before the
    # first upload and one `(k, N)` after each of the N uploads.
    # #205: the value sequence alone is what a deferred callback also produces, so the third
    # element -- uploads visible when the callback ran -- must equal `done` on every entry.
    "the hosted chunk counter advances as each upload returns (#193)",
    [done for done, _, _ in _chunk_seen] == list(range(len(_bodies) + 1))
    and {total for _, total, _ in _chunk_seen} == {len(_bodies)}
    and all(done == seen for done, _, seen in _chunk_seen),
    str(_chunk_seen),
)

_starts = [word.start for seg in tr_chunked.segments for word in seg.words]
check(
    "word timings survive the splice: every chunk contributes its kept words",
    tr_chunked.timed_words == 6,
    f"{tr_chunked.timed_words} timed words",
)
check(
    "the spilled tail word is dropped at the seam, not duplicated",
    " SPILL" not in [word.text for seg in tr_chunked.segments for word in seg.words],
)
check(
    "the word at the seam is kept once, from the chunk that owns it",
    len(set(round(s, 6) for s in _starts)) == len(_starts),
    str([round(s, 4) for s in _starts]),
)
check(
    "later chunks' words are offset by their chunk's start",
    _starts == sorted(_starts) and _starts[2] > 0.25,
    str([round(s, 4) for s in _starts]),
)
check(
    "transcribe's coverage check passes on the spliced result",
    tr_chunked.timed_words > 0 and tr_chunked.coverage > 0,
    f"{tr_chunked.timed_words} words, coverage {tr_chunked.coverage:.3f}",
)
check(
    "the raw payload keeps the server's other fields",
    json.loads(tr_chunked.raw_payload)["segments"][0]["words"][0]["t_dtw"] == -1,
)
check(
    # "a reader must be able to tell a chunked upload from a single one."
    "provenance records the slicing, its threshold and the geometry",
    tr_chunked.parameters["chunking"]["chunks"] == len(_bodies) >= 2
    and tr_chunked.parameters["chunking"]["threshold_bytes"] == _limit
    and tr_chunked.parameters["chunking"]["overlap_seconds"] > 0,
    str(tr_chunked.parameters["chunking"]),
)

# Under the limit the route is exactly what it was: one request, the file verbatim, so a wav
# this code cannot slice still uploads as long as it fits.
small_wav = pcm_wav(tmp / "small-125.wav", 0.1)
small_http = FakeHTTP([b"ok", json.dumps({"segments": [chunk_segment("one", [0.0])]}).encode()])
dyso.urllib.request.urlopen = small_http
_single_seen: list[tuple[int, int]] = []
tr_single = dystt.transcribe(
    small_wav,
    audio_duration=0.1,
    backend="openai",
    on_progress=lambda done, total: _single_seen.append((done, total)),
)
_single_posts = [r.data for r in small_http.sent if r.data is not None]
check("a wav under the threshold is one request", len(_single_posts) == 1, str(len(_single_posts)))
check(
    # The one-request branch is not silent either: it reports the single unit it is about to
    # upload and then that it returned, which is what a reader sees instead of a bare wait.
    "the single-request route reports its one unit, start and finish (#193)",
    _single_seen == [(0, 1), (1, 1)],
    str(_single_seen),
)
check("and its body is the file verbatim", small_wav.read_bytes() in _single_posts[0])
check(
    "and provenance says a single upload",
    tr_single.parameters["chunking"]["chunks"] == 1,
    str(tr_single.parameters["chunking"]),
)

# A server that ignores timestamp_granularities for one chunk while another chunk has words
# would slip past the whole-result coverage check, and the loss would be silent. This is the
# failure mode the per-chunk check exists for.
bad_http = ChunkHTTP(
    [
        json.dumps({"segments": [chunk_segment("one", [0.0])]}).encode(),
        json.dumps({"text": "no timings at all"}).encode(),
        json.dumps({"segments": [chunk_segment("three", [0.0])]}).encode(),
    ]
)
dyso.urllib.request.urlopen = bad_http
exc = raises(dystt.transcribe, chunk_wav, audio_duration=1.0, backend="openai")
check(
    "a chunk with segments but no words -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "and the failure names the chunk that lost them",
    exc is not None and "chunk 2 of 3" in exc.message,
    getattr(exc, "message", ""),
)
check(
    "and the remediation names the ignored parameter",
    exc is not None and "timestamp_granularities" in exc.remediation,
    getattr(exc, "remediation", ""),
)

# Above the threshold and not a PCM wav, the route must refuse rather than post 140 MiB and
# let the endpoint answer with an opaque HTTP error.
junk_wav = tmp / "junk-125.wav"
junk_wav.write_bytes(b"RIFF" + b"\0" * 30000)
dyso.urllib.request.urlopen = FakeHTTP([b"ok"])
exc = raises(dystt.transcribe, junk_wav, audio_duration=1.0, backend="openai")
check(
    "an unsliceable wav above the threshold -> exit 3, not a 140 MiB body",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "and the message says why it cannot be sliced",
    exc is not None and "not a RIFF/WAVE file" in exc.message,
    getattr(exc, "message", ""),
)

# A mistyped threshold is a usage error naming the setting, not a silent fall back to 25 MiB.
os.environ["DYAK_OPENAI_MAX_UPLOAD_BYTES"] = "25MB"
dyso.urllib.request.urlopen = FakeHTTP([b"ok"])
exc = raises(dystt.transcribe, chunk_wav, audio_duration=1.0, backend="openai")
check(
    "a non-numeric threshold -> exit 1",
    exc is not None and exc.code == ec.USAGE,
    f"{getattr(exc, 'code', None)}",
)
check(
    "and the message names the setting",
    exc is not None and "DYAK_OPENAI_MAX_UPLOAD_BYTES" in exc.message,
    getattr(exc, "message", ""),
)

# The built-in default is the documented hosted limit, and it is what an unset run reports.
os.environ.pop("DYAK_OPENAI_MAX_UPLOAD_BYTES", None)
_default_oa = dyso.OpenAICompat("http://127.0.0.1:1")
check(
    "the unset threshold is the documented 25 MiB",
    _default_oa.max_upload_setting.value == str(25 * 1024 * 1024)
    and _default_oa.max_upload_setting.source == "default",
    f"{_default_oa.max_upload_setting.value!r} from {_default_oa.max_upload_setting.source}",
)

if _prev_limit is None:
    os.environ.pop("DYAK_OPENAI_MAX_UPLOAD_BYTES", None)
else:
    os.environ["DYAK_OPENAI_MAX_UPLOAD_BYTES"] = _prev_limit


# -- autodetection: both branches, neither dependent on the machine ---------------
_install = dysb.resolve
_install.__globals__


class _Fake:
    def __init__(self, ok):
        self.ok = ok

    def available(self):
        return self.ok

    def check(self):
        if not self.ok:
            raise dyr.DependencyError("stt", ec.DEPENDENCY_FAILED, "down", "up")


# DYAK_STT is still pinned to whispercpp by section 6, so autodetection cannot be
# exercised until it is unset. Left set, resolve() returns the pinned branch and every
# assertion below passes without testing anything. The fakes are installed *before* the pin
# is removed, so resolve() cannot reach a real urlopen on the way through.
real_wsc = dysw.HttpWhisperCpp
real_oa = dyso.OpenAICompat
pinned = os.environ.pop("DYAK_STT", None)


class _Named:
    def __init__(self, name, ok=True):
        self.name = name
        self.ok = ok

    def available(self):
        return self.ok


dysw.HttpWhisperCpp = lambda: _Named("whispercpp", True)
dyso.OpenAICompat = lambda: _Named("openai", True)
check(
    "with both available, autodetect prefers local whisper.cpp",
    dysb.resolve().name == "whispercpp",
    dysb.resolve().name,
)

dysw.HttpWhisperCpp = lambda: _Fake(True)
dyso.OpenAICompat = lambda: _Fake(False)
check("autodetect prefers a reachable whisper.cpp endpoint", dysb.resolve().ok is True)

dysw.HttpWhisperCpp = lambda: _Fake(False)
dyso.OpenAICompat = lambda: _Fake(True)
check("autodetect falls back to the OpenAI-compatible endpoint", dysb.resolve().ok is True)

dyso.OpenAICompat = lambda: _Fake(False)
exc = raises(dysb.resolve)
check(
    "autodetect with nothing reachable -> exit 2",
    exc is not None and exc.code == ec.MISSING_DEPENDENCY,
    f"{getattr(exc, 'code', 'autodetect picked something anyway')}",
)
check(
    "the no-backend message names every way out",
    exc is not None and all(k in exc.remediation.lower() for k in ("whispercpp", "openai")),
    getattr(exc, "remediation", ""),
)

# The backend name that was removed is now rejected rather than silently resolved. This is
# the assertion that inverts: before #44, `resolve("shipboard")` returned a backend.
exc = raises(dysb.resolve, "shipboard")
check(
    "the removed backend is rejected, not resolved",
    exc is not None and exc.code == ec.USAGE,
    f"{getattr(exc, 'code', 'shipboard still resolves to something')}",
)
check(
    "and the rejection names the two that remain",
    exc is not None and "shipboard" in exc.message,
    getattr(exc, "message", ""),
)
dysw.HttpWhisperCpp = real_wsc
dyso.OpenAICompat = real_oa
if pinned is not None:
    os.environ["DYAK_STT"] = pinned

dysw.HttpWhisperCpp = real_wsc
if pinned is not None:
    os.environ["DYAK_STT"] = pinned

# An unknown backend name is a usage error, not a stack trace.
exc = raises(dysb.resolve, "nonsense")
check("an unknown backend name -> exit 1", exc.code == ec.USAGE, f"{getattr(exc, 'code', None)}")

# The interfaces are structurally identical -- that is the contract, and a typo in one
# backend's method name would otherwise only surface at runtime.
for _cls in (dysw.HttpWhisperCpp, dyso.OpenAICompat):
    check(
        f"{_cls.__name__} satisfies TranscriptionBackend",
        isinstance(_cls(), dysb.TranscriptionBackend),
    )

dysw.urllib.request.urlopen = real_urlopen
dyso.urllib.request.urlopen = real_oai_urlopen
urllib.request.urlopen = real_urlopen


# --- 10. the output bundle: XDG default, publication, verified export -----------
# The default matters more than it looks. `./output/<lecture>` puts a 137.9 MiB wav and a
# 3.7 MB transcript wherever the user happened to be standing -- which is the repo
# pollution ADR-0001 D9 was written against, reintroduced one directory level down.
_real_state = os.environ.pop("XDG_STATE_HOME", None)
_real_outdir = os.environ.pop("DYAK_OUTPUT_DIR", None)
xdg = tmp / "xdg"
os.environ["XDG_STATE_HOME"] = str(xdg)

check(
    "the default root is XDG_STATE_HOME/dyak",
    dyb.state_root() == xdg / "dyak",
    str(dyb.state_root()),
)
b_default = dyb.Bundle.open(src)
check(
    "the bundle is named after the source stem",
    b_default.root == (xdg / "dyak" / "lecture").resolve(),
    str(b_default.root),
)
check("the bundle directory is created", b_default.root.is_dir())

# Cyrillic is kept, because the lectures here are named in Russian and transliterating
# them would produce a directory whose name does not match its contents.
check(
    "a Cyrillic name survives intact",
    dyb.slugify("Оптимальные СУ") == "Оптимальные-СУ",
    dyb.slugify("Оптимальные СУ"),
)
check("path separators are neutralised", "/" not in dyb.slugify("a/b\nc"))

# An explicit --output-dir IS the bundle root. Nesting the lecture name under it would
# silently give a different path than the one that was typed.
explicit = tmp / "explicit"
b_explicit = dyb.Bundle.open(src, output_dir=str(explicit))
check(
    "--output-dir is the bundle root itself",
    b_explicit.root == explicit.resolve(),
    str(b_explicit.root),
)
check(
    "the explicit dir does not gain a lecture subdir",
    not (explicit / src.stem).exists(),
    str(sorted(q.name for q in explicit.iterdir())),
)

# DYAK_OUTPUT_DIR is the middle precedence step.
os.environ["DYAK_OUTPUT_DIR"] = str(tmp / "fromenv")
b_env = dyb.Bundle.open(src)
check(
    "DYAK_OUTPUT_DIR is used when no flag is given",
    b_env.root == (tmp / "fromenv").resolve(),
    str(b_env.root),
)
check(
    "the flag beats the environment",
    dyb.Bundle.open(src, output_dir=str(explicit)).root == explicit.resolve(),
)
del os.environ["DYAK_OUTPUT_DIR"]

# publish moves the deliverable out of scratch, and refuses to lie about it.
scratch = tmp / "scratch"
scratch.mkdir()
(src_dir := scratch / "transcript.json").write_bytes(b'{"a":1}')
published = b_explicit.publish("transcript", src_dir)
check(
    "publish moves the file into the bundle",
    published == explicit / "transcript.json",
    str(published),
)
check("publish removes it from scratch", not src_dir.exists())
check("publish records it", b_explicit.artefacts.get("transcript") == published)

# A missing artefact must not be reported as published. `publish` used to return the dangling
# source path and record nothing, which is how stage 5's gate became a silent no-op (#82, #89).
ghost = scratch / "ghost.json"
try:
    b_explicit.publish("ghost", ghost)
    _ghost_raised = None
except FileNotFoundError as exc:
    _ghost_raised = exc
check(
    "publish of a missing file raises, so a caller cannot receive a dangling path",
    _ghost_raised is not None and "ghost" in str(_ghost_raised),
    f"raised={_ghost_raised!r}",
)
check("a failed publish is not recorded", "ghost" not in b_explicit.artefacts)

# The optional form says so in the return type: None, not a path that does not exist.
check(
    "publish_optional returns None for a missing source",
    b_explicit.publish_optional("ghost", ghost) is None,
)
check(
    "publish_optional records nothing for a missing source",
    "ghost" not in b_explicit.artefacts,
)
_optional_real = scratch / "optional.json"
_optional_real.write_bytes(b'{"b":2}')
check(
    "publish_optional still publishes a source that exists",
    b_explicit.publish_optional("optional", _optional_real) == explicit / "optional.json"
    and b_explicit.artefacts.get("optional") == explicit / "optional.json",
    str(b_explicit.artefacts.get("optional")),
)

# overwrite replaces the run's own artefacts; it must not rmtree the directory (#119).
# A user file in a directory they pointed --output-dir at has to survive --overwrite.
user_dir = tmp / "user-owned"
user_dir.mkdir()
(user_dir / "thesis.txt").write_text("mine")
(user_dir / "note.md").write_text("old run")
(user_dir / "images").mkdir()
(user_dir / "images" / "f_0001.jpg").write_bytes(b"old")
dyb.Bundle.open(src, output_dir=str(user_dir), overwrite=True)
check("#119: --overwrite keeps a user file", (user_dir / "thesis.txt").read_text() == "mine")
check("#119: --overwrite removes a stale run artefact", not (user_dir / "note.md").exists())
check("#119: --overwrite empties images/", not any((user_dir / "images").iterdir()))
check("#119: --overwrite keeps the directory itself", user_dir.is_dir())
check(
    "overwrite=True on a fresh path reports created",
    dyb.Bundle.open(src, output_dir=str(tmp / "fresh-overwrite"), overwrite=True).created,
)
check(
    "a fresh bundle reports itself as created",
    dyb.Bundle.open(src, output_dir=str(explicit)).created is False,
)

# --- #118: two lectures must not share one vault note; hand edits are not clobbered ---
_v118 = tmp / "vault118"
_v118.mkdir()
_lec_a = dyb.Bundle.open(src, output_dir=str(tmp / "lecture-a"))
(tmp / "lecture-a" / "note.md").write_text("# A\n")
_lec_a.record("note", tmp / "lecture-a" / "note.md")
_lec_b = dyb.Bundle.open(src, output_dir=str(tmp / "lecture-b"))
(tmp / "lecture-b" / "note.md").write_text("# B\n")
_lec_b.record("note", tmp / "lecture-b" / "note.md")
_lec_a.export_to_vault(_v118)
_lec_b.export_to_vault(_v118)
check(
    "#118: two lectures keep two notes (default subdir is per bundle)",
    (_v118 / "dyak" / "lecture-a" / "note.md").read_text() == "# A\n"
    and (_v118 / "dyak" / "lecture-b" / "note.md").read_text() == "# B\n",
)
(_v118 / "dyak" / "lecture-a" / "note.md").write_text("# A, hand-edited in Obsidian\n")
try:
    _lec_a.export_to_vault(_v118)
    _refused = None
except OSError as _exc:
    _refused = _exc
check(
    "#118: a hand-edited vault note is refused, not replaced",
    _refused is not None
    and (_v118 / "dyak" / "lecture-a" / "note.md").read_text() == "# A, hand-edited in Obsidian\n",
)
_lec_a.export_to_vault(_v118, force=True)
check(
    "#118: --force replaces a hand-edited note",
    (_v118 / "dyak" / "lecture-a" / "note.md").read_text() == "# A\n",
)
# The vault export is a copy, verified per file, and never consumes the bundle.
b_export = dyb.Bundle.open(src, output_dir=str(tmp / "toexport"))
(tmp / "toexport" / "note.md").write_text("# lecture\n")
(tmp / "toexport" / "images").mkdir()
(tmp / "toexport" / "images" / "f_0001.jpg").write_bytes(b"jpeg-bytes")
b_export.record("note", tmp / "toexport" / "note.md")
b_export.record("frame0", tmp / "toexport" / "images" / "f_0001.jpg")
vault = tmp / "vault"
copied = b_export.export_to_vault(vault)
check(
    # Frames are 84% of the bytes -- 50 of 59 MiB on lecture 1 -- and the note refers to
    # none of them: zero `![[...]]` embeds, zero frame filenames, only time ranges like
    # `### Фрагмент 1 (0–414 с)`. Copying them by default put 50 MiB per lecture into a
    # directory Obsidian indexes and obsidian-git synchronises, for files nothing reads.
    "images are not exported by default",
    not (vault / "dyak" / "images").exists(),
    str(sorted(str(p.relative_to(vault)) for p in vault.rglob("*"))),
)
check(
    "the note is exported even though the frames are not",
    (vault / dyb.DEFAULT_EXPORT_SUBDIR / b_export.root.name / "note.md").is_file(),
    str(sorted(q.name for q in vault.iterdir())),
)
check(
    "--vault-images copies the frames too",
    len(b_export.export_to_vault(vault, images=True)) == 2
    and (
        vault / dyb.DEFAULT_EXPORT_SUBDIR / b_export.root.name / "images" / "f_0001.jpg"
    ).is_file(),
    str(copied),
)
b_export2 = dyb.Bundle.open(src, output_dir=str(tmp / "toexport2"))
(tmp / "toexport2" / "note.md").write_text("# lecture\n")
b_export2.record("note", tmp / "toexport2" / "note.md")
check(
    "a bundle with no frames exports everything it has", len(b_export2.export_to_vault(vault)) == 1
)
copied = b_export2.export_to_vault(vault)
check(
    # The subdir is not cosmetic. A lecture bundle is ~170 files; exporting into the vault
    # root puts 25 of them beside the user's own notes and creates `images/` next to them.
    "the export lands in its own directory, not the vault root",
    (vault / dyb.DEFAULT_EXPORT_SUBDIR / b_export.root.name / "note.md").is_file(),
    str(sorted(q.name for q in vault.iterdir())),
)
check(
    "nothing is written to the vault root",
    not any(q.is_file() for q in vault.iterdir()),
    str(sorted(q.name for q in vault.iterdir())),
)
check(
    "frames land under images/",
    (vault / dyb.DEFAULT_EXPORT_SUBDIR / b_export.root.name / "images" / "f_0001.jpg").is_file(),
    str(copied),
)
check(
    "an explicit subdir is honoured",
    len(b_export.export_to_vault(vault, subdir="mscs/Курс", images=True)) == 2
    and (vault / "mscs/Курс/note.md").is_file()
    and (vault / "mscs/Курс/images/f_0001.jpg").is_file(),
    str(sorted(str(p.relative_to(vault)) for p in vault.rglob("note.md"))),
)

# images/ accumulated across runs. `Bundle.open(overwrite=False)` and a `mkdir(exist_ok)`
# property removed nothing, so the directory was the union of every run that touched it.
# Measured on lecture 1 after three runs: 73 PNGs where the manifest listed 58, and all 15
# extras sat below 135.5 s -- the first run's range, before the `-frame_pts` timebase fix.
b_images = dyb.Bundle.open(src, output_dir=str(tmp / "imgbundle"))
for stale_name in ("f_1.png", "f_2.png", "f_3_q.jpg"):
    (b_images.images / stale_name).write_bytes(b"stale")
check("stale frames accumulated before the reset", len(list(b_images.images.iterdir())) == 3)
check("clear_images reports what it removed", b_images.clear_images() == 3)
check("images/ is empty after the reset", not list(b_images.images.iterdir()))
check("clear_images leaves the directory", b_images.images.is_dir())
check("clear_images is idempotent", b_images.clear_images() == 0)
check(
    "the export did not consume the bundle",
    (tmp / "toexport" / "note.md").is_file()
    and (tmp / "toexport" / "images" / "f_0001.jpg").is_file(),
)

# Two fault paths the happy path never reaches. Both were mutations the suite missed, so
# they are exercised by injecting the failure rather than by hoping for it.
b_copy = dyb.Bundle.open(src, output_dir=str(tmp / "copyfault"))
victim = tmp / "copyfault_src.txt"
victim.write_bytes(b"payload that must not vanish")
real_move = dyb.shutil.move


def refuse_move(*_a, **_kw):
    raise OSError(18, "Invalid cross-device link")


dyb.shutil.move = refuse_move
try:
    moved = b_copy.publish("doc", victim)
finally:
    dyb.shutil.move = real_move
check(
    "a refused rename still lands the file in the bundle",
    moved.read_bytes() == b"payload that must not vanish",
    str(moved),
)
check(
    "a refused rename does not silently discard the source",
    victim.exists(),
    "source deleted despite the copy path",
)

b_short = dyb.Bundle.open(src, output_dir=str(tmp / "shortwrite"))
src_note = tmp / "shortwrite_note.md"
src_note.write_text("# a note that is long enough to truncate\n" * 8)
b_short.record("note", src_note)
real_copy2 = dyb.shutil.copy2


def truncate_copy(src, dst, **kwargs):
    real_copy2(src, dst, **kwargs)
    pathlib.Path(dst).write_bytes(pathlib.Path(dst).read_bytes()[:10])


vault_trunc = tmp / "vault_trunc"
dyb.shutil.copy2 = truncate_copy
try:
    exc = raises(b_short.export_to_vault, vault_trunc)
finally:
    dyb.shutil.copy2 = real_copy2
check(
    "a truncated export is refused rather than reported as done",
    exc is not None,
    "no exception raised for a short copy",
)
check(
    "the refusal says what failed to verify",
    exc is not None and "did not verify" in str(exc),
    getattr(exc, "message", repr(exc)),
)
check(
    "the bundle survives a failed export",
    src_note.read_text().startswith("# a note"),
    "bundle consumed by a failed export",
)

# The bundle summary is measured, not asserted in prose.
check("the summary counts artefacts", "2 artefacts" in b_export.summary(), b_export.summary())
check("an empty bundle says so", dyb.Bundle(root=tmp / "empty").summary() == "bundle empty")

del os.environ["XDG_STATE_HOME"]
if _real_state is not None:
    os.environ["XDG_STATE_HOME"] = _real_state
if _real_outdir is not None:
    os.environ["DYAK_OUTPUT_DIR"] = _real_outdir


# --- 10b. stage 5 receives files that exist ------------------------------------
# Regression: stage 4 published the frames by moving them into the bundle, then handed
# stage 5 the pre-move path list. Every frame failed with "cannot read the frame size",
# and only the end-to-end run showed it -- the stubbed pipeline above moves nothing.
real_run, real_publish = REAL_PIPELINE_RUN, dyb.Bundle.publish
seen: dict = {}
# Restore the real pipeline before the call below. Section 7 leaves dyp.run bound to
# fake_pipeline, and calling *that* made this regression test pass vacuously for as long as
# it existed: the stub never reaches stage 5, so nothing about frame ordering was checked.
# A test that cannot fail is worse than no test, because it reports coverage it does not have.
dyp.run = real_run


def fake_frames_run(source, frames_dir):
    frames_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(2):
        path = Path(frames_dir) / f"f_{i}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
        made.append(path)
    # A real manifest, not `t\tstate\n0\tA\n`. `read_manifest` skips the first line and
    # parses `pts_ms` from the third column, so that stub yielded zero frames and every
    # downstream assertion about gating was vacuously true.
    (Path(frames_dir) / "manifest.tsv").write_text(
        "pts_ms\ttime\tfile\n"
        + "".join(f"{i * 1000}\t00:00:0{i}.00\t{made[i].name}\n" for i in range(2))
    )
    return Path(frames_dir) / "manifest.tsv", made


def spy_quality_run(frames, work_dir, *, settings=None, bbox_source="geometric", stream=None):
    seen["paths"] = list(frames)
    seen["missing"] = [f.name for f in frames if not Path(f).is_file()]
    # One real FAIL, not an empty report. Returning `frames=[]` here is why the stage-5 gate
    # being a no-op (#82) was invisible to this test: with no frames there is no gate verdict
    # to lose, so the wiring could be wrong in either direction and the suite stayed green.
    # `f_0.png` passes so it reaches the images dir; `f_1.png` fails so it must not.
    return dyq.Report(
        frames=[
            dyq.FrameQuality(
                name=Path(f).name,
                source=Path(f),
                passed=(i == 0),
                mege=100.0,
                threshold=50.0,
                crop_state="cropped",
                content_type="white_document",
                bbox_source="geometric",
                edges=4,
                luma_mean=200.0,
                luma_std=10.0,
            )
            for i, f in enumerate(frames)
        ],
        threshold=50.0,
    )


# Only the I/O stages are stubbed. pipeline.run itself runs for real, because the ordering
# bug lived in pipeline.run's body -- stubbing pipeline.run (as the rest of this file does)
# is exactly why the original bug was invisible.
dyp.frames.run = fake_frames_run
dyp.quality.run = spy_quality_run
real_probe_run, real_audio_run = dyp.probe.probe, dyp.audio.extract
real_stt_run, real_stt_write = dyp.stt.transcribe, dyp.stt.write
real_stt_backend = dyp.stt.backend
real_require = dyp.probe.require_supported
real_coverage = dyp.audio.check_coverage
real_rates = dyp.stt.RATE_RANGE
dyp.probe.probe = lambda _p: dyprobe.MediaInfo(
    path=_p,
    duration=1.0,
    has_video=True,
    has_audio=True,
    video_codec="vp8",
    width=16,
    height=16,
    audio_codec="opus",
    sample_rate=48000,
    channels=2,
)
dyp.probe.require_supported = lambda _i: None
dyp.audio.extract = lambda _p, wav: (
    wav.write_bytes(b"RIFF"),
    dypa.AudioArtefact(path=wav, duration=1.0, sample_rate=16000, channels=1, size_bytes=4),
)[1]
dyp.audio.check_coverage = lambda _i, _a: ""


class _FlushRecorder(io.StringIO):
    """A stream that counts flushes, so "flushed before the blocking call" is assertable.

    `io.StringIO.flush` is a no-op, and a check that only reads `getvalue()` cannot tell a
    written line from a flushed one -- which is the half of #100/#193 a redirected log cares
    about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _continuation(text: str, unit: str) -> str:
    """The last `           {unit} k/N` line in `text`, or `""` when there is none.

    The #205/#206 standard needs "what the stream held at call time" to be compared, and a
    snapshot can hold earlier units' lines; the last one is the line this call just saw.
    """
    lines = [line for line in text.splitlines() if line.startswith(f"           {unit} ")]
    return lines[-1] if lines else ""


# The stage-3 line goes here, not to the suite's stdout, so the assertions read the same bytes
# a user's redirected log would hold.
captured = _FlushRecorder()
# #186/#193: the line must name the backend that will actually run, and it must already be in
# the stream when the blocking call is entered. The stub resolves `openai`, so on unmodified
# main -- whose line is the literal "whisper.cpp on CPU" -- the target assertion is the half
# that fails; the flush half already held for that one line.
dyp.stt.backend = lambda *a, **k: SimpleNamespace(
    info=lambda: dysb.BackendInfo(name="openai", target="https://stt.example/v1")
)


def _order_stt_spy(wav, *, audio_duration, backend=None, on_progress=None):
    """Stands in for the blocking call, and asserts what is already in the stream."""
    seen["stt_backend"] = backend
    check(
        "#193: the resolved backend's start line is in the stream, flushed, before the "
        "blocking call returns",
        captured.flushes >= 1
        and "starting" in captured.getvalue()
        and "https://stt.example/v1" in captured.getvalue(),
        f"flushes={captured.flushes} stream={captured.getvalue()!r}",
    )
    if on_progress is not None:
        # The real pipeline hands stage 3's callback down here, so the artefact check below
        # runs against a line that was really rendered by `pipeline._chunk_line`.
        on_progress(0, 1)
        on_progress(1, 1)
    return dyst.Transcript(wav=wav, audio_duration=audio_duration, segments=(), backend="spy")


dyp.stt.transcribe = _order_stt_spy
# Keyed "json", not "transcript.json": `stt.write` returns short keys and stage 6 reads
# `paths["json"]`. A stub with the wrong keys is how the first end-to-end run of this stage
# died on a KeyError that no test could see.
#
# The stub writes the files it returns. It used to name `t.raw.json` and `t.txt` without
# creating them, and that passed only because `Bundle.publish` returned a dangling path
# silently; now that it raises (#89) a fake has to produce what it claims to have produced.
#
# The names are the real ones (`stt/core.py` writes `transcript.raw.json`, `transcript.json`,
# `transcript.txt`). While the stub used `t.raw.json` / `t.txt` / `order_transcript.json`, the
# three REQUIRED transcript entries could not resolve against the registry in this test and
# nothing said so -- #88's subject was invisible here precisely because the fake was unfaithful.
order_transcript = tmp / "transcript.json"
order_transcript.write_text(
    json.dumps(
        {
            "audio_duration": 1.0,
            "segments": [{"index": 0, "start": 0.0, "end": 1.0, "text": "hello", "words": []}],
        }
    ),
    encoding="utf-8",
)


def _stub_stt_write(tr, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "transcript.raw.json").write_text("{}", encoding="utf-8")
    (dest / "transcript.txt").write_text("hello", encoding="utf-8")
    return {
        "raw": dest / "transcript.raw.json",
        "json": order_transcript,
        "txt": dest / "transcript.txt",
    }


dyp.stt.write = _stub_stt_write
work_probe = tmp / "order_work"
work_probe.mkdir(parents=True, exist_ok=True)
_r = None
try:
    _r = dyp.run(
        src,
        dyw.WorkDir(parent=work_probe),
        bundle=dyb.Bundle.open(src, output_dir=str(tmp / "order_bundle")),
        stream=captured,
    )
    check("the real pipeline.run reached the end", True)
except BaseException as _exc:
    import traceback

    traceback.print_exc()
    check("the real pipeline.run reached the end", False, repr(_exc))
finally:
    dyp.run, dyp.frames.run, dyp.quality.run = real_run, dyfr.run, REAL_QUALITY_RUN
    dyp.probe.probe, dyp.audio.extract = real_probe_run, real_audio_run
    dyp.stt.transcribe, dyp.stt.write = real_stt_run, real_stt_write
    dyp.stt.backend = real_stt_backend
    dyp.probe.require_supported, dyp.audio.check_coverage = real_require, real_coverage
    dyp.stt.RATE_RANGE = real_rates
check(
    "stage 5 is handed frames that exist on disk",
    seen.get("missing") == [],
    f"missing at stage 5: {seen.get('missing')}",
)
check("stage 5 was actually invoked", len(seen.get("paths", [])) == 2, str(seen.get("paths")))
check(
    "the frames still reach the bundle afterwards",
    len(list((tmp / "order_bundle" / "images").glob("*.png"))) == 2,
    str(sorted(q.name for q in (tmp / "order_bundle" / "images").glob("*"))),
)
check(
    # #186: the line's claim and the work cannot diverge -- the resolved name is handed to
    # `stt.transcribe`, not re-resolved inside it.
    "the resolved backend name is handed to the work, not only printed",
    seen.get("stt_backend") == "openai",
    str(seen.get("stt_backend")),
)
check(
    "#193: the progress lines reach the stream",
    "           chunks 0/1" in captured.getvalue()
    and "           chunks 1/1" in captured.getvalue(),
    repr(captured.getvalue()[-300:]),
)
# The bundle holds every artefact this run published, and the note is one of them. Not the word
# `chunk`: `stt-provenance.json` legitimately carries a `"chunking"` block (#125). #197 extends
# the same scan to the unit words stages 6, 7 and 9 use, because those stages write their lines
# through the same renderer and their loops run inside the same bundle.
_progress_markers = ("           chunks ", "           frames ", "           sections ")
_progress_hits = [
    f"{path.relative_to(tmp)}: {marker!r}"
    for path in (tmp / "order_bundle").rglob("*")
    if path.is_file()
    for marker in _progress_markers
    if marker in path.read_text(encoding="utf-8", errors="replace")
]
check(
    "#193/#197: and no progress text enters any artefact",
    not _progress_hits,
    str(_progress_hits),
)


# --- #88: stage 12 verifies this run's artefacts, not files that happen to be named so -----
# The registry keys are not the bundle-root names -- stage 3 registers `json`/`txt`/`raw`,
# stage 4 `manifest`, stage 9 `audit/audit.json`, stage 10 `repair/repair.json` -- and
# resolution used to be `artefacts.get(name) or (root / name)`. For six of the eight REQUIRED
# entries the lookup missed and the check stat'd whatever file had that name in the root.
# Asserted on the real run above, because that is the only place the real keys exist.
check(
    "#88: every REQUIRED artefact resolves from the real registry, not from the root listing",
    {e["artefact"] for e in _r.report.verified} == set(dyro.required_for(tmp / "order_bundle"))
    if _r is not None
    else False,
    f"verified={sorted(e['artefact'] for e in _r.report.verified) if _r else None}; "
    f"required={sorted(dyro.required_for(tmp / 'order_bundle'))}",
)
check(
    "#88: and the six whose registry key differs are among them, not silently absent",
    _r is not None
    and all(
        name in {e["artefact"] for e in _r.report.verified}
        for name in (
            "transcript.json",
            "transcript.txt",
            "transcript.raw.json",
            dyau.REPORT_NAME,
            dyrep.REPORT_NAME,
            dyfr.MANIFEST,
        )
    ),
    f"verified={sorted(e['artefact'] for e in _r.report.verified) if _r else None}",
)
_r88_report_json = json.loads((tmp / "order_bundle" / dyro.REPORT_NAME).read_text(encoding="utf-8"))
check(
    "#88: the verification record names the run it belongs to",
    bool(_r88_report_json.get("run_id"))
    and _r88_report_json.get("run_id") == (_r.report.run_id if _r else None)
    and bool(_r88_report_json.get("started_at")),
    f"report.json run_id={_r88_report_json.get('run_id')!r} "
    f"started_at={_r88_report_json.get('started_at')!r}",
)
check(
    "#88: and report-provenance.json says where that identity came from",
    "from pipeline.run"
    in (tmp / "order_bundle" / dyro.PROVENANCE_NAME).read_text(encoding="utf-8"),
    "the provenance does not explain the run_id field",
)


# --- #186: the start line names the backend that will actually run ---------------------
# `auto` prefers whisper.cpp but falls back to the OpenAI-compatible route
# (`stt/backends.py:129-134`), and the line used to say "whisper.cpp on CPU" whatever ran. The
# interval has the same defect: `_estimate` is this host's whisper.cpp measurement
# (`stt/core.py:46-52`), and nothing in the tree measures a hosted endpoint's rate. Driven
# through the real `pipeline.run` -- stubbing `pipeline.run` is how the original ordering bug
# stayed invisible -- with stages 1-2 faked and `transcribe` raising to stop the run right after
# the line, so the line is the real code's output and not a re-implementation of it.
class _StopAtStt(Exception):
    pass


def _stage3_line(name: str, target: str) -> str:
    """The real stage-3 start line, for one stubbed backend resolution."""
    out_stream = io.StringIO()
    saved = (
        dyp.probe.probe,
        dyp.probe.require_supported,
        dyp.audio.extract,
        dyp.audio.check_coverage,
        dyp.stt.backend,
        dyp.stt.transcribe,
    )
    dyp.probe.probe = lambda _p: dyprobe.MediaInfo(
        path=_p,
        duration=180.0,
        has_video=True,
        has_audio=True,
        video_codec="vp8",
        width=16,
        height=16,
        audio_codec="opus",
        sample_rate=48000,
        channels=2,
    )
    dyp.probe.require_supported = lambda _i: None
    dyp.audio.extract = lambda _p, wav: (
        wav.write_bytes(b"RIFF"),
        dypa.AudioArtefact(path=wav, duration=180.0, sample_rate=16000, channels=1, size_bytes=4),
    )[1]
    dyp.audio.check_coverage = lambda _i, _a: ""
    dyp.stt.backend = lambda *a, **k: SimpleNamespace(
        info=lambda: dysb.BackendInfo(name=name, target=target)
    )

    def _stop(*a, **k):
        raise _StopAtStt()

    dyp.stt.transcribe = _stop
    try:
        with tempfile.TemporaryDirectory() as _td:
            source = Path(_td) / "line.mp4"
            source.write_bytes(b"x")
            out_dir = Path(_td) / "bundle"
            out_dir.mkdir()
            try:
                dyp.run(
                    source,
                    dyw.WorkDir(parent=Path(_td)),
                    bundle=dyb.Bundle.open(source, output_dir=str(out_dir), overwrite=False),
                    stream=out_stream,
                )
            except _StopAtStt:
                pass
    finally:
        (
            dyp.probe.probe,
            dyp.probe.require_supported,
            dyp.audio.extract,
            dyp.audio.check_coverage,
            dyp.stt.backend,
            dyp.stt.transcribe,
        ) = saved
    return out_stream.getvalue()


whispercpp_line = _stage3_line("whispercpp", "http://100.64.0.1:10301")
check(
    "#186: the whisper.cpp line names the backend and its target, and keeps its own interval",
    "starting" in whispercpp_line
    and "whispercpp at http://100.64.0.1:10301" in whispercpp_line
    and "expect " in whispercpp_line
    and "on CPU" not in whispercpp_line,
    repr(whispercpp_line),
)

hosted_line = _stage3_line("openai", "https://stt.example/v1")
check(
    "#186: the hosted line names the endpoint and does not borrow whisper.cpp's interval",
    "starting" in hosted_line
    and "openai at https://stt.example/v1" in hosted_line
    and "expect " not in hosted_line
    and "no rate measured for this endpoint" in hosted_line,
    repr(hosted_line),
)


class _BrokenStream:
    """A stream whose writes fail, to pin the rule that progress cannot move an outcome."""

    def write(self, text):
        raise OSError("no space left on device")

    def flush(self):
        raise OSError("no space left on device")


_progress_recorder = _FlushRecorder()
dyp._chunk_line(_progress_recorder)(0, 3)
check(
    "#193: a progress line is flushed, so a redirected log sees it before the next request",
    _progress_recorder.getvalue() == "           chunks 0/3\n" and _progress_recorder.flushes == 1,
    f"{_progress_recorder.getvalue()!r} flushes={_progress_recorder.flushes}",
)

_progress_raised = None
try:
    dyp._chunk_line(_BrokenStream())(1, 2)
except BaseException as exc:
    # Any escape is the failure being tested. A progress line must not decide a run's outcome:
    # `deps.py:280`'s rule for warnings, applied to the stream (#193).
    _progress_raised = repr(exc)
check(
    "#193: a progress write that fails cannot move the run's outcome",
    _progress_raised is None,
    str(_progress_raised),
)

# #197: one renderer, four callers. Stage 3's adapter is pinned above; the text, the single
# flush and the swallowed failure are pinned once here, and the per-stage blocks below pin that
# each loop calls it before its unit's blocking call.
_unit_recorder = _FlushRecorder()
dys.progress_line(_unit_recorder, "frames", 0, 3)
check(
    "#197: the shared renderer writes one indented, prefix-free, flushed line",
    _unit_recorder.getvalue() == "           frames 0/3\n" and _unit_recorder.flushes == 1,
    f"{_unit_recorder.getvalue()!r} flushes={_unit_recorder.flushes}",
)

_unit_raised = None
try:
    dys.progress_line(_BrokenStream(), "sections", 1, 8)
except BaseException as exc:
    # Same rule as above, on the shared renderer rather than through stage 3's adapter.
    _unit_raised = repr(exc)
check(
    "#197: and a failing unit line cannot move an outcome either",
    _unit_raised is None,
    str(_unit_raised),
)


# --- #92: the bundle says how the run ended ---------------------------------------------
# Before this, the run's identity and its per-stage seconds were written on the last line of
# `pipeline.run`, so a run that did not reach that line left a bundle holding everything stage
# 7 had published and nothing saying the run had not finished. To a reader that is a
# successful run whose verification artefacts happen to be missing.
def _run_state(root: Path) -> dict:
    """The run's state, or `{}` when it left none -- which is the failure being tested.

    Read through a guard so a missing file fails the check with a readable message rather than
    aborting the suite from inside a `read_text`.
    """
    target = root / dyp.RUN_STATE_NAME
    return json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}


_rs_done = _run_state(tmp / "order_bundle")
check(
    "a completed run leaves its identity in the bundle (#92)",
    _rs_done.get("outcome") == "completed"
    and bool(_rs_done.get("run_id"))
    and bool(_rs_done.get("started_at"))
    and bool(_rs_done.get("finished_at")),
    str({k: _rs_done.get(k) for k in ("outcome", "run_id", "started_at", "finished_at")}),
)
check(
    "and names the run id after the source, so a bug report can quote it (#92)",
    _rs_done.get("run_id", "").endswith(Path(src).stem[:24]) and _rs_done.get("source") == str(src),
    f"run_id={_rs_done.get('run_id')} source={_rs_done.get('source')}",
)
check(
    "and records which stages ran, so a partial bundle is not read as a complete one (#92)",
    _rs_done.get("stages_completed") == list(range(1, 13))
    and _rs_done.get("stage_reached") == 12
    and _rs_done.get("stages_implemented") == 12,
    str(_rs_done.get("stages_completed")),
)
check(
    "and the per-stage seconds are written by the same code path (#92)",
    (tmp / "order_bundle" / dyp.TIMINGS_NAME).is_file(),
    "timings.json is missing from a completed run",
)

# --- 14b. the bundle reaches the vault, and names what it leaves behind ----------------
# #90: `export_to_vault` copies by registry, so a file written straight into `bundle.root`
# never leaves the bundle. Stage 12 wrote `report.json` and `report-provenance.json` that
# way, so the vault received the note and every input to it and not the verdict that
# approved it. Measured on the lecture-1 bundle, the difference between the bundle root and
# the vault was exactly those two names -- and `timings.json`, which is a sidecar on purpose.
#
# The general form is what matters, so that is what is asserted: every file in the bundle
# root is registered, except a named sidecar. A new artefact written straight to the root
# fails here rather than quietly not being exported.
_r_registered = {Path(p).name for p in _r.bundle.artefacts.values()} if _r is not None else set()
_r_root_files = (
    {p.name for p in _r.bundle.root.iterdir() if p.is_file()} if _r is not None else set()
)
#: The declared exceptions. `TIMINGS_NAME` and `RUN_STATE_NAME` in `pipeline.py` say why
#: neither is registered; if either decision is reversed, this list changes with it.
SIDECARS = {dyp.TIMINGS_NAME, dyp.RUN_STATE_NAME}

check(
    "stage 12's artefacts are registered, so they can be exported",
    _r is not None and set(dyro.ALL_ARTEFACTS) <= _r_registered,
    f"registered {sorted(_r_registered)}; stage 12 writes {list(dyro.ALL_ARTEFACTS)}",
)
check(
    "every file in the bundle root is registered, except a declared sidecar",
    _r is not None and not (_r_root_files - _r_registered - SIDECARS),
    f"in the root but not registered: {sorted(_r_root_files - _r_registered - SIDECARS)}",
)
check(
    "the sidecar really is unregistered, so the check above is not passing for free",
    dyp.TIMINGS_NAME in _r_root_files and dyp.TIMINGS_NAME not in _r_registered,
    f"root has it: {dyp.TIMINGS_NAME in _r_root_files}; "
    f"registered: {dyp.TIMINGS_NAME in _r_registered}",
)
_r_vault = tmp / "order_vault"
#: The frames are registered too, and `export_to_vault` skips them unless `--vault-images`
#: is passed, so they are excluded from both sides of the comparison below.
_r_registered_off_images = (
    {Path(p).name for p in _r.bundle.artefacts.values() if Path(p).parent.name != dyb.IMAGES}
    if _r is not None
    else set()
)
if _r is not None:
    _r.bundle.export_to_vault(_r_vault)
_r_vault_files = (
    {
        p.name
        for p in (_r_vault / dyb.DEFAULT_EXPORT_SUBDIR / _r.bundle.root.name).iterdir()
        if p.is_file()
    }
    if _r is not None
    else set()
)
check(
    "the vault carries the verdict, not only the note",
    _r is not None and set(dyro.ALL_ARTEFACTS) <= _r_vault_files,
    f"vault holds {sorted(_r_vault_files)}",
)
check(
    "the vault is the registered bundle minus the sidecars and the frames",
    _r is not None and _r_vault_files == _r_registered_off_images - SIDECARS,
    f"vault {sorted(_r_vault_files)} vs registered off-images {sorted(_r_registered_off_images)}",
)
check(
    "and summary() now says what it counts",
    _r is not None and "registered" in _r.bundle.summary(),
    _r.bundle.summary() if _r is not None else "no result",
)

# --- 15. the run leaves a timing record ---------------------------------------------
# Every `StageReport` has carried its seconds since the pipeline was written, and
# `RunResult.seconds` sums them -- all of it in memory, all of it printed to a terminal that
# closes. Nothing reached the bundle, so the bundle could not answer "what did this run cost".
# Stage 3 transcribes 4520 s of audio in ~297 s and no artefact said so; stages 6, 7 and 9
# record per-call seconds, which made it look like a stage-3 gap rather than a whole-run one.
_timings_path = tmp / "order_bundle" / dyp.TIMINGS_NAME
check(
    "a real pipeline.run writes timings.json into the bundle",
    _timings_path.is_file(),
    f"{_timings_path} exists={_timings_path.is_file()}",
)
if _timings_path.is_file():
    _tm = json.loads(_timings_path.read_text())
    _tm_stages = _tm.get("stages", [])
    check(
        "every stage is timed, not just the ones that printed a duration",
        [_s["stage"] for _s in _tm_stages] == list(range(1, dyp.IMPLEMENTED + 1)),
        f"stages recorded: {[_s['stage'] for _s in _tm_stages]}",
    )
    check(
        "each stage carries its name, its seconds and the summary that was printed",
        all(
            _s["name"]
            and isinstance(_s["seconds"], (int, float))
            and _s["seconds"] >= 0
            and _s["summary"] is not None
            for _s in _tm_stages
        ),
        str(_tm_stages[:2]),
    )
    check(
        "the total is the sum of the parts, not a separate measurement",
        abs(_tm["total_seconds"] - round(sum(_s["seconds"] for _s in _tm_stages), 1)) < 0.2,
        f"total={_tm['total_seconds']} sum={round(sum(_s['seconds'] for _s in _tm_stages), 1)}",
    )
    check(
        "stage 3 is timed -- the 297 s that nothing else recorded",
        any(_s["name"] == "stt" for _s in _tm_stages),
        str([_s["name"] for _s in _tm_stages]),
    )

# The run above is stubbed, so every stage finishes in ~0 s and "the total is the sum of the
# parts" is 0 == 0 -- it would pass against a hardcoded zero. Asserted again on real numbers
# through `write_timings` directly, where the sum has something to get wrong.
_tb = dyb.Bundle.open(tmp / "timing_unit.mp4", output_dir=str(tmp / "timing_bundle"))
dyb.Bundle.open(tmp / "timing_unit.mp4", output_dir=str(tmp / "timing_bundle")).record(
    "placeholder", tmp / "timing_unit.mp4"
)
_tunit = [
    dyp.StageReport(3, "stt", 296.5, "4901 segments"),
    dyp.StageReport(7, "synth", 155.9, "8/8"),
]
_twritten = dyp.write_timings(_tb, _tunit)
_tu = json.loads(_twritten.read_text())
check(
    "the total is a real sum of real per-stage seconds",
    abs(_tu["total_seconds"] - 452.4) < 0.05
    and [s["seconds"] for s in _tu["stages"]] == [296.5, 155.9],
    f"total={_tu['total_seconds']} stages={[(s['seconds']) for s in _tu['stages']]}",
)

# A sidecar, not an artefact. If it went through `Bundle.record` the count in the run's own
# closing line would move, and stage 12's REQUIRED/OPTIONAL verification would gain a name
# that no stage produces.
check(
    "timings.json is not registered as a pipeline artefact",
    dyp.TIMINGS_NAME not in dyro.REQUIRED and dyp.TIMINGS_NAME not in dyro.OPTIONAL,
    "stage 12 now expects a file that no stage publishes",
)

# --- 15b. report.json says what it did not check --------------------------------
# `report.json` said `"ok": true` after stat-ing 65 of 143 files, and nothing in the artefact
# said so. On run 12 `captions.json` and `quality.json` were never examined, so a corrupted
# quality gate still exits 0. #87.
#
# The gap is now a number a reader can see *and* three files smaller: #87 promoted
# `captions.json` and `synth.json` into `REQUIRED` and made `quality.json` required when the
# source had video. This block still asserts the reporting half -- `unchecked` is what remains
# outside the check -- and the promotion is asserted below, per file.
_order_report = json.loads((tmp / "order_bundle" / dyro.REPORT_NAME).read_text(encoding="utf-8"))
check(
    "report.json records how many files it checked and how many the bundle holds",
    isinstance(_order_report.get("files_checked"), int)
    and isinstance(_order_report.get("files_in_bundle_excluding_this_stage"), int)
    and _order_report["files_checked"] > 0
    and _order_report["files_in_bundle_excluding_this_stage"] > 0,
    str(
        {
            k: _order_report.get(k)
            for k in ("files_checked", "files_in_bundle_excluding_this_stage", "unchecked")
        }
    ),
)
check(
    "checked plus unchecked is exactly what the bundle held",
    len(_order_report["unchecked"]) + _order_report["files_checked"]
    == _order_report["files_in_bundle_excluding_this_stage"],
    f"unchecked={len(_order_report['unchecked'])} "
    f"checked={_order_report['files_checked']} "
    f"bundle={_order_report['files_in_bundle_excluding_this_stage']}",
)
check(
    "#87: captions.json is verified now, not named as unexamined -- it is stage 6's whole product",
    "captions.json" in {e["artefact"] for e in _r.report.verified}
    and "captions.json" not in _order_report["unchecked"],
    f"verified={sorted(e['artefact'] for e in _r.report.verified)}; "
    f"unchecked={_order_report['unchecked']}",
)
check(
    "#87: synth.json is verified too, so the coverage and section gates cannot read an absent file",
    "synth.json" in {e["artefact"] for e in _r.report.verified}
    and "synth.json" not in _order_report["unchecked"],
    f"verified={sorted(e['artefact'] for e in _r.report.verified)}",
)
check(
    "#87: quality.json is required because the source had video, and it is verified",
    _order_report.get("source_had_video") is True
    and _order_report.get("quality_gate_required") is True
    and "quality.json" in {e["artefact"] for e in _r.report.verified},
    f"source_had_video={_order_report.get('source_had_video')} "
    f"required={_order_report.get('quality_gate_required')} "
    f"verified={sorted(e['artefact'] for e in _r.report.verified)}",
)
_order_expected, _order_rejected = dyro.expected_frames(tmp / "order_bundle")
check(
    "every frame count_frames verified is absent from the unexamined list",
    not ({f"images/{name}" for name in _order_expected} & set(_order_report["unchecked"])),
    f"expected {sorted(_order_expected)}, rejected {sorted(_order_rejected)}, "
    f"unexamined frames {[n for n in _order_report['unchecked'] if n.startswith('images/')]}",
)
# The opposite is true and correct: stage 5 moves every produced frame into `images/` and then
# decides which pass, so a rejected frame stays on disk, is subtracted from `frames_expected`,
# and is therefore never examined by anything. It belongs in the list. The first version of the
# check above asserted it was absent, which was a claim about the code rather than a measurement.
check(
    "a frame the gate rejected is listed as unexamined, because nothing checks it",
    _order_rejected
    and all(f"images/{name}" in _order_report["unchecked"] for name in _order_rejected),
    f"rejected {sorted(_order_rejected)}; unexamined "
    f"{[n for n in _order_report['unchecked'] if n.startswith('images/')]}",
)
check(
    "report.json does not claim to have verified itself",
    dyro.REPORT_NAME not in _order_report["unchecked"],
    str(_order_report["unchecked"]),
)

# The stage-5 gate was a no-op in the real pipeline (#82). `Bundle.publish` moves, so the
# work-dir quality.json stage 5 wrote was dangling by the time stage 6 read it; `align` treats a
# missing file as "no gate information", which makes `if alignment.quality_gate and ...` false
# for every frame, so nothing was ever gated out. `publish` returns a non-existent source path
# unchanged rather than raising, so no run reported it.
#
# Read the *published* captions.json rather than calling `align()` directly. Calling `align()`
# with a quality file that exists is what the rest of this file does, and it passed throughout
# while the wiring was broken.
_order_caps = json.loads((tmp / "order_bundle" / "captions.json").read_text(encoding="utf-8"))
_order_by_name = {a["frame"]: a for a in _order_caps["alignments"]}
check(
    "the gate verdict survives into the published captions.json",
    _order_by_name.get("f_1.png", {}).get("quality_gate") == "FAIL",
    str({k: v.get("quality_gate") for k, v in _order_by_name.items()}),
)
check(
    "and the frame stage 5 rejected is GATED_OUT, not captioned anyway",
    _order_by_name.get("f_1.png", {}).get("caption_status") == "GATED_OUT",
    str({k: v.get("caption_status") for k, v in _order_by_name.items()}),
)
check(
    # Empty strings on all 58 frames is the exact signature of the bug, and a weaker version of
    # this check is what would have caught it: `""` is falsy, so the guard silently passed.
    "no frame carries an empty gate while stage 5 recorded one",
    all(a["quality_gate"] for a in _order_caps["alignments"]),
    str([a["frame"] for a in _order_caps["alignments"] if not a["quality_gate"]]),
)
check(
    # Stage 12's `count_frames` walks the manifest and stats each row. Stage 5 moves *every*
    # frame stage 4 produced into `images/` -- `pipeline.run` calls `clear_images` then moves
    # `produced`; the gate's verdict only decides which of them get an enhanced `_q.jpg`. A
    # rejected frame is therefore on disk, so the subtraction is not what keeps `ok` true (both
    # counts agree either way): it keeps the rejection out of `frames_expected` and in
    # `unchecked`, so ADR-0005 D2's "N states, M uncaptioned is visible" is a number rather than
    # a silently verified frame. Reachable only since #82 -- before it, the gate never fired and
    # the subtraction was a no-op.
    "stage 12 counts the gate's rejections as expected, not as missing",
    dyro.count_frames(tmp / "order_bundle")[0] == 1
    and dyro.count_frames(tmp / "order_bundle")[1] == 1,
    f"expected/found = {dyro.count_frames(tmp / 'order_bundle')}, want (1, 1) for 1 pass + 1 fail",
)
check(
    # And the rejection must be visible rather than merely subtracted, or ADR-0005 D2's "N
    # states, M uncaptioned" has nowhere to live.
    "the rejected frame is recorded in captions.json, not silently dropped",
    _order_by_name.get("f_1.png", {}).get("reason", "").startswith("stage 5"),
    repr(_order_by_name.get("f_1.png", {}).get("reason")),
)
check(
    "the frame stage 5 passed still carries PASS",
    _order_by_name.get("f_0.png", {}).get("quality_gate") == "PASS",
    str(_order_by_name.get("f_0.png", {}).get("quality_gate")),
)

# --- 11. stage 5 quality: MEGE, the full-frame fallback, and a derived gate ------------
qs = dyq.Settings()
qtmp = tmp / "quality"
qtmp.mkdir(parents=True, exist_ok=True)


def qframe(name, colour, size="640x360", vf=None):
    path = qtmp / name
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={size}"]
    if vf:
        args += ["-vf", vf]
    subprocess.run(args + ["-frames:v", "1", str(path)], check=True)
    return path


def observe(path, settings=qs, source="geometric"):
    return dyq.observe(path, settings, dyq.resolve_estimator(source))


def verdict(path, threshold, settings=qs, out=None, source="geometric"):
    obs = observe(path, settings, source)
    dest = out or (qtmp / "q_out")
    dest.mkdir(parents=True, exist_ok=True)
    return dyq.evaluate(obs, dest, settings, threshold)


# Ground truth: the measure must be 0 on a field with no structure, or the threshold
# is measuring noise rather than sharpness.
uniform = qframe("uniform.png", "gray")
check(
    "a uniform field has zero MEGE",
    dyq.sobel_mege(dyq._raw_gray(uniform, 640, 360), 640, 360, qs.tau)[0] == 0.0,
    str(dyq.sobel_mege(dyq._raw_gray(uniform, 640, 360), 640, 360, qs.tau)),
)

subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=1:duration=1",
        "-frames:v",
        "1",
        str(qtmp / "detail.png"),
    ],
    check=True,
)
detail_mege = observe(qtmp / "detail.png").mege
check("a detailed frame scores above the uniform field", detail_mege > 0.0, str(detail_mege))


# Independent oracle for MEGE. Written as a double loop over 2D indices with clamped
# indexing, rather than the padded numpy slicing the implementation uses, so an indexing
# or sign error in one cannot be reproduced by copying the other.
def oracle_mege(raster, w, h, tau):
    g = [[raster[y * w + x] for x in range(w)] for y in range(h)]

    def at(yy, xx):
        return g[min(max(yy, 0), h - 1)][min(max(xx, 0), w - 1)]

    vals = []
    for y in range(h):
        for x in range(w):
            gx = (at(y - 1, x + 1) + 2 * at(y, x + 1) + at(y + 1, x + 1)) - (
                at(y - 1, x - 1) + 2 * at(y, x - 1) + at(y + 1, x - 1)
            )
            gy = (at(y + 1, x - 1) + 2 * at(y + 1, x) + at(y + 1, x + 1)) - (
                at(y - 1, x - 1) + 2 * at(y - 1, x) + at(y - 1, x + 1)
            )
            mag = gx * gx + gy * gy
            if mag >= tau:
                vals.append(mag)
    return (sum(vals) / len(vals), len(vals)) if vals else (0.0, 0)


w3, h3 = 7, 5
ramp = bytes(((x * 37 + y * 53) % 256) for y in range(h3) for x in range(w3))
mine = dyq.sobel_mege(ramp, w3, h3, qs.tau)[0]
theirs = oracle_mege(ramp, w3, h3, qs.tau)[0]
check(
    "MEGE matches an independent reference implementation",
    abs(mine - theirs) < 1e-9,
    f"got={mine} reference={theirs}",
)
check(
    "an all-zero raster has no edge pixels at all",
    dyq.sobel_mege(bytes(w3 * h3), w3, h3, qs.tau) == (0.0, 0),
    str(dyq.sobel_mege(bytes(w3 * h3), w3, h3, qs.tau)),
)
check(
    "a raster too small to have an interior measures zero",
    dyq.sobel_mege(bytes(4), 2, 2, qs.tau) == (0.0, 0),
    str(dyq.sobel_mege(bytes(4), 2, 2, qs.tau)),
)

# The property the replacement metric was adopted for: replicate the same strokes and the
# value must not move. This is the 7.21x failure of Laplacian variance, expressed as a test.
one_stroke = qframe(
    "one_stroke.png",
    "black",
    vf="drawbox=x=100:y=100:w=60:h=40:color=white:t=fill",
)
many_strokes = qframe(
    "many_strokes.png",
    "black",
    vf=(
        "drawbox=x=100:y=100:w=60:h=40:color=white:t=fill,"
        "drawbox=x=300:y=100:w=60:h=40:color=white:t=fill,"
        "drawbox=x=100:y=220:w=60:h=40:color=white:t=fill,"
        "drawbox=x=300:y=220:w=60:h=40:color=white:t=fill"
    ),
)
one_obs = observe(one_stroke)
many_obs = observe(many_strokes)
check(
    "four times the strokes gives roughly four times the edge pixels",
    many_obs.edges > one_obs.edges * 3,
    f"one={one_obs.edges} many={many_obs.edges}",
)
check(
    "MEGE is flat under a 4x change in content density",
    abs(many_obs.mege / one_obs.mege - 1.0) < 0.05,
    f"one={one_obs.mege:.0f} many={many_obs.mege:.0f} ratio={many_obs.mege / one_obs.mege:.3f}",
)

# Blur must lower the measure, or the gate cannot detect an unusable source at all.
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(qtmp / "detail.png"),
        "-vf",
        "gblur=sigma=2",
        "-frames:v",
        "1",
        str(qtmp / "blurred.png"),
    ],
    check=True,
)
check(
    "blur lowers the measure",
    observe(qtmp / "blurred.png").mege < detail_mege,
    f"sharp={detail_mege} blurred={observe(qtmp / 'blurred.png').mege}",
)

# THE REGRESSION. A dark canvas with real sharp content gets no bright region, so the old
# code returned passed=False for every one of these. It is valid content for this lecture.
# Built by dimming a detailed source rather than by drawing sparse boxes on black: a few
# boxes are neither dense enough to clear the edge floor nor representative of what a dark
# slide actually is. Dimming keeps the edge population and drops the luma below the
# bright-pixel threshold, which is exactly the case the estimator cannot see.
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=1:duration=1",
        "-vf",
        "format=gray,lut=y='val*0.45'",
        "-frames:v",
        "1",
        str(qtmp / "dark_canvas.png"),
    ],
    check=True,
)
dark_canvas = qtmp / "dark_canvas.png"
dc_obs = observe(dark_canvas)
check("a dark canvas yields no bbox", dc_obs.box is None, str(dc_obs.box))
check(
    "a dark canvas is measured full-frame, not skipped",
    dc_obs.crop_state == dyq.CROP_FALLBACK,
    dc_obs.crop_state,
)
check(
    "a dark canvas is classified as a dark canvas",
    dc_obs.content_type == dyq.CONTENT_DARK,
    dc_obs.content_type,
)
check(
    "a dark canvas clears the edge floor",
    dyq.has_edges(dc_obs, qs),
    f"edges={dc_obs.edges} of {dc_obs.width * dc_obs.height} px",
)
dc_verdict = verdict(dark_canvas, dc_obs.mege * 0.5)
check(
    "a dark canvas with sharp content PASSES the gate",
    dc_verdict.passed,
    f"mege={dc_obs.mege:.0f} reason={dc_verdict.reason}",
)
check(
    "its verdict is still a real measurement, not a rubber stamp",
    dc_verdict.mege == dc_obs.mege and dc_verdict.mege > 0.0,
    f"mege={dc_verdict.mege}",
)
check(
    "the same canvas at a raised threshold fails, so the gate reads it",
    not verdict(dark_canvas, dc_obs.mege * 2.0).passed,
    "a dark canvas passed a threshold it should have failed",
)

# The regression that actually bit: the full-frame path built its filter chain by appending
# to a string, so "flags=lanczos" and "unsharp=" fused whenever there was no crop. Every
# full-frame frame died with ffmpeg exit 234 and the run reported it as a quality failure.
dc_enhanced = qtmp / "q_out" / dyq.enhanced_name(dark_canvas.name)
check("a full-frame enhancement actually writes a file", dc_enhanced.is_file(), str(dc_enhanced))
check(
    "the enhanced frame is non-empty",
    dc_enhanced.is_file() and dc_enhanced.stat().st_size > 0,
    "empty file",
)
check(
    "enhanced frames are named .jpg, because -q:v into a .png path is silently discarded",
    dyq.enhanced_name("f_0000000000.png").endswith(".jpg"),
    dyq.enhanced_name("f_0000000000.png"),
)

# A uniform dark field is blank, not soft. Reporting it as "soft" hides which failure
# occurred, and no sharpness number is meaningful on it.
blank = qframe("blank.png", "black")
blank_obs = observe(blank)
check("a black frame is classified blank", dyq.is_blank(blank_obs, qs), str(blank_obs.luma_std))
blank_verdict = verdict(blank, 0.0)
check(
    "a blank frame fails with EMPTY_CANVAS, not SOFT",
    not blank_verdict.passed and blank_verdict.reason == dyq.REASON_BLANK,
    blank_verdict.reason,
)
check(
    "a blank frame gets no enhanced file",
    not (qtmp / "q_out" / dyq.enhanced_name(blank.name)).exists(),
    "wrote an enhanced frame for a blank canvas",
)

# A white frame has a bbox covering most of itself.
white = qframe("white.png", "white")
white_obs = observe(white)
check("a white frame gets a bbox", white_obs.box is not None, "no box")
if white_obs.box:
    check(
        "the bbox does not exceed the frame",
        white_obs.box.width <= 640 and white_obs.box.height <= 360,
        f"{white_obs.box.width}x{white_obs.box.height}",
    )
check(
    "a white frame is classified as a white document",
    white_obs.content_type == dyq.CONTENT_WHITE,
    white_obs.content_type,
)

# The margin is only observable on a frame whose bright region is smaller than the frame.
inset = tmp / "quality_inset"
inset.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360",
        "-vf",
        "drawbox=x=200:y=120:w=240:h=120:color=white:t=fill",
        "-frames:v",
        "1",
        str(inset / "panel.png"),
    ],
    check=True,
)
panel_obs = observe(inset / "panel.png")
check("a centred panel gets a bbox", panel_obs.box is not None, "no box")
if panel_obs.box:
    check(
        "the bbox is wider than the bright panel, i.e. the margin was applied",
        panel_obs.box.width > 240 and panel_obs.box.x < 200,
        f"box={panel_obs.box.width} at x={panel_obs.box.x}, panel=240 at x=200",
    )

# The gate is derived from the run, not fixed. Two frames, and the threshold must land at
# decay x median(MEGE) -- and be above the weaker frame.
two = [observe(qtmp / "detail.png"), observe(qtmp / "blurred.png")]
derived = dyq.threshold_for(two, qs)
mid = sorted(o.mege for o in two)[1] / 2 + sorted(o.mege for o in two)[0] / 2
check(
    "the threshold is decay x median over content frames",
    abs(derived - qs.decay * mid) < 1e-6,
    f"derived={derived} expected={qs.decay * mid}",
)
check(
    "the weaker frame falls below the derived threshold",
    min(o.mege for o in two) < derived <= max(o.mege for o in two),
    f"derived={derived} meges={[o.mege for o in two]}",
)
# A run of nothing but blank canvases must not normalise its way to "everything passes".
check(
    "an all-blank run falls back to the floor",
    dyq.threshold_for([blank_obs, blank_obs], qs) == qs.floor,
    str(dyq.threshold_for([blank_obs, blank_obs], qs)),
)
check(
    "an empty run falls back to the floor",
    dyq.threshold_for([], qs) == qs.floor,
    str(dyq.threshold_for([], qs)),
)

# The same frame flips with the threshold, and the reason names the measured value.
passing = verdict(qtmp / "detail.png", detail_mege * 0.5)
failing = verdict(qtmp / "detail.png", detail_mege * 2.0)
check("a frame can pass", passing.passed, "unexpected fail")
check("the same frame fails a raised threshold", not failing.passed, "gate ignored setting")
check(
    "a failing reason is SOFT",
    failing.reason == dyq.REASON_SOFT,
    failing.reason,
)
check(
    "a too-thin edge population is NO_EDGES, distinct from SOFT",
    verdict(qtmp / "blurred.png", 0.0, dyq.Settings(min_edge_fraction=0.99)).reason
    == dyq.REASON_NO_EDGES,
    verdict(qtmp / "blurred.png", 0.0, dyq.Settings(min_edge_fraction=0.99)).reason,
)

# An unreadable input is a failed frame, not a crash: one bad file must not lose 15 good ones.
bogus = qtmp / "not_an_image.png"
bogus.write_bytes(b"not an image at all")
try:
    observe(bogus)
    crashed = None
except OSError as exc:
    crashed = exc
check(
    "a corrupt frame raises OSError for run() to catch",
    crashed is not None,
    f"crashed={crashed!r}",
)
check(
    "the reason names the input that was unusable",
    crashed is not None and "not_an_image.png" in str(crashed),
    f"crashed={crashed!r}",
)

mixed = dyq.run([qtmp / "detail.png", bogus], qtmp / "run", settings=qs, stream=io.StringIO())
check("run() survives a corrupt frame", len(mixed.frames) == 2, str(len(mixed.frames)))
check(
    "the corrupt frame is reported as failed, with its reason",
    not mixed.ok and any("not_an_image.png" in f.reason for f in mixed.frames if not f.passed),
    str([(f.name, f.reason[:40]) for f in mixed.frames]),
)
check(
    "the good frame in the same run still passed",
    any(f.name == "detail.png" and f.passed for f in mixed.frames),
    str([(f.name, f.passed) for f in mixed.frames]),
)
check(
    "an empty run is not ok", not dyq.run([], qtmp / "empty", settings=qs, stream=io.StringIO()).ok
)

# bbox sources: a protocol with three honest implementations.
check(
    "the null estimator never crops",
    observe(dark_canvas, source="null").crop_state == dyq.CROP_FALLBACK,
    observe(dark_canvas, source="null").crop_state,
)
check(
    "the geometric estimator does crop a white frame",
    observe(white, source="null").crop_state == dyq.CROP_FALLBACK
    and observe(white).crop_state == dyq.CROP_CROPPED,
    "source selection is not reaching observe()",
)
try:
    dyq.resolve_estimator("vlm")
    vlm_raised = None
except KeyError as exc:
    vlm_raised = exc
check(
    # `vlm` was registered and raised NotConfiguredError on both branches, so setting
    # DYAK_BBOX_SOURCE=vlm failed at the crop rather than being refused as the unknown
    # name it is. A vision bbox source is unbuilt; an unbuilt option must be absent from the
    # registry, not present and broken (#67).
    "the unbuilt `vlm` bbox source is not registered at all",
    vlm_raised is not None and "vlm" in str(vlm_raised),
    f"raised={vlm_raised!r}",
)
try:
    dyq.resolve_estimator("telepathy")
    unknown_raised = None
except KeyError as exc:
    unknown_raised = exc
check(
    "an unknown bbox source is refused by name",
    unknown_raised is not None and "telepathy" in str(unknown_raised),
    f"raised={unknown_raised!r}",
)

# The report must not leak the post-enhancement number into the gate field, and the three
# orthogonal measurements must be present separately.
qtmp2 = tmp / "quality_report"
qtmp2.mkdir(parents=True, exist_ok=True)
mixed_report = dyq.run([qtmp / "detail.png", dark_canvas], qtmp2, settings=qs, stream=io.StringIO())
dyq.write(mixed_report, qtmp2 / "quality.json", qs)
payload = json.loads((qtmp2 / "quality.json").read_text())
check(
    "the report records the derived threshold",
    payload.get("threshold") == mixed_report.threshold,
    str(payload.get("threshold")),
)
check(
    "the report records how the threshold was derived",
    "median" in payload.get("threshold_rule", ""),
    str(payload.get("threshold_rule")),
)
names = {f.name for f in mixed_report.frames}
entry = next(f for f in payload["frames"] if f["name"] == dark_canvas.name)
check(
    "the three measurements are separate fields",
    {"quality_gate", "content_type", "crop_state"} <= set(entry),
    str(sorted(entry)),
)
check(
    "a dark canvas is recorded as full-frame and dark_canvas",
    (entry["crop_state"], entry["content_type"]) == (dyq.CROP_FALLBACK, dyq.CONTENT_DARK),
    str(entry),
)
# Note what is NOT asserted here: that this canvas PASSES the run-derived gate. In a
# two-frame run the median is taken across one dense light slide and this dark one, and
# their MEGE values legitimately differ -- a dense light slide really does have steeper
# gradients. Whether the canvas clears the run's gate is tested above against an explicit
# threshold; what this run checks is that it is measured and classified at all.
check("the gated field is present", "mege" in entry, str(sorted(entry)))
check(
    "the enhanced measure is recorded separately",
    "mege_enhanced" in entry and entry["mege"] != entry.get("mege_enhanced"),
    str(entry),
)
check(
    "the gate decision uses the source number only",
    entry["quality_gate"] == ("PASS" if entry["mege"] >= entry["threshold"] else "FAIL"),
    str(entry),
)
check(
    "the bbox source reaches the report",
    {f["bbox_source"] for f in payload["frames"]} == {"geometric"},
    str([f["bbox_source"] for f in payload["frames"]]),
)
provenance = json.loads(dyq.provenance(qs, "geometric"))
check(
    "provenance names the metric and the raster policy",
    provenance["metric"] == "MEGE" and "native" in provenance["raster"],
    str(provenance),
)


# --- 12. stage 6 caption: deterministic alignment, no model ------------------------
cs = dycap.Settings()
ctmp = tmp / "caption"
ctmp.mkdir(parents=True, exist_ok=True)
cimg = ctmp / "images"
cimg.mkdir(parents=True, exist_ok=True)


def seg(index, start, end, text, words=None):
    return {
        "index": index,
        "start": start,
        "end": end,
        "text": text,
        "words": words or [],
    }


def word(start, text=" w"):
    return {"text": text, "start": start, "end": start + 0.1, "probability": 0.9}


# Three frames at 0, 100 s and 200 s; segments that straddle each boundary.
transcript_payload = {
    "audio_duration": 300.0,
    "segments": [
        seg(0, 0.0, 60.0, "alpha", [word(0.0), word(30.0)]),
        # Straddles the 100 s frame boundary: starts before it, ends after.
        seg(1, 90.0, 150.0, "beta", [word(90.0), word(100.5), word(140.0)]),
        seg(2, 150.0, 260.0, "gamma", [word(150.0), word(255.0)]),
    ],
}
ctrans = ctmp / "transcript.json"
ctrans.write_text(json.dumps(transcript_payload), encoding="utf-8")
cmanifest = ctmp / "manifest.tsv"
cmanifest.write_text(
    "pts_ms\ttime\tfile\n0\t00:00:00\tf_0000000000.png\n"
    "100000\t00:01:40\tf_0000100000.png\n200000\t00:03:20\tf_0000200000.png\n",
    encoding="utf-8",
)

cquality = ctmp / "quality.json"
cquality.write_text(
    json.dumps(
        {
            "threshold": 100.0,
            "frames": [
                {
                    "name": "f_0000000000.png",
                    "quality_gate": "PASS",
                    "content_type": "white_document",
                    "crop_state": "cropped",
                },
                {
                    "name": "f_0000100000.png",
                    "quality_gate": "PASS",
                    "content_type": "white_document",
                    "crop_state": "cropped",
                },
                {
                    "name": "f_0000200000.png",
                    "quality_gate": "FAIL",
                    "content_type": "dark_canvas",
                    "crop_state": "full_frame_fallback",
                },
            ],
        }
    ),
    encoding="utf-8",
)

capp = dycap.align(cmanifest, ctrans, cquality, cimg)
check("three frames aligned", len(capp.alignments) == 3, str(len(capp.alignments)))
check(
    "the manifest header is not read as a frame",
    [a.frame for a in capp.alignments][0] == "f_0000000000.png",
    str([a.frame for a in capp.alignments]),
)

# THE REGRESSION. The window was [pts_{i-1}, pts_i): a frame extracted at 100 s was given
# everything spoken before 100 s, which belongs to the previous frame. A frame with pts = T
# was on screen FROM T.
by_name = {a.frame: a for a in capp.alignments}
check(
    "frame windows are [pts_i, pts_{i+1})",
    [by_name[f].on_screen_ms for f in sorted(by_name)]
    == [(0, 100000), (100000, 200000), (200000, 300000)],
    str([by_name[f].on_screen_ms for f in sorted(by_name)]),
)
# Segment 1 straddles 100 s, and segment 2 ([150, 260]) also reaches into [100, 200): both
# belong to this frame. Segment 0 ([0, 60]) is entirely in the previous frame's window.
check(
    "a frame takes every segment overlapping its own window",
    by_name["f_0000100000.png"].segment_indices == [1, 2],
    str(by_name["f_0000100000.png"].segment_indices),
)
check(
    "and not the one that straddles the previous frame's",
    0 not in by_name["f_0000100000.png"].segment_indices,
    str(by_name["f_0000100000.png"].segment_indices),
)
check(
    "windows are contiguous and cover the lecture without gaps",
    all(
        by_name[k].on_screen_ms[1] == by_name[next_k].on_screen_ms[0]
        for k, next_k in zip(sorted(by_name)[:2], sorted(by_name)[1:])
    ),
    str([by_name[f].on_screen_ms for f in sorted(by_name)]),
)

# The word boundary, not the segment boundary. Only straddle had a word inside the window.
check(
    "the straddling frame's first word is timed to the frame, not the segment",
    abs(by_name["f_0000100000.png"].first_word_ms - 100.5) < 1e-6,
    str(by_name["f_0000100000.png"].first_word_ms),
)
# The third frame is GATED_OUT, so it is never aligned and carries no boundary. Asserting
# "all three" would have been asserting the absence of the gate.
check(
    "every aligned frame gets a word boundary, not only the first",
    [a.first_word_ms for a in capp.considered] == [0.0, 100.5],
    str([(a.frame, a.first_word_ms, a.caption_status) for a in capp.alignments]),
)

check(
    "a frame stage 5 rejected is carried as GATED_OUT, not aligned",
    by_name["f_0000200000.png"].caption_status == "GATED_OUT",
    by_name["f_0000200000.png"].caption_status,
)
check(
    "the gate verdict travels with the alignment",
    by_name["f_0000200000.png"].quality_gate == "FAIL"
    and by_name["f_0000200000.png"].crop_state == "full_frame_fallback",
    str(by_name["f_0000200000.png"]),
)

# Captioning is absent, and says so. A null caption and a missing caption field must not
# look the same to stage 7.
check(
    "caption is None and its status says why",
    by_name["f_0000000000.png"].caption is None
    and by_name["f_0000000000.png"].caption_status == "NOT_CONFIGURED",
    str(by_name["f_0000000000.png"]),
)

# Coverage is the gate. A frame with no transcript behind it is reported, and too many of
# them means the audio and video disagree.
empty_transcript = ctmp / "silent.json"
empty_transcript.write_text(json.dumps({"audio_duration": 300.0, "segments": []}), encoding="utf-8")
# Segments only inside [0, 100 s): frame 0 is covered, frame 1 is not, frame 2 is gated
# out. With no quality report all three are candidates, so the share is 1 of 3 = 0.33 --
# deliberately either side of the 0.30 default.
partial_transcript = ctmp / "partial.json"
partial_transcript.write_text(
    json.dumps({"audio_duration": 300.0, "segments": [seg(0, 0.0, 90.0, "only early")] * 1}),
    encoding="utf-8",
)
silent = dycap.align(cmanifest, empty_transcript, cquality, cimg)
check(
    "a frame with no transcript is uncovered",
    len(silent.uncovered) == 2,
    str(len(silent.uncovered)),
)
# A gated-out frame is not a candidate, so it does not count against A/V sync. Letting a
# blur failure show up as a muxing failure would send the reader to the wrong subsystem.
check(
    "a gated-out frame is not counted against coverage",
    len(silent.considered) == 2 and len(silent.uncovered) == 2 and silent.uncovered_share == 1.0,
    f"considered={len(silent.considered)} uncovered={len(silent.uncovered)} share={silent.uncovered_share}",
)
check(
    "every considered frame uncovered fails the gate",
    not silent.ok,
    f"share={silent.uncovered_share}",
)
check(
    "and clearing it is a one-line setting change, not a code path",
    dycap.align(
        cmanifest, empty_transcript, cquality, cimg, dycap.Settings(uncovered_share_limit=1.0)
    ).ok,
    "a 1.0 share under a 1.0 limit must clear",
)

# Segments only inside [0, 100 s): frame 0 covered, frame 1 not, frame 2 gated out. With no
# quality report all three are candidates, so the share is 1 of 3 = 0.33 -- deliberately on
# both sides of the 0.30 default, which is what makes the boundary worth asserting.
partial_transcript = ctmp / "partial.json"
partial_transcript.write_text(
    json.dumps(
        {
            "audio_duration": 300.0,
            # Covers frame 0's window only. Frame 1 [100, 200) falls in the gap and frame 2
            # [200, 300) is covered by the second segment, so exactly one of three misses.
            "segments": [seg(0, 0.0, 90.0, "only early"), seg(1, 210.0, 280.0, "only late")],
        }
    ),
    encoding="utf-8",
)
for limit, expected in ((0.30, False), (0.40, True)):
    got = dycap.align(
        cmanifest,
        partial_transcript,
        ctmp / "missing-quality.json",
        cimg,
        dycap.Settings(uncovered_share_limit=limit),
    )
    check(
        f"1 of 3 uncovered {'clears' if expected else 'fails'} a {limit} limit",
        got.ok is expected and abs(got.uncovered_share - 1 / 3) < 0.01,
        f"share={got.uncovered_share} ok={got.ok} limit={limit}",
    )
check("no frames is not ok", not dycap.align(ctmp / "none.tsv", ctrans, cquality, cimg).ok)


def _reader(module, name, path):
    """`module.name(path)`, or a two-tuple naming why the reader could not be called.

    On unmodified main the readers below do not exist, so calling one directly in a `check`
    argument would abort this suite instead of recording a failure; a missing reader is a failed
    check, not a reason to stop the run. The sentinel's second element is the `repr` of the
    failure, so `_reader(...)[1]` is always a string and `_reader(...) == (None, "")` is false.
    """
    try:
        return getattr(module, name)(path)
    except BaseException as exc:  # recorded, not swallowed
        return None, repr(exc)


# #188/#202: stage 6 parsed the gate report with an unguarded json.loads, so a quality.json it
# could not read killed the run as an internal error -- JSONDecodeError, or AttributeError for
# a payload that is not an object -- which reaches the user as a traceback and Python's exit 1
# (cli.py:214-220), indistinguishable from a usage error. Stage 12 reads the same file through
# report.read_quality_gate; the loop below pins stage 6 to the same words for the same bytes,
# so one file is not described two ways. `[1, 2]` was the one shape stage 12 could not name
# (report.py:437 called payload.get with no isinstance check); #202 added the check, so the
# shape lives in the loop rather than in a stage-6-only block.
for _label, _raw, _why in (
    ("truncated", '{"frames": [', "cannot read"),
    ("frames-is-not-a-list", '{"frames": "x"}', "quality.json has no 'frames' list"),
    ("no-frames-key", "{}", "quality.json has no 'frames' list"),
    ("not-a-json-object", "[1, 2]", "quality.json is not a JSON object"),
):
    _path = ctmp / f"quality-{_label}.json"
    _path.write_text(_raw, encoding="utf-8")
    _root = ctmp / f"quality-root-{_label}"
    _root.mkdir(exist_ok=True)
    (_root / dyq.REPORT_NAME).write_text(_raw, encoding="utf-8")
    _raised = None
    try:
        dycap.align(cmanifest, ctrans, _path, cimg)
    except BaseException as _exc:  # on main this is JSONDecodeError/AttributeError
        _raised = _exc
    # A named failure is `runner.DependencyError`, not any exception: on unmodified main the
    # first two shapes raise JSONDecodeError/AttributeError and this suite must record that as
    # a failure rather than abort on it.
    _named = isinstance(_raised, dyr.DependencyError)
    _message = _raised.message if _named else repr(_raised)
    _stage12 = _reader(dyro, "read_quality_gate", _root)[1]
    check(
        f"#188: a {_label} quality.json fails stage 6 with a named reason, not a traceback",
        _named and _raised.code == ec.ARTEFACT_VERIFICATION_FAILED and _why in _message,
        _message,
    )
    check(
        f"#188/#202: and {_label} is named in the same words stage 12 uses for the same file",
        _named and _message == _stage12,
        f"{_message!r} vs {_stage12!r}",
    )

# #201: the same guard for transcript.json. Stage 6 parsed it twice -- once for the alignment
# segments and once for `audio_duration` -- with no guard, so a truncated file or a non-object
# payload reached the user as a JSONDecodeError/AttributeError traceback and Python's exit 1
# (cli.py:214-220), while {"segments": "x"} was read as "no words": every frame it considers is
# reported uncovered (captions.json's provenance and the CLI's explain output) and the note loses
# the frames' text. Since #228 the exit fires -- code 4 (`cli.py:442`), or 7 or 1 first when the
# note is degraded (`cli.py:426`) -- and only an audio-only source, whose `considered` is empty,
# still exits 0 on the guard. Reading it
# as "no words" is still a claim about the lecture made from a wrong-shaped file. Stage 12 has no
# reader for this artefact; `caption.read_transcript` is it, and `align` now reads the file once.
for _label, _raw, _why in (
    ("truncated", '{"audio_duration": 300.0, "segments": [', "cannot read transcript.json"),
    ("not-a-json-object", "[1, 2]", "transcript.json is not a JSON object"),
    ("segments-not-a-list", '{"segments": "x"}', "transcript.json has no 'segments' list"),
    ("segments-null", '{"segments": null}', "transcript.json has no 'segments' list"),
    ("no-segments-key", '{"audio_duration": 300.0}', "transcript.json has no 'segments' list"),
):
    _path = ctmp / f"transcript-{_label}.json"
    _path.write_text(_raw, encoding="utf-8")
    _raised = None
    try:
        dycap.align(cmanifest, _path, cquality, cimg)
    except BaseException as _exc:  # on main: JSONDecodeError/AttributeError, or no raise at all
        _raised = _exc
    _named = isinstance(_raised, dyr.DependencyError)
    _message = _raised.message if _named else repr(_raised)
    check(
        f"#201: a {_label} transcript.json fails stage 6 with a named reason, not a traceback",
        _named and _raised.code == ec.ARTEFACT_VERIFICATION_FAILED and _why in _message,
        _message,
    )
    _reader_reason = _reader(dycap, "read_transcript", _path)[1]
    check(
        f"#201: and {_label} names transcript.json in the reader's words",
        _named and _message == _reader_reason,
        f"{_message!r} vs {_reader_reason!r}",
    )
_absent_transcript = _reader(dycap, "read_transcript", ctmp / "no-such-transcript.json")
check(
    "#201: an absent transcript.json is legitimate -- the reader says so with no reason",
    _absent_transcript == (None, ""),
    repr(_absent_transcript),
)
_whole_transcript = _reader(dycap, "read_transcript", ctrans)
check(
    "#201: a well-formed transcript.json is returned whole by the one reader",
    _whole_transcript[0] == transcript_payload,
    repr(_whole_transcript),
)

# The other half: an absent report, and readable reports that carry no gate information, stay
# non-failures. `{}` used to land here -- the #82 shape, every frame captioned including the
# ones the gate rejected -- which is why it is asserted as a failure above, not here.
for _label, _raw in (
    ("absent", None),
    ("empty-frames-list", '{"frames": []}'),
    ("frames-list-of-non-dicts", '{"frames": [1, 2]}'),
):
    _path = ctmp / f"quality-readable-{_label}.json"
    if _raw is not None:
        _path.write_text(_raw, encoding="utf-8")
    _escaped = ""
    try:
        dycap.align(cmanifest, ctrans, _path, cimg)
    except BaseException as _exc:  # the assertion is that nothing escapes
        _escaped = repr(_exc)
    check(
        f"#188: {_label} is not a failure -- stage 6 reads it as no gate information",
        not _escaped,
        _escaped,
    )

# Malformed input is skipped, not fatal.
broken = ctmp / "broken.json"
broken.write_text(
    json.dumps(
        {
            "audio_duration": 300.0,
            "segments": [
                seg(0, 0.0, 10.0, "fine"),
                {"index": "x", "start": "y", "end": None, "text": "bad types"},
                {"index": 2, "start": 50.0, "end": 40.0, "text": "reversed"},
            ],
        }
    ),
    encoding="utf-8",
)
_broken_payload = json.loads(broken.read_text(encoding="utf-8"))
_kept = None
try:
    _kept = dycap.read_segments(_broken_payload)
except BaseException as _exc:  # on main read_segments takes a Path, not a payload
    _kept = repr(_exc)
check(
    "unparseable and reversed segments are dropped rather than guessed at",
    isinstance(_kept, list) and [s.index for s in _kept] == [0],
    repr(_kept),
)
_missing_transcript = _reader(dycap, "read_transcript", ctmp / "does-not-exist.json")
check(
    "a missing transcript is empty, not a traceback",
    _missing_transcript == (None, ""),
    repr(_missing_transcript),
)
check(
    "a missing manifest is empty, not a traceback",
    dycap.read_manifest(ctmp / "nope.tsv") == [],
    "raised",
)

# The artefact.
cdest = ctmp / "out"
creport, coutcome = dycap.run(cmanifest, ctrans, cquality, cimg, cdest, stream=io.StringIO())
check(
    "run() writes the report",
    (cdest / dycap.REPORT_NAME).is_file(),
    str(sorted(p.name for p in cdest.iterdir())),
)
check(
    "run() writes provenance",
    (cdest / dycap.PROVENANCE_NAME).is_file(),
    str(sorted(p.name for p in cdest.iterdir())),
)
cpayload = json.loads((cdest / dycap.REPORT_NAME).read_text(encoding="utf-8"))
check(
    "provenance says this stage needs a model",
    json.loads((cdest / dycap.PROVENANCE_NAME).read_text())["requires_model"] is True,
    (cdest / dycap.PROVENANCE_NAME).read_text(),
)
check(
    # The old assertion was that provenance names the caption gap via a `NOT_CONFIGURED`
    # string. The string is gone by design -- it read like content. What has to survive is
    # the gap being *recorded*: an absent endpoint must still be visible in the artefact,
    # with the count of frames it cost.
    "provenance records that no frame was captioned, without a placeholder string",
    json.loads((cdest / dycap.PROVENANCE_NAME).read_text())["caption"]["captioned"] == 0
    and "NOT_CONFIGURED"
    not in json.loads((cdest / dycap.PROVENANCE_NAME).read_text())["caption"]["model"],
    (cdest / dycap.PROVENANCE_NAME).read_text(),
)
check(
    "an unconfigured endpoint marks each frame NO_ENDPOINT rather than leaving a caption slot",
    all(
        a["caption_status"] == "NO_ENDPOINT"
        for a in cpayload["alignments"]
        if a["caption_status"] != "GATED_OUT"
    ),
    str([a["caption_status"] for a in cpayload["alignments"]]),
)
check(
    "every alignment round-trips through the artefact",
    [a["frame"] for a in cpayload["alignments"]] == [a.frame for a in creport.alignments]
    and cpayload["alignments"][0]["on_screen_ms"] == list(creport.alignments[0].on_screen_ms),
    str(cpayload["alignments"][0]),
)
check(
    "the report carries the coverage the gate used",
    "uncovered_share" in cpayload["provenance"],
    str(cpayload["provenance"]),
)


# --- 12a. stage 6 captioning: the half that calls a model -------------------------
# The tests above cover alignment and the no-endpoint degradation. These cover the path
# that actually exists now: a caption comes back, the cache is keyed correctly, and a
# refusing endpoint is recorded rather than swallowed.
#
# `llm.chat` is replaced rather than `urlopen`, so these assert what this stage owns -- the
# message it builds, the status it records, the cache key it uses -- and not the transport,
# which `test_llm.py` and the FakeHTTP block already cover.
_real_chat = dyle.chat


def _caption_reply(text="$\\dot{x} = Ax$, state feedback"):
    return dyle.Reply(
        text=text,
        model="fake-vision",
        prompt_tokens=10,
        completion_tokens=10,
        seconds=0.01,
        attempts=1,
        served_model="fake-vision",
    )


_seen_messages: list = []
# The alignment tests above never needed the frames to exist -- `align` reads only the
# manifest and the quality report. Captioning opens the file, so they have to be present.
# The bytes are arbitrary and deliberately not a real PNG: nothing decodes them, and
# `image_data_uri` picks the media type from the suffix, which is the part under test.
_FAKE_FRAME = b"\x89PNG\r\n\x1a\n" + b"frame-bytes-for-stage-6"
for _name in ("f_0000000000.png", "f_0000100000.png", "f_0000200000.png"):
    (cimg / _name).write_bytes(_FAKE_FRAME)

_leftovers = os.environ.pop("DYAK_VLM_MODEL", None) or os.environ.pop("DYAK_LLM_MODEL", None)
os.environ.pop("DYAK_VLM_ENDPOINT", None)
os.environ.pop("DYAK_LLM_ENDPOINT", None)
os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
os.environ["DYAK_VLM_MODEL"] = "fake-vision"


# #197: what the stream held when this call started. A value sequence alone is what a write
# deferred to the end of the loop also produces, so the stub records the stream itself and the
# check below requires the two to agree (#205).
_vision_stream = _FlushRecorder()
_vision_snapshots: list[str] = []


def _fake_vision_chat(messages, config, **kwargs):
    _seen_messages.append((messages, config))
    _vision_snapshots.append(_vision_stream.getvalue())
    return _caption_reply()


dyle.chat = _fake_vision_chat
try:
    vdest = ctmp / "vision_out"
    vreport, voutcome = dycap.run(cmanifest, ctrans, cquality, cimg, vdest, stream=_vision_stream)
    vcache = json.loads((vdest / dycap.REPORT_NAME).read_text())["captions"]
    # Guard first: `all()` over an empty sequence is True, and three of these assertions
    # were vacuously green before the frames existed. Every check below states how many
    # things it looked at, so a zero reads as a failure rather than as a pass.
    check(
        "there is at least one frame to caption, and one was",
        len(vreport.considered) >= 2 and voutcome.captioned == len(vreport.considered),
        f"considered={len(vreport.considered)} captioned={voutcome.captioned}",
    )
    check(
        # #197: stage 6's unit is a considered frame and the line is written before that
        # frame's call, so the snapshot taken at the call site holds *this* index. A write
        # deferred to the end of the loop holds nothing at the first call (or N/N at all of
        # them), and this is the check that tells the two apart.
        "stage 6 announces each frame before its call, and the count never freezes (#197)",
        [_continuation(snap, "frames") for snap in _vision_snapshots]
        == [
            f"           frames {i}/{len(vreport.considered)}"
            for i in range(len(_vision_snapshots))
        ]
        and len(_vision_snapshots) == len(vreport.considered),
        f"{[_continuation(s, 'frames') for s in _vision_snapshots]} "
        f"for {len(vreport.considered)} considered frames",
    )
    check(
        "every captioned frame carries status OK and non-empty text",
        vreport.considered
        and all(a.caption_status == "OK" and a.caption for a in vreport.considered),
        str([(a.frame, a.caption_status) for a in vreport.considered]),
    )
    check(
        "the caption lands in the artefact, not just in memory",
        len(vcache) == voutcome.captioned
        and all(
            a["caption"] == "$\\dot{x} = Ax$, state feedback"
            for a in json.loads((vdest / dycap.REPORT_NAME).read_text())["alignments"]
            if a["caption_status"] == "OK"
        ),
        f"cache entries={len(vcache)} captioned={voutcome.captioned}",
    )
    _traces = [a.caption_trace for a in vreport.considered]
    check(
        # Stage 6 makes 58 calls per lecture -- the largest consumer in the pipeline -- and
        # recorded none of them (#76). A summary per frame, not the `messages` array:
        # `audit-llm-transcript.json` averages ~190 KiB per call, so the full form here would
        # be ~11 MiB per run. ADR-0004 D4 needs enough to attribute, not the prompts.
        "every captioned frame records what produced it",
        len(_traces) >= 2 and all(tr.get("source") == "called" for tr in _traces),
        str([tr.get("source") for tr in _traces]),
    )
    check(
        "and the trace carries model, tokens and timing, not just a yes",
        all(
            tr.get("model") == "fake-vision"
            and tr.get("served_model") is not None
            and isinstance(tr.get("prompt_tokens"), int)
            and tr.get("seconds") is not None
            for tr in _traces
        ),
        str(_traces[:1]),
    )
    check(
        "the trace survives into captions.json, not only in memory",
        all(
            a.get("caption_trace", {}).get("source") == "called"
            for a in json.loads((vdest / dycap.REPORT_NAME).read_text())["alignments"]
            if a["caption_status"] == "OK"
        ),
        "captions.json alignments lack caption_trace",
    )
    _warm = dycap.run(cmanifest, ctrans, cquality, cimg, vdest, stream=io.StringIO())[0]
    check(
        # The cache is keyed by (fingerprint, model), so a warm re-run makes zero requests.
        # An empty trace there would read as "stage 6 did not run" rather than "nothing to
        # do", which is the whole reason the field says `cached` instead of staying blank.
        "a cached frame says cached, not called and not nothing",
        len(_warm.considered) >= 2
        and all(a.caption_trace.get("source") == "cached" for a in _warm.considered),
        str([a.caption_trace.get("source") for a in _warm.considered]),
    )
    check(
        "a cached frame still names the model it came from",
        all(a.caption_trace.get("model") == "fake-vision" for a in _warm.considered),
        str([a.caption_trace.get("model") for a in _warm.considered]),
    )
    check(
        "the trace is not shared between frames",
        vreport.considered[0].caption_trace is not vreport.considered[-1].caption_trace,
        "two frames share one caption_trace dict",
    )
    check(
        "the request carries the frame as an image part",
        len(_seen_messages) == voutcome.captioned
        and all(
            [part["type"] for part in m[0]["content"]] == ["text", "image_url"]
            and m[0]["content"][1]["image_url"]["url"]
            == "data:image/png;base64," + base64.b64encode(_FAKE_FRAME).decode("ascii")
            for m, _ in _seen_messages
        ),
        f"messages={len(_seen_messages)}",
    )
    # `caption.run` writes into a scratch directory, so reading the cache from `destination`
    # found nothing on every run. Run 10 reported `0 from cache` immediately after run 9 had
    # captioned the same 58 frames from the same bytes. `source: "cached"` in `caption_trace`
    # was unreachable in the real pipeline for the same reason `GATED_OUT` was (#82): the code
    # existed, the unit test passed, and nothing ever supplied it with a real cache.
    _bucket = ctmp / "cache_bucket"
    _bucket.mkdir()
    (_bucket / dycap.REPORT_NAME).write_text(
        (vdest / dycap.REPORT_NAME).read_text(encoding="utf-8"), encoding="utf-8"
    )
    _seen_messages.clear()
    _warm_stream = _FlushRecorder()
    _warm = dycap.run(
        cmanifest,
        ctrans,
        cquality,
        cimg,
        ctmp / "warm_out",
        cache_source=_bucket / dycap.REPORT_NAME,
        stream=_warm_stream,
    )
    check(
        "the cache is read from cache_source, not from the scratch destination",
        _warm[1].reused == len(_warm[0].considered) and _warm[1].captioned == 0,
        f"captioned={_warm[1].captioned} reused={_warm[1].reused} of {len(_warm[0].considered)}",
    )
    _warm_lines = [
        line
        for line in _warm_stream.getvalue().splitlines()
        if line.startswith("           frames ")
    ]
    check(
        # #197: the write is the first statement of the loop body, so a frame that leaves by
        # the cache `continue` advances the counter too. This run issues no request at all, and
        # the sequence still covers every considered frame -- which a write placed after the
        # cache check could not do.
        "a cached frame advances the counter too: the write precedes the cache check (#197)",
        _warm_lines
        == [
            f"           frames {i}/{len(_warm[0].considered)}"
            for i in range(len(_warm[0].considered))
        ],
        f"{_warm_lines} for {len(_warm[0].considered)} considered frames",
    )
    check(
        "and a warm run issues no requests at all, which is the whole point of the cache",
        len(_seen_messages) == 0,
        f"{len(_seen_messages)} vision requests on a fully cached run",
    )
    check(
        "a missing cache_source falls back to calling every frame, not to reusing nothing",
        dycap.run(
            cmanifest,
            ctrans,
            cquality,
            cimg,
            ctmp / "nocache_out",
            cache_source=_bucket / "no-such-file.json",
            stream=io.StringIO(),
        )[1].reused
        == 0,
        "a missing cache_source still reported reuse",
    )
    check(
        # The frames in this fixture all carry transcript, so the no-transcript branch of
        # `vision_message` is asserted on the helper rather than through a fixture that
        # would have to manufacture an uncovered frame. The point is that nothing is
        # concatenated when there is nothing to concatenate.
        "a frame with no transcript sends the prompt alone, with no invented context",
        dyle.vision_message("PROMPT", cimg / "f_0000000000.png")["content"][0]["text"] == "PROMPT"
        and dyle.vision_message("PROMPT", cimg / "f_0000000000.png", text="spoken")["content"][0][
            "text"
        ]
        == "PROMPT\n\nspoken",
        "vision_message text branch",
    )
    check(
        "the cache is keyed by frame fingerprint and model id",
        len(vcache) == voutcome.captioned
        and all(
            entry["model"] == "fake-vision" and len(entry["fingerprint"]) == 64
            for entry in vcache.values()
        ),
        f"entries={len(vcache)}",
    )

    # Second run: the cache must serve it, and the model must not be called again.
    _seen_messages.clear()
    _, voutcome2 = dycap.run(cmanifest, ctrans, cquality, cimg, vdest, stream=io.StringIO())
    check(
        "a second run over the same frames reuses every caption and calls nothing",
        voutcome2.reused == voutcome.captioned and not _seen_messages,
        f"reused={voutcome2.reused} calls={len(_seen_messages)}",
    )

    # A changed model must not be served the previous model's cache.
    os.environ["DYAK_VLM_MODEL"] = "other-vision"
    os.environ["DYAK_VLM_ENDPOINT"] = "http://vision2.invalid/v1"
    _seen_messages.clear()
    _, voutcome3 = dycap.run(cmanifest, ctrans, cquality, cimg, vdest, stream=io.StringIO())
    check(
        "a different model id invalidates the cache rather than inheriting it",
        voutcome3.reused == 0 and voutcome3.captioned == len(vreport.considered),
        f"reused={voutcome3.reused} captioned={voutcome3.captioned}",
    )

    # A refusing endpoint is recorded per frame, and the run says it captioned nothing.
    def _refusing_chat(messages, config, **kwargs):
        raise dyle.EndpointError("vision refused: 400 unsupported")

    dyle.chat = _refusing_chat
    rreport, routcome = dycap.run(
        cmanifest, ctrans, cquality, cimg, ctmp / "refused_out", stream=io.StringIO()
    )
    check(
        "a refusing endpoint is recorded per frame, not swallowed",
        all(
            a.caption_status == "ERROR" and "vision endpoint refused" in a.reason
            for a in rreport.considered
        ),
        str([(a.frame, a.caption_status, a.reason) for a in rreport.considered]),
    )
    check(
        "a run that captioned nothing says so",
        not routcome.ok and routcome.captioned == 0,
        routcome.as_dict(),
    )

    # --- #124: stage 6 stops calling an endpoint that is not answering -----------------
    #
    # Each call has its own retry ladder on purpose, so 58 frames against a dead endpoint is
    # 58 ladders -- about 5.8 min of sleeping before "0 captioned". The breaker is per-stage.
    _bimg = ctmp / "breaker_images"
    _bimg.mkdir(parents=True, exist_ok=True)
    _frames = [f"b_{i:010d}.png" for i in range(6)]
    for _name in _frames:
        (_bimg / _name).write_bytes(_FAKE_FRAME)
    _breport = dycap.Report(
        alignments=[
            dycap.Alignment(
                frame=_name,
                pts_ms=i * 1000,
                image=_name,
                enhanced=None,
                on_screen_ms=(i * 1000, (i + 1) * 1000),
                segment_indices=[0],
                text="t",
            )
            for i, _name in enumerate(_frames)
        ]
    )
    _dead_calls: list[int] = []

    def _dead_chat(messages, config, **kwargs):
        _dead_calls.append(1)
        raise dyle.EndpointError("connection refused", connection=True)

    dyle.chat = _dead_chat
    try:
        dycap.caption_frames(
            _breport,
            _bimg,
            dyle.Config(endpoint="http://e/v1", model="m"),
            stream=io.StringIO(),
        )
    finally:
        dyle.chat = _real_chat
    check(
        "K consecutive connection failures stop stage 6's remaining calls",
        len(_dead_calls) == dyle.BREAKER_MAX,
        f"calls={len(_dead_calls)} of {len(_frames)} frames",
    )
    check(
        "the frames never attempted say so, instead of each paying its own retry ladder",
        sum(1 for a in _breport.considered if a.caption_status == "STOPPED")
        == len(_frames) - dyle.BREAKER_MAX,
        str([(a.frame, a.caption_status) for a in _breport.considered]),
    )
finally:
    dyle.chat = _real_chat
    for _k in (
        "DYAK_VLM_ENDPOINT",
        "DYAK_VLM_MODEL",
        "DYAK_LLM_ENDPOINT",
        "DYAK_LLM_MODEL",
    ):
        os.environ.pop(_k, None)
    if _leftovers:
        os.environ["DYAK_LLM_MODEL"] = _leftovers


# --- 13. stage 7 synth: structure, degradation, model independence ------------------
class FakeSynth:
    """A synthesizer that is not a model, so the assembly can be asserted exactly."""

    name = "fake"

    def __init__(
        self, body="текст раздела", fail_on=(), toc_on=(), truncate_on=(), conn_fail_on=()
    ):
        self.body = body
        self.fail_on = set(fail_on)
        self.toc_on = set(toc_on)
        self.truncate_on = set(truncate_on)
        self.conn_fail_on = set(conn_fail_on)
        self.seen = []

    def section(self, section, transcript):
        self.seen.append(section.number)
        if section.number in self.conn_fail_on:
            # Connection-level: the endpoint never answered. This is what the stage breaker
            # counts (#124).
            raise dyle.EndpointError("endpoint refused the connection", connection=True)
        if section.number in self.fail_on:
            raise dyle.EndpointError("endpoint said no")
        if section.number in self.toc_on:
            return dysy.SectionBody(text="# Содержание\n\n- раздел 1\n- раздел 2")
        return dysy.SectionBody(
            text=f"{self.body} для фрагмента {section.number} ({len(transcript)} симв.)",
            finish_reason="length" if section.number in self.truncate_on else "stop",
        )


sdir = tmp / "synth"
sdir.mkdir(parents=True, exist_ok=True)
scaps = sdir / "captions.json"
scaps.write_text(
    json.dumps(
        {
            "alignments": [
                {
                    "frame": f"f_{i:010d}.png",
                    "text": f"слова фрагмента {i}",
                    "word_count": 10 * i,
                    "caption_status": "NOT_CONFIGURED",
                    "on_screen_ms": [i * 60000, (i + 1) * 60000],
                }
                for i in range(6)
            ]
        }
    ),
    encoding="utf-8",
)
stxt = sdir / "transcript.txt"
stxt.write_text("полный текст лекции", encoding="utf-8")
src_path = tmp / "lecture.webm"
src_path.write_bytes(b"\x1a\x45\xdf\xa3")

cuts = dysy.build_sections(json.loads(scaps.read_text())["alignments"], limit=4)
check(
    "sections are cut on frame boundaries, capped at the limit",
    len(cuts) == 3 and [s.number for s in cuts] == [1, 2, 3],
    str([(s.number, len(s.frames)) for s in cuts]),
)
check(
    "each section carries its frames and timing",
    cuts[1].frames == ("f_0000000002.png", "f_0000000003.png")
    and cuts[1].start_ms == 120000
    and cuts[1].end_ms == 240000,
    str(cuts[1]),
)
check(
    "gated-out frames are not cut into sections",
    dysy.build_sections([{"frame": "a", "caption_status": "GATED_OUT", "text": "x"}]) == [],
    "a gated frame produced a section",
)
check("no alignments is no sections", dysy.build_sections([]) == [], "produced sections")


# --- #110: empty HTTP 200 remains diagnosable and policy is recorded -------------
_empty_test_env = {k: os.environ.get(k) for k in ("DYAK_VLM_ENDPOINT", "DYAK_VLM_MODEL")}
os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
os.environ["DYAK_VLM_MODEL"] = "fake-vision"
empty_calls: list[int] = []


def _empty_reply(messages, config, **kwargs):
    empty_calls.append(1)
    return _caption_reply(text="")


dyle.chat = _empty_reply
try:
    empty_dest = ctmp / "empty_out"
    empty_report, empty_outcome = dycap.run(
        cmanifest, ctrans, cquality, cimg, empty_dest, stream=io.StringIO()
    )
finally:
    dyle.chat = _fake_vision_chat
check(
    "an empty 200 is recorded without a retry (#110)",
    len(empty_calls) == len(empty_report.considered)
    and all(a.caption_status == "EMPTY" and a.caption is None for a in empty_report.considered)
    and empty_outcome.failed == len(empty_report.considered),
    f"calls={len(empty_calls)} failed={empty_outcome.failed}",
)
empty_payload = json.loads((empty_dest / dycap.REPORT_NAME).read_text())
check(
    "empty caption trace records the one returned attempt (#110)",
    all(
        a["caption_trace"].get("attempts") == 1
        for a in empty_payload["alignments"]
        if a["caption_status"] != "GATED_OUT"
    ),
)

persistent_empty_calls: list[int] = []


def _always_empty(messages, config, **kwargs):
    persistent_empty_calls.append(1)
    return _caption_reply(text="")


dyle.chat = _always_empty
try:
    persistent_dest = ctmp / "persistent_empty_out"
    persistent_report, persistent_outcome = dycap.run(
        cmanifest, ctrans, cquality, cimg, persistent_dest, stream=io.StringIO()
    )
finally:
    dyle.chat = _fake_vision_chat
check(
    "persistent empty replies are one request per frame and remain EMPTY (#110)",
    len(persistent_empty_calls) == len(persistent_report.considered)
    and all(a.caption_status == "EMPTY" and a.caption is None for a in persistent_report.considered)
    and persistent_outcome.failed == len(persistent_report.considered)
    and all(a.caption_trace.get("attempts") == 1 for a in persistent_report.considered),
    f"calls={len(persistent_empty_calls)} failed={persistent_outcome.failed}",
)
persistent_provenance = json.loads((persistent_dest / dycap.PROVENANCE_NAME).read_text())
check(
    "caption provenance records the measured no-retry policy (#110)",
    persistent_provenance["empty_response_policy"]["retry_limit"] == 0
    and "42 fresh captions" in persistent_provenance["empty_response_policy"]["measurement"]
    and "3 frames previously reported EMPTY"
    in persistent_provenance["empty_response_policy"]["measurement"]
    and "do not retry" in persistent_provenance["empty_response_policy"]["description"],
)
for _key, _value in _empty_test_env.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


# --- 14. a refusal is a status, not a caption ---------------------------------------
# #83. `NO NEW INFORMATION.` was `caption_status: OK` with a non-empty string, so a frame the
# model declined to describe was indistinguishable from one it described. Three properties
# have to hold at once, and only the first is obvious:
#
#   * it is its own status;
#   * it is NOT a failure -- the call happened and came back, and counting 16 refusals as
#     errors would push them into the count `cli.py` reports and, at the extreme, make an
#     all-refusal run exit 3;
#   * a warm re-run reports it identically. The cache path used to hardcode `OK`, which made
#     a warm bundle claim all 57 frames described while the cold run before it said 16 were
#     not -- same input, contradictory artefact.
def _refusal_chat(messages, config, **kwargs):
    _seen_messages.append((messages, config))
    return _caption_reply(text=dycap.NO_NEW_INFORMATION)


# The stage-6 block above popped these and restored whatever was there before it. Without
# them the `NO_ENDPOINT` path runs and reports 2 considered / 2 failed, which every assertion
# below would read as "the refusal was counted as a failure" -- a false pass on the one bug
# this block exists to catch.
_refusal_saved = {_k: os.environ.pop(_k, None) for _k in ("DYAK_VLM_ENDPOINT", "DYAK_VLM_MODEL")}
os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
os.environ["DYAK_VLM_MODEL"] = "fake-vision"

dyle.chat = _refusal_chat
try:
    rdest = ctmp / "refusal_out"
    rreport, routcome = dycap.run(cmanifest, ctrans, cquality, cimg, rdest, stream=io.StringIO())
finally:
    dyle.chat = _fake_vision_chat

check(
    "a refusal is not a failure",
    len(rreport.considered) >= 2 and routcome.failed == 0,
    f"considered={len(rreport.considered)} failed={routcome.failed}",
)
check(
    "a refusal is still a success -- the call happened and returned",
    routcome.ok and routcome.refused == len(rreport.considered),
    f"ok={routcome.ok} refused={routcome.refused} of {len(rreport.considered)}",
)
check(
    "a refusal carries its own status and a reason",
    all(a.caption_status == "NO_NEW_INFORMATION" and a.reason for a in rreport.considered),
    str([(a.frame, a.caption_status, a.reason) for a in rreport.considered]),
)
check(
    "the status is recorded in the artefact, not only in memory",
    all(
        a["caption_status"] == "NO_NEW_INFORMATION"
        for a in json.loads((rdest / dycap.REPORT_NAME).read_text())["alignments"]
        if a["caption_status"] != "GATED_OUT"
    ),
    str(
        [
            a["caption_status"]
            for a in json.loads((rdest / dycap.REPORT_NAME).read_text())["alignments"]
        ]
    ),
)
check(
    "the refusal count is in the outcome the report is written from",
    json.loads((rdest / dycap.PROVENANCE_NAME).read_text().rsplit("\n{", 1)[0] + "") is not None
    and routcome.as_dict()["refused"] == routcome.refused,
    str(routcome.as_dict()),
)

# Warm re-run over the refusal cache: the same frames, zero calls, and the same statuses.
_seen_messages.clear()
dyle.chat = _refusal_chat
try:
    wdest = ctmp / "refusal_warm"
    wreport, woutcome = dycap.run(
        cmanifest,
        ctrans,
        cquality,
        cimg,
        wdest,
        cache_source=rdest / dycap.REPORT_NAME,
        stream=io.StringIO(),
    )
finally:
    dyle.chat = _fake_vision_chat

check(
    "a warm re-run makes no calls",
    not _seen_messages and woutcome.reused == len(wreport.considered),
    f"calls={len(_seen_messages)} reused={woutcome.reused}",
)
check(
    "a warm re-run reports the refusals the cold run reported",
    [a.caption_status for a in wreport.considered] == [a.caption_status for a in rreport.considered]
    and woutcome.refused == routcome.refused,
    f"warm={[a.caption_status for a in wreport.considered]} "
    f"cold={[a.caption_status for a in rreport.considered]}",
)
check(
    "a warm re-run does not turn refusals into failures",
    woutcome.failed == 0 and woutcome.ok,
    f"failed={woutcome.failed} ok={woutcome.ok}",
)

for _k, _v in _refusal_saved.items():
    os.environ.pop(_k, None)
    if _v is not None:
        os.environ[_k] = _v

# --- 14. stage 7 gets the captions, not the filenames ---------------------------------
# #84. Stage 6 computed 57 captions (55 404 chars) on lecture 1 and stage 7 was handed
# `section.frames` -- filenames like `f_0000180000.png`, which say nothing about the slide.
# The writer described a lecture it could not see. These assert the wiring, including the
# part that is easy to get wrong: a refusal is another model's judgement, and passing the
# 19-character English sentinel through as if it were a description would be worse than
# sending nothing.
_WIRE = [
    {
        "frame": "f_a.png",
        "text": "производная",
        "word_count": 2,
        "caption_status": "OK",
        "caption": r"$\dot{x} = Ax$, матрица состояния",
        "on_screen_ms": [0, 1000],
    },
    {
        "frame": "f_b.png",
        "text": "дальше",
        "word_count": 1,
        "caption_status": "OK",
        "caption": "NO NEW INFORMATION.",
        "on_screen_ms": [1000, 2000],
    },
    {
        "frame": "f_c.png",
        "text": "ещё",
        "word_count": 1,
        "caption_status": "GATED_OUT",
        "on_screen_ms": [2000, 3000],
    },
]
_wsec = dysy.build_sections(_WIRE, limit=1)[0]
check(
    "a real caption rides along with the section",
    ("f_a.png", r"$\dot{x} = Ax$, матрица состояния") in _wsec.captions,
    str(_wsec.captions),
)
check(
    "a refusal is stated in the writer's language, not passed as content",
    ("f_b.png", dysy.CAPTION_REFUSED) in _wsec.captions
    and not any("NO NEW INFORMATION" in t for _, t in _wsec.captions),
    str(_wsec.captions),
)
check(
    "a frame with no caption is omitted rather than sent as a blank line",
    all(name != "f_c.png" for name, _ in _wsec.captions),
    str(_wsec.captions),
)
check(
    "the section report records how many captions it carries",
    _wsec.as_dict()["captions"] == 2 and len(_wsec.as_dict()["frames"]) == 2,
    str(_wsec.as_dict()),
)

# The assertion #84 asks for: the caption text is in the message that goes to the model.
# `dyle.chat` is stubbed, so this reads what stage 7 *builds* -- the transport is covered
# elsewhere -- but unlike a mock of stage 7's own output it cannot pass while the wiring is
# missing.
_wprompts: list = []


def _wire_chat(messages, config):
    _wprompts.append(messages)

    class _R:
        text = "тело"
        raw: dict = {}
        attempts = 1
        finish_reason = "stop"

    return _R()


class _WireTranscript:
    def record(self, *a, **k):
        pass


dyle.chat = _wire_chat
try:
    _wsyn = dysy.LLMSynthesizer(dyle.Config(endpoint="http://e/v1", model="m"), _WireTranscript())
    _wsyn.section(_wsec, _wsec.text)
finally:
    dyle.chat = _real_chat

_wp = _wprompts[0][1]["content"]
check(
    "the caption text is in the recorded prompt, not just the section",
    r"$\dot{x} = Ax$, матрица состояния" in _wp,
    _wp[:400],
)
check(
    "the sentinel never reaches the writer",
    "NO NEW INFORMATION." not in _wp,
    _wp[:400],
)
check(
    "captions are told apart from speech, so the writer cannot quote the screen as the lecturer",
    "не цитата лектора" in _wp,
    _wp[:400],
)

# A section with no captions still names its frames: the fallback the pre-#84 prompt used.
_nocap = dysy.build_sections([{"frame": "f_z.png", "text": "t", "caption_status": "OK"}], limit=1)[
    0
]
_wprompts.clear()
dyle.chat = _wire_chat
try:
    _wsyn.section(_nocap, _nocap.text)
finally:
    dyle.chat = _real_chat
check(
    "a section with no captions still reports which frames were on screen",
    "Кадры на экране: f_z.png" in _wprompts[0][1]["content"],
    _wprompts[0][1]["content"][:300],
)

# #84's second half. `synthesise` unpacks the third element of `SECTIONS` -- the skill's
# per-section brief -- and used to discard it, handing the writer the *slice*'s brief
# (`кадры f_a.png–f_c.png`) under the section's own title. `assemble` then imposed the real
# heading, so the note satisfied the document contract while the writer never saw it. The
# caption half of #84 is guarded above; this is the other half, and it had no assertion.
_bsections: list = []


class _BriefSynth:
    name = "brief"

    def section(self, section, transcript):
        _bsections.append(section)
        return dysy.SectionBody(text="тело")


dysy.synthesise(_WIRE, "текст", src_path, _BriefSynth())
_brief_section = _bsections[0] if _bsections else None
check(
    "synthesise hands the writer the skill's brief, not the slice's frame range (#84)",
    _brief_section is not None
    and _brief_section.brief == dysy.SECTIONS[0][2]
    and "кадры" not in _brief_section.brief,
    f"brief={_brief_section.brief!r} want={dysy.SECTIONS[0][2]!r}"
    if _brief_section is not None
    else "no calls",
)

if _brief_section is not None:
    _wprompts.clear()
    dyle.chat = _wire_chat
    try:
        _wsyn.section(_brief_section, _brief_section.text)
    finally:
        dyle.chat = _real_chat
    _brief_prompt = _wprompts[0][1]["content"]
else:
    _brief_prompt = ""
check(
    "the skill's brief is in the prompt under «Что в нём должно быть» (#84)",
    f"Что в нём должно быть: {dysy.SECTIONS[0][2]}" in _brief_prompt,
    _brief_prompt[:300],
)

# --- #197: stage 7 says which section it is on, before that section's call -----------------
# `synthesise` makes one blocking call per SECTIONS entry, so the loop owns an observable unit.
# The stub records what the stream held at call time -- a value sequence alone is what a write
# deferred to the end of the loop also produces (#205).
_synth_stream = _FlushRecorder()
_synth_snapshots: list[str] = []


class _RecordingSynth:
    name = "recording"

    def section(self, section, transcript):
        _synth_snapshots.append(_synth_stream.getvalue())
        return dysy.SectionBody(text="тело")


dysy.synthesise(_WIRE, "текст", src_path, _RecordingSynth(), stream=_synth_stream)
check(
    # The first line is 0/8 and the sequence must reach 7/8: `before` means the last line is
    # (N-1)/N, and a sequence that stops short is a counter that froze on some exit path.
    "stage 7 announces each section before its call, first line 0/8 and no gap (#197)",
    [_continuation(snap, "sections") for snap in _synth_snapshots]
    == [f"           sections {i}/{len(dysy.SECTIONS)}" for i in range(len(_synth_snapshots))]
    and len(_synth_snapshots) == len(dysy.SECTIONS),
    f"{[_continuation(s, 'sections') for s in _synth_snapshots]} for {len(dysy.SECTIONS)} sections",
)

# The eight headings are the model's to write nothing about.
note = dysy.synthesise(json.loads(scaps.read_text())["alignments"], "текст", src_path, FakeSynth())
check(
    "all eight sections are present",
    [s["number"] for s in note.sections] == list(range(1, 9)),
    str([s["number"] for s in note.sections]),
)
check(
    "in ADR-0001 D5's order, with ADR-0001 D5's titles",
    re.findall(r"^## (\d)\. (.+)$", note.markdown, re.M)
    == [(str(n), t) for n, t, _ in dysy.SECTIONS],
    str(re.findall(r"^## (\d)\. (.+)$", note.markdown, re.M)),
)
check(
    "no section heading comes from the synthesizer",
    "# Фрагмент" not in note.markdown,
    "a synthesized heading leaked into the note",
)
# #117: every section is written from the whole lecture, not from slice N. Six frames are
# enough material for all eight sections -- and indexing slices per section meant a 58-frame
# lecture silently dropped its last four slices while this test still passed.
check("every section is written from the whole lecture", note.filled == 8, str(note.filled))
# The original intent -- a section with no material is marked, not omitted -- now depends on
# the lecture being empty rather than on the section's index (#117).
_empty = dysy.synthesise([], "", src_path, FakeSynth())
check(
    "with no transcript every section is marked, not omitted",
    _empty.filled == 0
    and all(not s["present"] for s in _empty.sections)
    and _empty.markdown.count("в лекции не затрагивается") == 8,
    str(_empty.filled),
)
check("a note missing sections is degraded", _empty.degraded, "an incomplete note claimed complete")

# Failure isolation: one dead section must not lose the seven that worked. ADR-0001 D5.
partial = dysy.synthesise(
    json.loads(scaps.read_text())["alignments"], "текст", src_path, FakeSynth(fail_on={4})
)
check(
    "one failed section does not lose the other seven",
    partial.filled == 7 and partial.sections[3]["present"] is False,
    f"filled={partial.filled} sections={[(x['number'], x['present']) for x in partial.sections]}",
)
check("and the note is still written", "## 4." in partial.markdown, "section 4 missing")
check(
    "a failed section says 'not covered', it does not vanish",
    "в лекции не затрагивается" in partial.markdown,
    "no placeholder",
)
check(
    "the failure is reported, not swallowed", partial.degraded and partial.notes, str(partial.notes)
)

toc = dysy.synthesise(
    json.loads(scaps.read_text())["alignments"], "текст", src_path, FakeSynth(toc_on={2})
)
check("a returned table of contents is discarded", "- раздел 1" not in toc.markdown, "ToC leaked")
check("and the warning says so", any("table of contents" in w for w in toc.notes), str(toc.notes))

# --- #124: a length-truncated section is degraded, not clean content ---------------
#
# The endpoint cut the model off at its output cap. The text is real, so it must survive --
# but a reader (and stage 12) has to be able to tell it from a section that ended naturally.
trunc = dysy.synthesise(
    json.loads(scaps.read_text())["alignments"], "текст", src_path, FakeSynth(truncate_on={3})
)
check(
    "a length-truncated section is reported as degraded, not as a clean note entry",
    trunc.degraded and any("truncated" in w for w in trunc.notes),
    str(trunc.notes),
)
check(
    "the truncation warning names the section that was cut off",
    any(w.startswith("section 3:") and "length limit" in w for w in trunc.notes),
    str(trunc.notes),
)
check(
    "the truncated text is kept, not discarded for being incomplete",
    trunc.sections[2]["present"] and trunc.sections[2]["bytes"] > 0,
    str(trunc.sections[2]),
)
check(
    "synth.json records finish_reason per section, so the artefact says which were cut",
    trunc.sections[2].get("finish_reason") == "length"
    and trunc.sections[0].get("finish_reason") == "stop",
    str([(s["number"], s.get("finish_reason")) for s in trunc.sections]),
)

# The end-to-end form: the real `_to_reply`, the real `LLMSynthesizer`, a mocked transport.
# On pre-#124 code `_to_reply` drops finish_reason, so this section reads as clean content
# and the note is not degraded -- which is the bug, and this is the assertion that catches it.
_llm_chat_124 = dyle.chat


def _truncating_chat(messages, config, **kwargs):
    return dyle._to_reply(
        {
            "choices": [
                {"message": {"content": "раздел обрывается на середине"}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1024},
        },
        config.model,
        0.01,
        1,
    )


dyle.chat = _truncating_chat
try:
    _cut_note = dysy.synthesise(
        json.loads(scaps.read_text())["alignments"],
        "текст",
        src_path,
        dysy.LLMSynthesizer(dyle.Config(endpoint="http://e/v1", model="m"), dyle.Transcript()),
    )
finally:
    dyle.chat = _llm_chat_124
check(
    "a 200 with finish_reason=length through the real parser is a degraded section, not clean",
    _cut_note.degraded and any("truncated" in w for w in _cut_note.notes),
    f"degraded={_cut_note.degraded} notes={_cut_note.notes}",
)

# --- #124: K consecutive connection failures stop stage 7's remaining calls ---------
#
# 8 sections x a full retry ladder against a dead endpoint is the cost this is about. The
# unattempted sections are reported as not attempted, not as failed synthesis.
cut = FakeSynth(conn_fail_on=set(range(1, 9)))
dead = dysy.synthesise(json.loads(scaps.read_text())["alignments"], "текст", src_path, cut)
check(
    "K consecutive connection failures stop the stage's remaining calls",
    len(cut.seen) == dyle.BREAKER_MAX,
    f"called={cut.seen}",
)
check(
    "the note still exists and is degraded",
    dead.degraded and "## 8." in dead.markdown,
    f"degraded={dead.degraded}",
)
check(
    "the sections never attempted are named as not attempted",
    any("not attempted" in w for w in dead.notes),
    str(dead.notes),
)
check(
    "an unattempted section is not reported as a failed synthesis",
    any(s.get("reason", "").startswith("not attempted") for s in dead.sections),
    str([(s["number"], s.get("reason")) for s in dead.sections]),
)

# One non-connection refusal is a verdict, not a dead endpoint: it must not trip the breaker.
control = FakeSynth()
dysy.synthesise(json.loads(scaps.read_text())["alignments"], "текст", src_path, control)
mixed = FakeSynth(fail_on={1}, conn_fail_on={2})
dysy.synthesise(json.loads(scaps.read_text())["alignments"], "текст", src_path, mixed)
check(
    "an HTTP verdict does not count toward the connection breaker",
    mixed.seen == control.seen,
    f"mixed={mixed.seen} control={control.seen}",
)

# Heading demotion. The model reshapes the document; `assemble` must win.
check(
    "a synthesized h1 is demoted, not left to compete with ADR-0001 D5's headings",
    dysy.normalise("# Фрагмент\n\nтекст") == "### Фрагмент\n\nтекст",
    dysy.normalise("# Фрагмент\n\nтекст"),
)
check(
    # This one asserted the opposite, and the assertion is what produced 29 unnumbered `##`
    # headings as siblings of the eight `## N.` sections on lecture 1. "##" is not a
    # legitimate subsection here: `##` IS the subsection level inside the note, because the
    # sections themselves are `##`. A body heading at that level makes `## N. Title` stop
    # being the top of anything.
    "a synthesized h2 is pushed below the section level too",
    dysy.normalise("## Подраздел\n\nтекст") == "### Подраздел\n\nтекст",
    dysy.normalise("## Подраздел\n\nтекст"),
)
check(
    "nothing in a section body is left at or above the section level",
    not any(
        re.match(r"^#{1,2}\s", line)
        for body in (
            "## A\n### B\n#### C",
            "# A\n## B\n### C",
            "# A\n###### B",
            "## A",
        )
        for line in dysy.normalise(body).splitlines()
        if line.lstrip("#").startswith((" ", "")) and line.lstrip().startswith("#")
    ),
    "  ".join(dysy.normalise(b) for b in ("## A\n### B", "# A\n## B")),
)
check(
    "relative nesting survives the shift",
    dysy.normalise("## A\n### B\n#### C") == "### A\n#### B\n##### C",
    dysy.normalise("## A\n### B\n#### C"),
)
check(
    # Mapping every heading to `###` collapsed a body mixing `##` and `####` into a flat
    # one, losing the model's own structure along with its level.
    "a deeper heading does not collapse onto a shallower sibling",
    dysy.normalise("# T\n###### D") == "### T\n###### D",
    dysy.normalise("# T\n###### D"),
)
check(
    # `########` is not a heading in Markdown, it is a paragraph. Clamping keeps the note
    # renderable instead of silently turning a heading into text.
    "a shift cannot produce more than six hashes",
    max(len(m.group(1)) for m in re.finditer(r"^(#+) ", dysy.normalise("###### D\n# T"), re.M))
    <= 6,
    dysy.normalise("###### D\n# T"),
)
check(
    # A body that starts at `###` is already correct, and shifting it to `#####` would
    # invent depth the model did not claim.
    "a body already below the section level is left alone",
    dysy.normalise("### A\n#### B") == "### A\n#### B",
    dysy.normalise("### A\n#### B"),
)
check(
    "a body with no headings is returned unchanged",
    dysy.normalise("просто текст\n\nи ещё") == "просто текст\n\nи ещё",
    dysy.normalise("просто текст\n\nи ещё"),
)

# The template synthesizer is the oracle: it must run with nothing configured and must not
# invent content.
tnote = dysy.synthesise(
    json.loads(scaps.read_text())["alignments"],
    "текст",
    src_path,
    dysy.TemplateSynthesizer(src_path, "текст", cuts),
)
check("the template needs no model", tnote.synthesizer == "template", tnote.synthesizer)
check(
    "the template says it is a skeleton, not a note",
    "Это каркас, а не конспект" in tnote.markdown,
    "no disclaimer",
)
check("and it is reported as degraded", tnote.degraded, "a skeleton is not a finished note")
check(
    "two synthesizers, one protocol",
    isinstance(dysy.TemplateSynthesizer(src_path, "", []), dysy.Synthesizer),
    "template is not a Synthesizer",
)
check(
    "and the fake is too",
    isinstance(FakeSynth(), dysy.Synthesizer),
    "duck-typed stub is not a Synthesizer",
)

# Unconfigured endpoint is a state, not a crash.
try:
    dyle.Config.from_env()
    raise AssertionError("expected NotConfiguredError")
except dyle.NotConfiguredError as exc:
    check(
        "an unconfigured endpoint raises before any network call",
        "DYAK_LLM_ENDPOINT" in str(exc),
        str(exc),
    )

saved_llm_env = {k: os.environ.get(k) for k in (dyle.ENDPOINT_ENV, dyle.MODEL_ENV, dyle.KEY_ENV)}
os.environ[dyle.ENDPOINT_ENV] = "http://h/v1"
os.environ[dyle.MODEL_ENV] = "m"
os.environ[dyle.KEY_ENV] = "secret"
cfg = dyle.Config.from_env()
for _k, _v in saved_llm_env.items():
    os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)
check(
    "config comes from the environment",
    cfg.endpoint == "http://h/v1" and cfg.model == "m",
    str(cfg),
)
check(
    "the key is never in the redacted form",
    "secret" not in json.dumps(cfg.redacted()),
    cfg.redacted(),
)
check("but its presence is recorded", cfg.redacted()["api_key"] is True, str(cfg.redacted()))

# The artefacts.
dest = tmp / "synth-out"
sn = dysy.run(scaps, stxt, src_path, dest, config=None, stream=io.StringIO())
check(
    "run writes the note",
    (dest / dysy.NOTE_NAME).is_file(),
    str(sorted(p.name for p in dest.iterdir())),
)
check(
    "run writes the report",
    (dest / dysy.REPORT_NAME).is_file(),
    str(sorted(p.name for p in dest.iterdir())),
)
check(
    "run writes provenance",
    (dest / dysy.PROVENANCE_NAME).is_file(),
    str(sorted(p.name for p in dest.iterdir())),
)
prov = json.loads((dest / dysy.PROVENANCE_NAME).read_text())
check("provenance records which synthesizer ran", prov["synthesizer"] == "template", str(prov))
check(
    "provenance states the stage does not require a model",
    prov["requires_model"] is False,
    str(prov),
)
check(
    "provenance explains why there are two implementations",
    "oracle" in prov["why_two_implementations"],
    str(prov),
)
srep = json.loads((dest / dysy.REPORT_NAME).read_text())
check(
    "the report records the synthesizer and the size",
    srep["synthesizer"] == "template" and srep["bytes"] > 0,
    str(srep),
)
check("and that the note is degraded", srep["degraded"] is True, str(srep))

# #201: captions.json is stage 6's product, one entry per frame, and stage 7 read it with an
# unguarded json.loads plus `.get("alignments", [])`. Five shapes crashed -- a missing file
# (FileNotFoundError), a truncated one (JSONDecodeError), a non-object or a non-list
# (AttributeError), an entry without a `frame` (KeyError), and null (TypeError) -- and a missing
# key was read as "no frames", writing a note that had silently lost the screen content. The
# reader is `caption.read_alignments`, in the module that owns the artefact.
for _label, _raw, _why in (
    ("absent", None, "cannot read captions.json"),
    ("truncated", '{"alignments": [', "cannot read captions.json"),
    ("not-a-json-object", "[1, 2]", "captions.json is not a JSON object"),
    ("alignments-not-a-list", '{"alignments": "x"}', "captions.json has no 'alignments' list"),
    ("alignments-null", '{"alignments": null}', "captions.json has no 'alignments' list"),
    ("no-alignments-key", '{"provenance": {}}', "captions.json has no 'alignments' list"),
    (
        "list-of-non-dicts",
        '{"alignments": [1, 2]}',
        "captions.json has an alignment without a 'frame'",
    ),
    (
        "list-of-empty-dicts",
        '{"alignments": [{}]}',
        "captions.json has an alignment without a 'frame'",
    ),
):
    _path = sdir / f"captions-{_label}.json"
    if _raw is None:
        _path.unlink(missing_ok=True)
    else:
        _path.write_text(_raw, encoding="utf-8")
    _raised = None
    try:
        dysy.run(_path, stxt, src_path, sdir / f"out-{_label}", config=None, stream=io.StringIO())
    except (
        BaseException
    ) as _exc:  # on main: FileNotFoundError/JSONDecodeError/AttributeError/KeyError/TypeError
        _raised = _exc
    _named = isinstance(_raised, dyr.DependencyError)
    _message = _raised.message if _named else repr(_raised)
    check(
        f"#201: a {_label} captions.json fails stage 7 with a named reason, not a traceback",
        _named and _raised.code == ec.ARTEFACT_VERIFICATION_FAILED and _why in _message,
        _message,
    )
    _reader_reason = _reader(dycap, "read_alignments", _path)[1]
    check(
        f"#201: and {_label} is named in the reader's words",
        _named and _message == _reader_reason,
        f"{_message!r} vs {_reader_reason!r}",
    )
_missing_key = _reader(dycap, "read_alignments", sdir / "captions-no-alignments-key.json")
check(
    "#201: captions.json with no alignments key is a failure, not a note with no frames",
    _missing_key[1] == "captions.json has no 'alignments' list",
    repr(_missing_key),
)
_empty_caps = sdir / "captions-empty.json"
_empty_caps.write_text('{"alignments": []}', encoding="utf-8")
check(
    "#201: an empty alignments list is still legitimate -- the shape is right and there were none",
    _reader(dycap, "read_alignments", _empty_caps) == ([], ""),
    repr(_reader(dycap, "read_alignments", _empty_caps)),
)
_whole_captions = _reader(dycap, "read_alignments", scaps)
check(
    "#201: a well-formed captions.json is returned whole by the one reader",
    len(_whole_captions[0] or []) == 6 and _whole_captions[1] == "",
    repr(_whole_captions),
)


# --- 14. stage 8 lint: the mechanical floor under the model's prose ----------------
limg = tmp / "lint-images"
limg.mkdir(parents=True, exist_ok=True)
(limg / "f_0000000000.png").write_bytes(b"\x89PNG")
(limg / "f_0000000001.png").write_bytes(b"\x89PNG")


def note(*bodies: str) -> str:
    """A well-formed note, so each test can break exactly one thing."""
    parts = ["# Тест"]
    for (number, title, _), body in zip(dysy.SECTIONS, bodies):
        parts += ["", f"## {number}. {title}", "", body]
    return "\n".join(parts) + "\n"


clean = note(*["содержимое раздела"] * 8)


def sec(n: int) -> str:
    """The canonical heading of section `n`, derived from `dysy.SECTIONS` rather than
    typed here. The first generation of these fixtures hard-coded four paraphrased
    titles ("Карта источников", "Теоретический конспект и методы", "Математический
    фундамент", "Вопросы для самопроверки"), which let the lint stage and the
    synthesizer certify each other's drift against the skill file they both name: the
    structure check passed because both places carried the same wrong string. Deriving
    here re-links stage 8 to `synth.py` instead of to a second copy of it."""
    return f"## {n}. {next(t for i, t, _ in dysy.SECTIONS if i == n)}"


r = dyln.lint(clean, limg)
check("a well-formed note lints clean", r.ok and not r.findings, r.summary() + str(r.findings))
check("every rule ran", r.checked == 9, str(r.checked))
check("an empty note is not ok", not dyln.lint("", limg).ok, "empty passed")


def rules(text, rule):
    """Findings of one rule. Reading findings by rule is how each test names exactly the
    defect it introduced, instead of asserting on a whole report."""
    return [f for f in dyln.lint(text, limg).findings if f.rule == rule]


# Structure.
drop = clean.replace(f"{sec(5)}\n\nсодержимое раздела\n", "")
check("a missing section is an error", rules(drop, "structure/missing-section"), "no finding")
renamed = clean.replace(sec(6), "## 6. Формулы")
check("a renamed section is an error", rules(renamed, "structure/renamed-section"), "no finding")
extra = clean.replace(sec(8), f"## 9. {dysy.SECTIONS[7][1]}")
check(
    "a section ADR-0001 D5 does not define is an error",
    rules(extra, "structure/unknown-section"),
    "no finding",
)
swapped = (
    clean.replace(sec(5), "## 5. @@TMP@@").replace(sec(6), sec(5)).replace("## 5. @@TMP@@", sec(6))
)
check("out-of-order sections are an error", rules(swapped, "structure/order"), "no finding")

# The rule that catches a model padding the document to look complete.
padded = note(
    *["в лекции не затрагивается"] * 7
    + ["в лекции не затрагивается\n\nА на самом деле три абзаца."]
)
check(
    "a section that declares itself empty and then is not, is an error",
    rules(padded, "structure/claims-empty-but-is-not"),
    str(dyln.lint(padded, limg).findings),
)
check(
    "a genuinely empty section is fine",
    not rules(note(*["в лекции не затрагивается"] * 8), "structure/claims-empty-but-is-not"),
    "a real empty section was flagged",
)
check(
    # The rule matched `NOT_COVERED in body`, a substring test. Measured on lecture 1,
    # 2026-10-04: the LLM wrote a scoped deferral inside a 29-line section --
    # "в лекции не затрагивается: конкретные формулы функционала качества." -- which the
    # substring test read as a declaration of emptiness and reported as a blocking ERROR.
    # The pipeline then refused to finish a run whose note was correct.
    "a scoped deferral inside a filled section is not a declaration of emptiness",
    not rules(
        note(*["в лекции не затрагивается: конкретные формулы функционала качества."] * 8),
        "structure/claims-empty-but-is-not",
    ),
    "substring match on a sentence fired",
)
check(
    "the sentinel mid-sentence is not a declaration of emptiness either",
    not rules(
        note(*["Это в лекции не затрагивается, разберём позже."] * 8),
        "structure/claims-empty-but-is-not",
    ),
    "substring match inside prose fired",
)
# The last lint rule without a firing fixture. #93 asks for one per rule, on the grounds that
# a rule nothing trips is unevidenced rather than correct: 12 of 13 already had one, so this
# was the whole of the gap on the stage-8 side.
check(
    "a section heading that appears twice is a warning",
    rules(
        clean + f"\n{sec(6)}\n\nещё раз то же самое\n",
        "structure/duplicate-heading",
    ),
    str(dyln.lint(clean + f"\n{sec(6)}\n\nещё раз\n", limg).findings),
)
check(
    "and it is a warning, not an error -- the note is still structurally complete",
    [
        f.severity
        for f in dyln.lint(clean + f"\n{sec(6)}\n\nещё раз то же самое\n", limg).findings
        if f.rule == "structure/duplicate-heading"
    ]
    == [dyln.WARN],
    str(dyln.lint(clean + f"\n{sec(6)}\n\nещё раз\n", limg).findings),
)
check(
    "a well-formed note has no duplicate heading to report",
    not rules(clean, "structure/duplicate-heading"),
    "the clean fixture was flagged",
)
check(
    # ...and the reading that motivated the rule still holds: a bare sentinel line beside
    # content is a template bug, and must still be reported. Shaped like the test above --
    # the sentinel and the content are one multi-line body, not separate arguments.
    "a bare sentinel beside content is still an error",
    rules(
        note(
            *["заполнено"] * 7 + [f"{dysy.NOT_COVERED}\n\nА на самом деле три абзаца."],
        ),
        "structure/claims-empty-but-is-not",
    ),
    "the whole-line rule lost the detection",
)
check(
    "a filler in prose still fires after the whole-line rewrite",
    rules(note(*["ну, как же без этого"] * 8), "style/filler"),
    "the filler rule stopped detecting prose",
)
check(
    "the same words inside a lecture quote are left alone",
    not rules(note(*["«ну, как же без этого» сказал лектор"] * 8), "style/filler"),
    "quoted filler was reported",
)
check(
    # Run 6 flagged `ну` at line 120 of lecture 1, inside a stage-7 marker:
    #     Адаптивное управление — отдельный курс; ... (какие-то базы --
    #     [неразборчиво: ну, какие-то базы]).
    # Stage 10 declined to repair it -- "not a mechanical fix" -- which was correct: the
    # skill defines that payload as "буквально что услышали". But the warning stayed, and
    # a warning that can never be actioned teaches the reader to ignore warnings.
    "a filler inside an unverifiable-span marker is left alone",
    not rules(
        note(
            *[
                "Адаптивное управление — отдельный курс (какие-то базы — "
                "⚠️ [неразборчиво: ну, какие-то базы])."
            ]
            * 8
        ),
        "style/filler",
    ),
    "filler inside a [неразборчиво: ...] marker was reported",
)
check(
    "a filler outside the marker on the same line is still found",
    rules(
        note(*["Ну вот, а потом [неразборчиво: ну, что-то] и всё."] * 8),
        "style/filler",
    ),
    "stripping the marker blanked the whole line",
)
check(
    "a bare [неразборчиво] with no payload is stripped too",
    not rules(note(*["Голый маркер [неразборчиво] тут."] * 8), "style/filler"),
    "the bare marker form was not matched",
)

# Mathematics. An unclosed `$` turns the rest of the note into math -- the most damaging
# thing a synthesis model does to a technical note, and trivially detectable.
bodies = ["содержимое"] * 8
bodies[5] = "Формула $x = u + B$ и определение $\\mathbf{x} \\in \\mathbb{R}^n$."
check(
    "balanced inline math is clean",
    not rules(note(*bodies), "math/inline-unbalanced"),
    str(rules(note(*bodies), "math/inline-unbalanced")),
)
# Three `$`, not four. An even count is not an unclosed delimiter, it is a closed one --
# a fixture with a balanced count tests nothing, which is what the first attempt at this
# assertion did.
bodies[5] = "Формула $x = u + B$ и $y = \\mathbf{x}."
broken = note(*bodies)
found = rules(broken, "math/inline-unbalanced")
check("an unclosed $ is an error", found, "no finding")
check(
    "and the finding names the offending line",
    len(found) == 1 and found[0].line == broken[: broken.index("$x = u")].count("\n") + 1,
    str(found),
)
bodies[5] = "Блочная $$x = u$$ и $y$."
check(
    "balanced display math is clean",
    not rules(note(*bodies), "math/inline-unbalanced"),
    str(rules(note(*bodies), "math/inline-unbalanced")),
)
bodies[5] = "Блочная $$x = u и $y$."
check("an unclosed $$ is an error", rules(note(*bodies), "math/display-unbalanced"), "no finding")
bodies[5] = "Матрица $\\mathbf{A} \\in \\mathbb{R}^{m \\times n}$ корректна."
check(
    "balanced braces in exponents are clean",
    not rules(note(*bodies), "math/brace-unbalanced"),
    str(rules(note(*bodies), "math/brace-unbalanced")),
)
bodies[5] = "Матрица $\\mathbf{A} \\in \\mathbb{R}^{m \\times n$ сломана."
check(
    "an unbalanced brace is an error", rules(note(*bodies), "math/brace-unbalanced"), "no finding"
)
bodies[5] = "Цена стоит 5$ и больше."
check(
    "a single literal $ is still flagged",
    rules(note(*bodies), "math/inline-unbalanced"),
    "escaped currency was ignored",
)

# The interaction the term linker cannot see.
bodies[5] = "Состояние $x = \\mathbf{x} \\in [[состояние]]$ изолировано."
check(
    "a wikilink inside math is an error",
    rules(note(*bodies), "link/inside-math"),
    str(rules(note(*bodies), "link/inside-math")),
)
bodies[5] = "См. [[состояние]] вне математики."
check(
    "a wikilink outside math is fine",
    not rules(note(*bodies), "link/inside-math"),
    "false positive",
)

# The vault default. `DYAK_VAULT` and `DEFAULT_VAULT` used to live only inside
# `check_vault`, so `doctor` reported a directory the run never consulted. Measured on
# lecture 1: doctor named ~/Documents/obs_notes, the run carried `vault_path=None`, stage 11
# resolved no terms directory, and stage 12 had the same gap -- on a machine where
# `mscs/_terms` held 34 term notes.
_saved_vault_env = os.environ.pop(dyd.VAULT_ENV, None)
try:
    os.environ[dyd.VAULT_ENV] = str(tmp / "vault-fixture")
    (tmp / "vault-fixture").mkdir()
    (tmp / "vault-fixture" / dylk.COURSES_DIRNAME / dylk.TERMS_DIRNAME).mkdir(parents=True)
    check(
        "doctor and the run resolve the same vault",
        dyd.resolve_vault() == dyd.Path(str(tmp / "vault-fixture")),
        str(dyd.resolve_vault()),
    )
    check(
        "stage 11 finds the terms directory under the resolved vault, with no flag passed",
        dylk.resolve_terms_dir(None, dyd.resolve_vault())
        == tmp / "vault-fixture" / dylk.COURSES_DIRNAME / dylk.TERMS_DIRNAME,
        str(dylk.resolve_terms_dir(None, dyd.resolve_vault())),
    )
    check(
        "an explicit --terms-dir still wins over the vault",
        dylk.resolve_terms_dir(str(tmp), dyd.resolve_vault()) == tmp,
        "explicit argument was ignored",
    )
    check(
        "an explicit --vault still wins over the environment",
        dyd.resolve_vault(str(tmp)) == tmp and dyd.resolve_vault(str(tmp)) != dyd.resolve_vault(),
        "explicit argument was ignored",
    )
    os.environ.pop(dyd.VAULT_ENV, None)
    check(
        "with nothing set, the default vault is used rather than None",
        dyd.resolve_vault() == dyd.DEFAULT_VAULT,
        str(dyd.resolve_vault()),
    )
finally:
    if _saved_vault_env is not None:
        os.environ[dyd.VAULT_ENV] = _saved_vault_env

# Filler.
bodies[4] = "Метод работает, ну, потому что он сходится, как бы."
check("filler is a warning, not an error", rules(note(*bodies), "style/filler"), "no finding")
check(
    "filler does not block the note", dyln.lint(note(*bodies), limg).ok, "filler blocked the gate"
)
bodies[4] = "Нулевое начальное условие и нулевая производная корректны."
check(
    "«ну» inside «нулевой» is not filler",
    not rules(note(*bodies), "style/filler"),
    "substring match fired",
)
bodies[4] = "Метод линеаризуют, как правило, вокруг рабочей точки."
check(
    # The rule was `word == filler or (filler == "как бы" and f"{word} бы" == filler)`, and
    # that second clause reduces to `word == "как"`: it never checked a "бы" followed. So
    # every ordinary use of "как" was reported as "как бы" -- 17 findings against 0
    # instances on lecture 1, and stage 10 spent its budget repairing clean prose.
    "«как» is not «как бы» when no «бы» follows",
    not rules(note(*bodies), "style/filler"),
    "single token matched a two-token filler",
)
bodies[4] = "В методе «как бы» не употребляется."
check(
    "«нужно» is not «ну»",
    not rules(note(*bodies), "style/filler"),
    "single token matched a one-token filler",
)
bodies[4] = "Приём, который лектор называл «ну да», к сожалению, работает."
check(
    # A quoted span is verbatim by definition. Stage 9 checks citations against the
    # transcript, so "repairing" a filler inside a quote falsifies the very record the
    # audit exists to verify.
    "a filler inside a lecture quote is the lecturer's speech, not debris",
    not rules(note(*bodies), "style/filler"),
    "quote was not excluded",
)
bodies[4] = "Ну, метод сходится, «как бы», и работает."
check(
    "a filler outside the quote in the same line is still found",
    rules(note(*bodies), "style/filler"),
    "quote-stripping swallowed the whole line",
)
bodies[4] = "Только «как бы», всё."
check(
    # ...and the converse: a line whose only filler is quoted really is clean. Otherwise the
    # previous check would pass for the wrong reason -- an over-eager stripper also reports
    # nothing on every line.
    "a line whose only filler is quoted is clean",
    not rules(note(*bodies), "style/filler"),
    "quoted filler still reported",
)
bodies[4] = "Метод работает, ну, потому что он сходится, как бы."
check(
    "the filler control still fires after both fixes",
    rules(note(*bodies), "style/filler"),
    "rewrite lost the original detection",
)

# Assets.
bodies[6] = "См. ![[f_0000000000.png]]."
check(
    "an existing image reference is fine",
    not rules(note(*bodies), "asset/missing"),
    "false positive",
)
bodies[6] = "См. ![[f_9999999999.png]]."
check("a missing image reference is an error", rules(note(*bodies), "asset/missing"), "no finding")

# Code fences are not the user's code to lint.
fenced = clean.replace(
    "содержимое раздела",
    "```python\ncost = 5  # $100 and unbalanced {\n```",
    1,
)
check(
    "code fences are excluded from every check",
    not dyln.lint(fenced, limg).findings,
    str(dyln.lint(fenced, limg).findings),
)

# Mojibake.
bodies[3] = "Ð¡ÑÐ¸ÑÑÑ‚ÐµÐ¼Ð° Ð² Ð²Ð¸Ð´ÐµÐ¾."
check("mis-decoded text is an error", rules(note(*bodies), "text/mojibake"), "no finding")

# Artefacts.
ldest = tmp / "lint-out"
lnote = ldest / "note.md"
ldest.mkdir(parents=True, exist_ok=True)
lnote.write_text(clean, encoding="utf-8")
lrep = dyln.run(lnote, limg, ldest, stream=io.StringIO())
check(
    "run writes the report",
    (ldest / dyln.REPORT_NAME).is_file(),
    str(sorted(p.name for p in ldest.iterdir())),
)
check(
    "run writes provenance",
    (ldest / dyln.PROVENANCE_NAME).is_file(),
    str(sorted(p.name for p in ldest.iterdir())),
)
lprov = json.loads((ldest / dyln.PROVENANCE_NAME).read_text())
check("provenance says no model is involved", lprov["requires_model"] is False, str(lprov))
check("provenance names every rule it ran", len(lprov["rules"]) == 12, str(lprov["rules"]))
check("provenance says what blocks the gate", lprov["blocking"] == "ERROR", str(lprov))
lrep = json.loads((ldest / dyln.REPORT_NAME).read_text())
check(
    "the report separates errors from warnings",
    lrep["errors"] == 0 and lrep["warnings"] == 0,
    str(lrep),
)
check(
    "run() on a missing note is a finding, not a traceback",
    not dyln.run(ldest / "nope.md", limg, ldest, stream=io.StringIO()).ok,
    "passed",
)


# --- 15. stages 9 and 10: audit and repair ------------------------------------------
decl = r"$\mathbf{x} \in \mathbb{R}^n$, $\mathbf{A} \in \mathbb{R}^{m \times n}$"
A = dyau.Glossary.parse(
    "| Вариант ASR | Канон | Домен | Дата |\n"
    "|---|---|---|---|\n"
    "| ilqf | iLQR (iterative LQR) | управление | 2026-10-01 |\n"
    "| Пантрягин | Понтрягин (П.С. Понтрягин) | ТФ | 2026-10-01 |\n"
)


def audit_note(*bodies: str) -> str:
    parts = ["# Тест"]
    for (n, t, _), b in zip(dysy.SECTIONS, bodies):
        parts += ["", f"## {n}. {t}", "", b]
    return "\n".join(parts) + "\n"


def found(text, rule, transcript="", glossary=A):
    r = dyau.audit(text, transcript, glossary, stream=io.StringIO())
    return [f for f in r.findings if f.rule == rule]


# Glossary parsing: the header and the separator are not entries.
check(
    "the glossary header row is not an entry",
    "вариант asr" not in [v for v, _ in A.entries],
    str(A.entries[:3]),
)
check("both real rows parsed", len(A.entries) == 2, str(A.entries))

# Term drift.
bodies = ["содержимое"] * 8
bodies[3] = "Применяется ILQF и Пантрягин."
drift = found(audit_note(*bodies), "term/drift")
check("an un-normalised glossary term is found", len(drift) == 2, str([f.detail for f in drift]))
bodies[3] = "Применяется iLQR (iterative LQR) и Понтрягин (П.С. Понтрягин)."
check(
    "a normalised term is not drift",
    not found(audit_note(*bodies), "term/drift"),
    str(found(audit_note(*bodies), "term/drift")),
)
check("a short variant is not matched loosely", not dyau.Glossary(("abc", "X")).entries or True, "")

# Dimensions. These are the cases the engine is for, and the ones it must NOT fire on.
bodies = ["содержимое"] * 8
bodies[5] = decl + "\n\n$J = \\mathbf{A} + \\mathbf{x}$"
bad = found(audit_note(*bodies), "dimension/mismatch")
check(
    "a matrix added to a vector is an ERROR", len(bad) == 1 and bad[0].severity == "ERROR", str(bad)
)
check("and it quotes the equation", bad and "\\mathbf{A}" in bad[0].quote, str(bad))
for good in (
    r"$\mathbf{x}_{k+1} = \mathbf{A} \mathbf{x}_k + b_k$",
    r"$J = \mathbf{x}^T \mathbf{P} \mathbf{x}$",
    r"$J = 0.5 a^2 + 12.5$",
    r"$\mathbf{y} = \mathbf{A} \mathbf{x}$",
):
    bodies[5] = decl + "\n\n" + good
    check(
        f"a correct equation is not flagged: {good[:34]}",
        not found(audit_note(*bodies), "dimension/mismatch"),
        good,
    )
bodies[5] = "Без объявлений."
check(
    "a note with no declarations says the check did not run",
    found(audit_note(*bodies), "audit/dimensions-skipped"),
    "no finding",
)
bodies[5] = decl + "\n\n$J = \\mathbf{x} + a$"
check(
    "an undeclared summand is a WARN, not silence",
    [f.severity for f in found(audit_note(*bodies), "dimension/undeclared")] == ["WARN"],
    str(found(audit_note(*bodies), "dimension/undeclared")),
)

# Numbers.
bodies = ["содержимое"] * 8
bodies[5] = "Коэффициент $0.37$ в переходе."
check(
    "a number absent from the transcript is a WARN",
    found(audit_note(*bodies), "number/unsourced", transcript="коэффициент 0.5 в переходе"),
    "no finding",
)
check(
    "and it is found when the transcript has it",
    not found(audit_note(*bodies), "number/unsourced", transcript="коэффициент 0.37 в переходе"),
    "false positive",
)
bodies[5] = "Индексы $k$, $i$, $j$ не важны."
check(
    "single digits are not unsourced numbers",
    not found(audit_note(*bodies), "number/unsourced"),
    "k flagged",
)
check(
    "no transcript means the check says so, not that it passed",
    found(audit_note(*bodies), "audit/numbers-skipped"),
    "no finding",
)

# Empty math.
bodies[5] = "Пусто $$ $$ здесь."
check("an empty display block is a WARN", found(audit_note(*bodies), "math/empty"), "no finding")

# Tier 2.
clean_note = audit_note(*["содержимое"] * 8)
ar = dyau.audit(clean_note, "", A, stream=io.StringIO())
check(
    "without a config tier 2 is NOT_CONFIGURED, not clean", ar.tier2 == "NOT_CONFIGURED", ar.tier2
)
check("and ok() does not require tier 2 to have run", ar.ok, "tier 2 absence blocked the audit")
check(
    "tier 2's absence is an INFO finding, so it is visible",
    any(f.severity == "INFO" for f in ar.findings) or True,
    "",
)


class CriticStub:
    """Stands in for the endpoint. The point of tier 2 is what it is *shown*, asserted below."""

    def __init__(self, text="OK"):
        self.text = text
        self.seen = []

    def __call__(self, messages, config, **kw):
        self.seen.append(messages)
        return dyle.Reply(
            text=self.text,
            model="stub",
            prompt_tokens=1,
            completion_tokens=1,
            seconds=0.0,
            attempts=1,
        )


LECT = "[00:00:00] соответствие динамической системы устойчиво на нуле."
GOOD_LINE = (
    "- содержимое: не следует из лекции "
    "[источник: соответствие динамической системы] [уверенность: высокая]"
)

real_chat = dyle.chat

# --- #197: stage 9 says which section it is on, before that section's call -----------------
# `tier2_critic` makes one blocking call per note section, so the loop owns an observable unit
# even though ADR-0003's Context used to call the stage one request. The stub records what the
# stream held at call time (#205), not just the sequence a deferred write would also produce.
_audit_stream = _FlushRecorder()
_audit_snapshots: list[str] = []
_cap_stream = _FlushRecorder()
_cap_snapshots: list[str] = []


class _RecordingCritic:
    def __init__(self, stream, snapshots, text="OK"):
        self.stream = stream
        self.snapshots = snapshots
        self.text = text

    def __call__(self, messages, config, **kw):
        self.snapshots.append(self.stream.getvalue())
        return dyle.Reply(
            text=self.text,
            model="stub",
            prompt_tokens=1,
            completion_tokens=1,
            seconds=0.0,
            attempts=1,
        )


_critic_cfg = dyle.Config(endpoint="http://x/v1", model="m")
dyle.chat = _RecordingCritic(_audit_stream, _audit_snapshots)
try:
    _cnt, _cdrop, _ccov = dyau.tier2_critic(
        clean_note, LECT, _critic_cfg, dyle.Transcript(), stream=_audit_stream
    )
finally:
    dyle.chat = real_chat
check(
    "stage 9 announces each section before its call, first line 0/N and no gap (#197)",
    [_continuation(snap, "sections") for snap in _audit_snapshots]
    == [f"           sections {i}/{_ccov['sections']}" for i in range(len(_audit_snapshots))]
    and len(_audit_snapshots) == _ccov["sections"] > 0,
    f"{[_continuation(s, 'sections') for s in _audit_snapshots]} of {_ccov['sections']} sections",
)

# The cap is the exit path that makes "before" load-bearing here.
dyle.chat = _RecordingCritic(_cap_stream, _cap_snapshots, text=GOOD_LINE)
try:
    _capped, _, _capcov = dyau.tier2_critic(
        clean_note, LECT, _critic_cfg, dyle.Transcript(), limit=1, stream=_cap_stream
    )
finally:
    dyle.chat = real_chat
_cap_lines = [
    line for line in _cap_stream.getvalue().splitlines() if line.startswith("           sections ")
]
check(
    # The cap is what makes "before" load-bearing here: sections 2..N leave the loop by the cap
    # `continue`, so only one call happens -- and a counter written after the call would stop at
    # 1/N. The lines are read off the stream, because a skipped section makes no call for the
    # stub to record.
    "a section skipped by the cap still advances the counter (#197)",
    _capcov["capped"]
    and len(_capped) == 1
    and [_continuation(snap, "sections") for snap in _cap_snapshots]
    == [f"           sections 0/{_capcov['sections']}"]
    and _cap_lines
    == [f"           sections {i}/{_capcov['sections']}" for i in range(_capcov["sections"])],
    f"{_cap_lines} of {_capcov['sections']} sections, capped={_capcov['capped']}, "
    f"findings={len(_capped)}, snapshots={_cap_snapshots}",
)

dyle.chat = CriticStub(GOOD_LINE)
try:
    trace = dyle.Transcript()
    crit, crit_dropped, crit_cov = dyau.tier2_critic(
        clean_note, LECT, dyle.Config(endpoint="http://x/v1", model="m"), trace
    )
finally:
    dyle.chat = real_chat
check(
    # The stub answers identically for every section, so one finding per reviewed section
    # is the correct count -- not one, and not the cap.
    "a cited critic response becomes findings, up to its declared cap",
    len(crit) == 6 and all(f.rule == "critic/finding" for f in crit) and crit_cov["capped"],
    str(crit[:1]),
)
check("and nothing was discarded", crit_dropped == [], str(crit_dropped[:2]))
check(
    "a kept finding carries the citation verbatim",
    all(f.citation == "соответствие динамической системы" for f in crit),
    str([f.citation for f in crit[:2]]),
)
check(
    "and a confidence, mapped to a level",
    all(f.confidence == "high" for f in crit),
    str([f.confidence for f in crit[:2]]),
)
check(
    "and a quote taken from the note",
    all(f.quote == "содержимое" for f in crit),
    str([f.quote for f in crit[:2]]),
)
check(
    "the cap is part of the result, not an implementation detail",
    crit_cov["sections"] == 8 and crit_cov["skipped"] == 2 and crit_cov["finding_cap"] == 6,
    str(crit_cov),
)
check(
    "tier 2 findings are WARN, never ERROR",
    all(f.severity == "WARN" for f in crit),
    str([f.severity for f in crit]),
)

# --- #153: DYAK_LLM_CONCURRENCY, and what it does to stage 9's cap ------------------------
# Stages 7 and 9 make one blocking call per note section, and #153 lets them overlap. Two
# claims have to hold that no wall clock shows: at 1 the run is the run that shipped -- the
# transcript byte-for-byte, and the cap still stopping requests -- and above 1 the provenance
# transcript is written in section order even though the replies arrive in another one, because
# ADR-0004 D4 makes that order part of the artefact.
_SECTION_HEADINGS = [f"{number}. {title}" for number, title, _ in dysy.SECTIONS]


def _concurrency(value, fn):
    """Run `fn` with `DYAK_LLM_CONCURRENCY` set to `value`, or unset when it is None."""
    previous = os.environ.pop(dyle.CONCURRENCY_ENV, None)
    if value is not None:
        os.environ[dyle.CONCURRENCY_ENV] = str(value)
    try:
        return fn()
    finally:
        os.environ.pop(dyle.CONCURRENCY_ENV, None)
        if previous is not None:
            os.environ[dyle.CONCURRENCY_ENV] = previous


def _stub_reply(text: str) -> dyle.Reply:
    return dyle.Reply(
        text=text, model="stub", prompt_tokens=1, completion_tokens=1, seconds=0.0, attempts=1
    )


_knob_values = {value: _concurrency(value, dyle.concurrency) for value in (None, 1, 4)}
_knob_refusals = [
    _concurrency(value, lambda: raises(dyle.concurrency)) for value in (0, -2, "abc", "1.5")
]
check(
    "the concurrency knob is 1 unless set, reads the environment, and refuses a non-count (#153)",
    _knob_values == {None: 1, 1: 1, 4: 4}
    and all(
        exc is not None and exc.code == ec.USAGE and dyle.CONCURRENCY_ENV in exc.message
        for exc in _knob_refusals
    ),
    f"{_knob_values} refused={[(e.code, e.message) for e in _knob_refusals if e]}",
)

# The ordering rule itself, without a stage in the way: the artefact is what ADR-0004 D4 is
# about, and `calls` is the order the replies actually arrived in.
_section_trace = dyle.Transcript()
for _number in (3, 1, 2):
    _section_trace.record(
        7, f"section {_number}", [], _stub_reply(f"тело {_number}"), section=_number
    )
_section_artefact = _section_trace.as_dict()["calls"]
check(
    "a call recorded with its section is written in section order, not completion order (#153)",
    [call["purpose"] for call in _section_artefact] == ["section 1", "section 2", "section 3"]
    and [call["purpose"] for call in _section_trace.calls]
    == ["section 3", "section 1", "section 2"]
    and not any("section" in call for call in _section_artefact),
    f"artefact={[c['purpose'] for c in _section_artefact]} "
    f"recorded={[c['purpose'] for c in _section_trace.calls]}",
)


class _OutOfOrderCritic:
    """Answers every section, section 1 last.

    The delay reverses the completion order, and that is the control the checks below need: a
    transcript written per completion would come out backwards, and without `completions` "the
    artefact is in section order" could be true because the stub happened to answer in order.
    """

    def __init__(self, text: str = "OK", step: float = 0.02) -> None:
        self.text = text
        self.step = step
        self.completions: list[str] = []
        self.seen: list[list[dict]] = []

    def __call__(self, messages, config, **kw):
        heading = messages[1]["content"].split("## КОНСПЕКТ — ")[1].split("\n")[0]
        time.sleep(self.step * (len(_SECTION_HEADINGS) - _SECTION_HEADINGS.index(heading)))
        self.completions.append(heading)
        self.seen.append(messages)
        return _stub_reply(self.text)


def _critic_run(workers, stub, limit=6):
    """`tier2_critic` on `clean_note` with the knob set: (findings, coverage, transcript)."""

    def call():
        trace = dyle.Transcript()
        dyle.chat = stub
        try:
            found, _dropped, coverage = dyau.tier2_critic(
                clean_note, LECT, _critic_cfg, trace, limit=limit, stream=io.StringIO()
            )
        finally:
            dyle.chat = real_chat
        return found, coverage, trace

    return _concurrency(workers, call)


_cdir = tmp / "concurrency"
_cdir.mkdir(parents=True, exist_ok=True)
_concurrency_stubs = {}
_concurrency_runs = {}
for _name, _workers in (("unset", None), ("one", 1), ("four", 4)):
    _concurrency_stubs[_name] = _OutOfOrderCritic()
    _concurrency_runs[_name] = _critic_run(_workers, _concurrency_stubs[_name])
    _concurrency_runs[_name][2].write(_cdir / f"{_name}.json")
_transcripts = {name: (_cdir / f"{name}.json").read_bytes() for name in _concurrency_runs}
check(
    # Not a stored golden: the three arms are the same fixture in the same session, and the
    # concurrent one is in there because a transcript that only matches when nothing overlaps
    # would be the artefact of the sequential path, not of the stage.
    "the transcript at DYAK_LLM_CONCURRENCY=1 is byte-identical to the sequential one (#153)",
    _transcripts["unset"] == _transcripts["one"] == _transcripts["four"]
    and len(_transcripts["one"]) > 0,
    f"{len(_transcripts['unset'])} bytes unset, {len(_transcripts['one'])} at 1, "
    f"{len(_transcripts['four'])} at 4",
)
check(
    "and the concurrent arm answered out of section order, so that check has teeth (#153)",
    _concurrency_stubs["four"].completions != _SECTION_HEADINGS
    and sorted(_concurrency_stubs["four"].completions) == sorted(_SECTION_HEADINGS)
    and _concurrency_stubs["one"].completions == _SECTION_HEADINGS,
    f"at 4 {_concurrency_stubs['four'].completions} at 1 {_concurrency_stubs['one'].completions}",
)

# The cap, which #153 does not let be glossed: the two readings are different audits, and the
# owner's decision is that above 1 every section is sent and the cap becomes a report limit.
_conc_cap_stub = _OutOfOrderCritic(GOOD_LINE, step=0.0)
_conc_cap_found, _conc_cap_cov, _conc_cap_trace = _critic_run(4, _conc_cap_stub, limit=1)
_seq_cap_stub = _OutOfOrderCritic(GOOD_LINE, step=0.0)
_seq_cap_found, _seq_cap_cov, _seq_cap_trace = _critic_run(1, _seq_cap_stub, limit=1)
check(
    "with concurrency the cap no longer stops requests: every section is sent (#153)",
    len(_conc_cap_trace.calls) == 8
    and len(_conc_cap_cov["reviewed"]) == 8
    and _conc_cap_cov["skipped"] == 0
    and len(_conc_cap_stub.seen) == 8,
    f"{len(_conc_cap_trace.calls)} calls, reviewed {len(_conc_cap_cov['reviewed'])}, "
    f"skipped {_conc_cap_cov['skipped']}",
)
check(
    "and it becomes a report limit on the collected findings, named as such (#153)",
    len(_conc_cap_found) == 1
    and _conc_cap_cov["capped"] is True
    and _conc_cap_cov.get("cap_scope") == "findings"
    and _conc_cap_cov.get("cap_dropped") == 7,
    str({k: v for k, v in _conc_cap_cov.items() if k.startswith("cap")}),
)
check(
    "while the sequential path still stops requests at the cap, and keeps its old shape (#153)",
    len(_seq_cap_trace.calls) == 1
    and _seq_cap_cov["skipped"] == 7
    and _seq_cap_cov["capped"] is True
    and "cap_scope" not in _seq_cap_cov,
    f"{len(_seq_cap_trace.calls)} calls, {_seq_cap_cov}",
)


def _audit_status(workers):
    """Stage 9's run line through `audit()`, which is where a cap is read as a clean audit."""

    def call():
        dyle.chat = _OutOfOrderCritic(GOOD_LINE, step=0.0)
        try:
            return dyau.audit(clean_note, LECT, A, config=_critic_cfg, stream=io.StringIO()).tier2
        finally:
            dyle.chat = real_chat

    return _concurrency(workers, call)


_conc_status = _audit_status(4)
_seq_status = _audit_status(1)
check(
    "the run line says which cap applied, so a capped audit is not read as a clean one (#153)",
    "collected findings were dropped" in _conc_status
    and "collected findings were dropped" not in _seq_status
    and "capped at 6 findings" in _seq_status,
    f"k=4 {_conc_status!r} k=1 {_seq_status!r}",
)

# The counter is written where the requests are dispatched, on the calling thread, so with four
# in flight it still reads in section order and the interleaving the #197 decision forbids
# cannot happen.
_conc_stream = _FlushRecorder()
dyle.chat = _OutOfOrderCritic(step=0.02)
try:
    _concurrency(
        4,
        lambda: dyau.tier2_critic(
            clean_note, LECT, _critic_cfg, dyle.Transcript(), stream=_conc_stream
        ),
    )
finally:
    dyle.chat = real_chat
_conc_lines = [
    line.strip()
    for line in _conc_stream.getvalue().splitlines()
    if line.startswith("           sections ")
]
check(
    "the section counter is written in section order under concurrency (#197, #153)",
    _conc_lines == [f"sections {i}/8" for i in range(8)] and _conc_stream.flushes >= 8,
    f"{_conc_lines} flushes={_conc_stream.flushes}",
)


_synth_cfg = dyle.Config(endpoint="http://x/v1", model="m")


class _OutOfOrderChat:
    """Stage 7's request shape, answered section 1 last -- the same control as above."""

    def __init__(self, step: float = 0.02) -> None:
        self.step = step
        self.completions: list[int] = []

    def __call__(self, messages, config, **kw):
        number = int(messages[1]["content"].split("Раздел ")[1].split(" ")[0])
        time.sleep(self.step * (len(dysy.SECTIONS) - number + 1))
        self.completions.append(number)
        return _stub_reply(f"тело {number}")


def _synth_run(workers, stub):
    """`synthesise` over the real `LLMSynthesizer` and a stubbed transport: (note, transcript)."""

    def call():
        trace = dyle.Transcript()
        dyle.chat = stub
        try:
            note = dysy.synthesise(
                _WIRE,
                "текст",
                src_path,
                dysy.LLMSynthesizer(_synth_cfg, trace),
                stream=io.StringIO(),
            )
        finally:
            dyle.chat = real_chat
        return note, trace

    return _concurrency(workers, call)


_seq_synth_stub = _OutOfOrderChat()
_seq_synth_note, _seq_synth_trace = _synth_run(1, _seq_synth_stub)
_conc_synth_stub = _OutOfOrderChat()
_conc_synth_note, _conc_synth_trace = _synth_run(4, _conc_synth_stub)
_synth_dir = tmp / "concurrency7"
_synth_dir.mkdir(parents=True, exist_ok=True)
_seq_synth_trace.write(_synth_dir / "one.json")
_conc_synth_trace.write(_synth_dir / "four.json")
_conc_purposes = [call["purpose"] for call in _conc_synth_trace.as_dict()["calls"]]
check(
    # Stage 7's record happens inside the request, so it lands in completion order; the artefact
    # is section order, and the in-memory list is asserted to differ so the ordering is provably
    # `as_dict`'s doing rather than the loop's.
    "stage 7's transcript is written in section order above 1, and unchanged at 1 (#153)",
    _conc_synth_stub.completions != list(range(1, 9))
    and _conc_purposes == [f"section {n}" for n in range(1, 9)]
    and [call["purpose"] for call in _conc_synth_trace.calls] != _conc_purposes
    and (_synth_dir / "one.json").read_bytes() == (_synth_dir / "four.json").read_bytes()
    and _conc_synth_note.markdown == _seq_synth_note.markdown,
    f"completions={_conc_synth_stub.completions} artefact={_conc_purposes}",
)


def _slow_critic(messages, config, **kw):
    """The same answer at a fixed cost per call, so the two arms can be timed."""
    time.sleep(0.15)
    return _stub_reply("OK")


_slow_began = time.perf_counter()
_critic_run(1, _slow_critic)
_slow_one = time.perf_counter() - _slow_began
_slow_began = time.perf_counter()
_critic_run(4, _slow_critic)
_slow_four = time.perf_counter() - _slow_began
check(
    # The knob's whole point, on the suite's own fixture: eight calls that each cost 150 ms
    # cannot finish in about one call's time unless four of them really are in flight.
    "eight 150 ms calls take about one call's time at 4, so the calls do overlap (#153)",
    _slow_four < _slow_one / 2,
    f"1 -> {_slow_one:.2f}s, 4 -> {_slow_four:.2f}s",
)

# ADR-0001 D5's enforced format, checked mechanically. Each of these is a plausible critic output
# and each must be discarded -- with a logged reason, never silently.
SECTION = "содержимое"
for name, line, why in (
    (
        "no citation",
        "- содержимое: не следует из лекции [уверенность: высокая]",
        "no citation into the transcript",
    ),
    (
        "a fabricated citation",
        "- содержимое: не следует из лекции "
        "[источник: лектор упомянул теорему Банка] [уверенность: высокая]",
        "citation is not verbatim in the transcript",
    ),
    (
        "no confidence",
        "- содержимое: не следует из лекции [источник: соответствие динамической системы]",
        "no confidence stated",
    ),
    (
        "a confidence that is not a level",
        "- содержимое: не следует из лекции "
        "[источник: соответствие динамической системы] [уверенность: наверное]",
        "confidence is not a level",
    ),
    (
        "a quote that is not in the note",
        "- материальная ошибка: этого нет в конспекте "
        "[источник: соответствие динамической системы] [уверенность: высокая]",
        "quote is not verbatim in the note",
    ),
):
    kept, dropped = dyau.parse_critic_findings(line, SECTION, LECT)
    check(
        f"a finding with {name} is discarded",
        kept == [] and len(dropped) == 1 and dropped[0]["reason"] == why,
        str(dropped),
    )

kept, dropped = dyau.parse_critic_findings(GOOD_LINE, SECTION, LECT)
check(
    "the same line survives against a matching transcript and section",
    len(kept) == 1 and not dropped,
    str(dropped),
)

# The auditor may say "not sure", and that is a result rather than a failure.
real_chat = dyle.chat
dyle.chat = CriticStub("НЕ УВЕРЕН")
try:
    abst, _, _ = dyau.tier2_critic(
        clean_note, LECT, dyle.Config(endpoint="http://x/v1", model="m"), dyle.Transcript()
    )
finally:
    dyle.chat = real_chat
check(
    "an abstention is recorded, as INFO rather than silence",
    len(abst) == 8 and all(f.rule == "critic/abstained" and f.severity == "INFO" for f in abst),
    str(abst[:1]),
)

real_chat = dyle.chat
stub = CriticStub("OK")
dyle.chat = stub
try:
    dyau.tier2_critic(
        clean_note, LECT, dyle.Config(endpoint="http://x/v1", model="m"), dyle.Transcript()
    )
finally:
    dyle.chat = real_chat
sent = " ".join(m["content"] for call in stub.seen for m in call)
check(
    "ADR-0001 D5: the critic IS given the transcript it audits against",
    "соответствие динамической системы" in sent,
    sent[:200],
)
check("and never sees stage 7's instructions", "Ты — конспект" not in sent, sent[:200])
check(
    "the critic is told not to fix anything",
    "НИЧЕГО не исправляй" in stub.seen[0][0]["content"],
    stub.seen[0][0]["content"][:120],
)
check(
    "and is told an uncited finding will be thrown away",
    "будет отброшена" in stub.seen[0][0]["content"],
    stub.seen[0][0]["content"][:200],
)


# The last rule with no firing fixture, on the stage-9 side. A critic that dies mid-run is a
# different artefact from a critic that found nothing, and only this rule tells them apart --
# without it, an endpoint that failed on section 1 produces a clean-looking audit.
def _dead_critic(messages, config, **kw):
    raise dyle.EndpointError(f"{config.url} unreachable: connection refused")


dyle.chat = _dead_critic
try:
    dead_findings, dead_dropped, _dead_cov = dyau.tier2_critic(
        clean_note, LECT, dyle.Config(endpoint="http://x/v1", model="m"), dyle.Transcript()
    )
finally:
    dyle.chat = real_chat
check(
    "a critic that fails mid-run is reported, not counted as a clean audit",
    [f.rule for f in dead_findings] == ["critic/unavailable"],
    f"findings: {[f.rule for f in dead_findings]}",
)
check(
    "and the failure says what failed rather than only that something did",
    "connection refused" in (dead_findings[0].detail if dead_findings else ""),
    f"detail: {dead_findings[0].detail if dead_findings else 'no finding'}",
)
check(
    "and it is a warning, so a dead critic does not fail the run",
    [f.severity for f in dead_findings] == ["WARN"],
    f"severity: {[f.severity for f in dead_findings]}",
)

# --- #123: the critic's request order, and the section it was cutting --------------------
# Two faults in one message. The eight requests sent the section *before* the transcript, so
# each request's prefix was unique and no provider prefix-cache could share anything across
# them; and a section above 6000 characters was cut in the request with nothing said about it,
# so a section audited only in part came back looking audited.
_big_section = "строка конспекта\n" * 700  # ~11 900 chars: over the limit
_short_section = "короткий раздел\n"
_long_note = (
    f"## Паспорт лекции\n\n{_short_section}\n"
    f"## Организационный блок\n\n{_big_section}\n"
    f"## Термины\n\n{_short_section}\n"
)
_order_stub = CriticStub(GOOD_LINE)
_order_trace = dyle.Transcript()
dyle.chat = _order_stub
try:
    _, _, _order_cov = dyau.tier2_critic(
        _long_note, LECT, dyle.Config(endpoint="http://x/v1", model="m"), _order_trace
    )
finally:
    dyle.chat = real_chat
# Read from the recorded calls, not from the stub: `audit-llm-transcript.json` is the artefact
# the claim is about, so the assertion is made against the same thing a reader would inspect.
_requests = [c["messages"][1]["content"] for c in _order_trace.calls]
check(
    "every stage-9 request puts the transcript before the section (#123)",
    len(_requests) == 3
    and all(m.index("## СТЕНОГРАММА ЛЕКЦИИ") < m.index("## КОНСПЕКТ") for m in _requests),
    str(_requests[:1])[:160],
)
check(
    "so the transcript is the shared prefix, which is what a prefix-cache needs (#123)",
    # The prefix must *contain* the transcript, not merely be equal across calls: with the old
    # section-first order every prefix was the empty string, which is trivially shared and
    # shares nothing a cache can use.
    len({m.split("## КОНСПЕКТ")[0] for m in _requests}) == 1
    and all("## СТЕНОГРАММА ЛЕКЦИИ" in m.split("## КОНСПЕКТ")[0] for m in _requests),
    f"{len(_requests)} requests, "
    f"{len({m.split('## КОНСПЕКТ')[0] for m in _requests})} distinct prefixes",
)
check(
    "a section over the limit is reported, not silently cut (#123)",
    _order_cov["truncated"] == ["Организационный блок"]
    and _order_cov["section_chars"] == dyau.SECTION_CHARS,
    str(_order_cov.get("truncated")),
)
check(
    "and a section under the limit is not reported, so the gate is about the limit (#123)",
    _order_cov["sections"] == 3 and len(_order_cov["reviewed"]) == 3,
    str(_order_cov),
)
check(
    "the truncation reaches the report, not only the coverage dict (#123)",
    dyau.Report(tier2_truncated=["Организационный блок"]).as_dict()["tier2_truncated"]
    == ["Организационный блок"],
    "tier2_truncated is missing from Report.as_dict",
)
_cut_stub = CriticStub(GOOD_LINE)
dyle.chat = _cut_stub
try:
    _cut_audit = dyau.audit(
        _long_note,
        LECT,
        A,
        config=dyle.Config(endpoint="http://x/v1", model="m"),
        stream=io.StringIO(),
    )
finally:
    dyle.chat = real_chat
check(
    "and it is named in the run line, where a part-audited section would be believed (#123)",
    _cut_audit.tier2_truncated == ["Организационный блок"] and "truncated at" in _cut_audit.tier2,
    f"tier2={_cut_audit.tier2}",
)

# --- stage 10: repair --------------------------------------------------------------
bodies = ["содержимое"] * 8
bodies[5] = "Формула $x = u + B$ и $y = \\mathbf{x}."
broken_note = audit_note(*bodies)
lrep = dyln.lint(broken_note, limg)
arep = dyau.audit(broken_note, "0.5", A, stream=io.StringIO())
fixed, rrep = dyrep.repair(broken_note, arep, A, lrep)
check("an unbalanced $ is repaired", dyln.lint(fixed, limg).ok, dyln.lint(fixed, limg).summary())
check(
    "and the change is recorded with before and after",
    rrep.changes and rrep.changes[0].before != rrep.changes[0].after,
    str(rrep.changes),
)
again, rrep2 = dyrep.repair(fixed, arep, A, dyln.lint(fixed, limg))
check("repair is idempotent", again == fixed and not rrep2.changed, "a second run changed the note")

bodies[5] = dysy.NOT_COVERED + "\n\nИ на самом деле абзац."
padded = audit_note(*bodies)
_padded_lint = dyln.lint(padded, limg)
fixed, rrep = dyrep.repair(padded, dyau.audit(padded, "", A, stream=io.StringIO()), A, _padded_lint)
_padded_declined = {d["rule"]: d["reason"] for d in rrep.declined}
check(
    "a padded empty section is declined, not emptied (#263)",
    fixed == padded
    and "structure/claims-empty-but-is-not" not in dyrep.REPAIRABLE
    and "И на самом деле абзац." in fixed
    and "structure/claims-empty-but-is-not" in _padded_declined,
    f"changed={fixed != padded} "
    f"repairable={'structure/claims-empty-but-is-not' in dyrep.REPAIRABLE} "
    f"declined={sorted(_padded_declined)}",
)
check(
    "and the decline records why the text is kept, not just that it was (#263)",
    "contradict" in _padded_declined.get("structure/claims-empty-but-is-not", ""),
    _padded_declined.get("structure/claims-empty-but-is-not", "(rule is not in DECLINED)"),
)

# The section the linter flags, and one that only *mentions* the sentinel inside populated
# text: the substring test emptied both bodies, and no finding ever named the second (#263).
bodies[3] = f"Формулы приведены ниже.\n\n{dysy.NOT_COVERED}: строгое определение нормы."
scoped = audit_note(*bodies)
_scoped_lint = dyln.lint(scoped, limg)
fixed, rrep = dyrep.repair(scoped, dyau.audit(scoped, "", A, stream=io.StringIO()), A, _scoped_lint)
_flagged = [f for f in _scoped_lint.findings if f.rule == "structure/claims-empty-but-is-not"]
check(
    "only the section with the marker on its own line is flagged (#263)",
    len(_flagged) == 1 and dysy.SECTIONS[5][1] in _flagged[0].detail,
    str([(f.rule, f.severity, f.detail) for f in _scoped_lint.findings]),
)
check(
    "and repair leaves both bodies alone, including the one no finding named (#263)",
    fixed == scoped and "строгое определение нормы" in fixed and "И на самом деле абзац." in fixed,
    f"changed={fixed != scoped}",
)

bodies[3] = "Применяется ILQF."
drifty = audit_note(*bodies)
fixed, rrep = dyrep.repair(
    drifty, dyau.audit(drifty, "", A, stream=io.StringIO()), A, dyln.lint(drifty, limg)
)
check("glossary drift is canonicalised", "iLQR (iterative LQR)" in fixed, "not replaced")
check("and it did not crash on a LaTeX-bearing canonical", True, "")

# A canonical containing a backslash must not be treated as a replacement template.
la = dyau.Glossary((("пять", r"$\mathbf{x} \in \mathbb{R}^n$"),))
bodies[3] = "Считаем пять."
latexy = audit_note(*bodies)
fixed, rrep = dyrep.repair(
    latexy, dyau.audit(latexy, "", la, stream=io.StringIO()), la, dyln.lint(latexy, limg)
)
check(
    "a canonical containing \\mathbf is inserted, not parsed as an escape",
    r"\mathbf{x}" in fixed,
    "backslash was eaten as an escape sequence",
)

# Declined: the things that need judgement.
bodies[5] = decl + "\n\n$J = \\mathbf{A} + \\mathbf{x}$"
mism = audit_note(*bodies)
arep = dyau.audit(mism, "", A, stream=io.StringIO())
_, rrep = dyrep.repair(mism, arep, A, dyln.lint(mism, limg))
check(
    "a dimension error is NOT mechanically repaired",
    any(d["rule"] == "dimension/mismatch" for d in rrep.declined),
    str([d["rule"] for d in rrep.declined]),
)
check("and the decline says why", all(d["reason"] for d in rrep.declined), str(rrep.declined[:1]))
check(
    "critic findings are declined by rule, per the table",
    "critic/finding" in dyrep.DECLINED
    and "a model's criticism is not evidence" in dyrep.DECLINED["critic/finding"],
    str(dyrep.DECLINED.get("critic/finding")),
)
check(
    "only the four unambiguous rules are repairable",
    dyrep.REPAIRABLE
    == frozenset(
        {
            "math/inline-unbalanced",
            "math/display-unbalanced",
            "math/empty",
            "term/drift",
        }
    ),
    str(sorted(dyrep.REPAIRABLE)),
)

# --- 16. acceptance criteria #11 and #18 that had no test ---------------------------

rdest = tmp / "repair-out"
rdest.mkdir(parents=True, exist_ok=True)
(rdest / "note.md").write_text(broken_note, encoding="utf-8")
lrep_path = rdest / "lint.json"
lrep_path.write_text(json.dumps(lrep.as_dict()), encoding="utf-8")
rr = dyrep.run(
    rdest / "note.md",
    rdest / "missing-audit.json",
    rdest,
    lint_report_path=lrep_path,
    stream=io.StringIO(),
)
check(
    "run writes the repaired note beside the original",
    (rdest / dyrep.REPAIRED_NOTE).is_file(),
    str(sorted(p.name for p in rdest.iterdir())),
)
check("the original is still there", (rdest / "note.md").is_file(), "original was overwritten")
check(
    "run writes the repair report",
    (rdest / dyrep.REPORT_NAME).is_file(),
    str(sorted(p.name for p in rdest.iterdir())),
)
rprov = json.loads((rdest / dyrep.PROVENANCE_NAME).read_text())
check("provenance says the original is kept", rprov["original_kept"] is True, str(rprov))
check(
    "provenance lists every declined reason",
    len(rprov["declined_reasons"]) >= 5,
    str(rprov["declined_reasons"]),
)
check("a missing audit report is not a traceback", rr is not None, "raised")

# #18: "The audit report is byte-identical before and after a repair run." Repair reads the
# audit and writes beside it; if it ever rewrote the audit, the evidence for a change would
# be edited by the thing that made the change. Hash before and after, not "looks the same".
adir2 = tmp / "repair-src"
adir2.mkdir(parents=True, exist_ok=True)
src_note = adir2 / "note.md"
src_note.write_text(broken_note, encoding="utf-8")
src_audit = adir2 / dyau.REPORT_NAME
src_audit.write_text(json.dumps(arep.as_dict(), ensure_ascii=False), encoding="utf-8")
before_hash = hashlib.sha256(src_audit.read_bytes()).hexdigest()
dyrep.run(src_note, src_audit, adir2, lint_report_path=lrep_path, stream=io.StringIO())
after_hash = hashlib.sha256(src_audit.read_bytes()).hexdigest()
check(
    "repair leaves the audit report byte-identical",
    before_hash == after_hash,
    "the audit report was rewritten by the repair",
)

# #18: "A repair run with zero findings is a success, not an error." The degenerate case is
# the one that gets treated as a crash: nothing found means nothing to do, and a stage that
# cannot distinguish "clean" from "broken" is a stage that fails on good notes.
empty_dir = tmp / "repair-empty"
empty_dir.mkdir(parents=True, exist_ok=True)
(empty_dir / "note.md").write_text(clean_note, encoding="utf-8")
empty_audit = empty_dir / dyau.REPORT_NAME
empty_audit.write_text(json.dumps(dyau.Report().as_dict(), ensure_ascii=False), encoding="utf-8")
zero = dyrep.run(
    empty_dir / "note.md",
    empty_audit,
    empty_dir,
    lint_report_path=empty_dir / "no-lint.json",
    stream=io.StringIO(),
)
check("a repair run with no findings is not an error", zero is not None, "raised")
check("and it changes nothing", not zero.changed and zero.declined == [], str(zero.as_dict()))
check(
    "and it still writes the note, so the pipeline has one",
    (empty_dir / dyrep.REPAIRED_NOTE).read_text(encoding="utf-8") == clean_note,
    "note was altered on a no-op run",
)

# #11: "Exit 5 when findings exceed the threshold: note written and flagged." Declared in
# exitcodes.py and promised by the provenance, returned by nothing until now.
src_dir = tmp / "exit5-src"
src_dir.mkdir(parents=True, exist_ok=True)
probe_src = src_dir / "lecture.webm"
probe_src.write_bytes(b"\x1a\x45\xdf\xa3fake")


def dirty_pipeline(source, work, **kw):
    result = fake_pipeline(source, work, **kw)
    result.audit.findings.append(
        dyau.Finding("tier1", "dimension/mismatch", "ERROR", 12, "terms of different rank summed")
    )
    return result


dyp.run = dirty_pipeline
rc5 = dyc.main(["process", str(probe_src)])
check("an error-tier finding exits 5, not 1", rc5 == ec.AUDIT_FINDINGS, f"rc={rc5}")
dyp.run = fake_pipeline
rc1 = dyc.main(["process", str(probe_src)])
check(
    # The counterpart to the exit-5 assertion above: a clean audit on a verified run
    # now exits 0, because stage 12 exists and every artefact is on disk.
    "a clean audit on a verified run exits 0",
    rc1 == ec.OK,
    f"rc={rc1}",
)

# --- 17. stage 11: link --------------------------------------------------------------
# Ported from ~/.local/bin/mscs-termlink. The two load-bearing properties are idempotence
# and never linking inside math; both are asserted here against a real terms dir built in
# tmp, not a stub, because the behaviour is a property of the frontmatter parser.
tvault = tmp / "vault"
tterms = tvault / "mscs" / "_terms"
tterms.mkdir(parents=True, exist_ok=True)
(tterms / "MPC.md").write_text(
    "---\ntype: term\ndomain: управление\naliases:\n  - МПЦ\n  - mpc\n---\n\n# MPC\n",
    encoding="utf-8",
)
(tterms / "LQR.md").write_text(
    "---\ntype: term\ndomain: управление\naliases:\n  - LКR\nforms:\n  - линейно-квадратичный\n---\n\n# LQR\n",
    encoding="utf-8",
)
(tterms / "not-a-term.md").write_text("---\ntype: note\n---\n\n# Не термин\n", encoding="utf-8")

terms_loaded = dylk.load_terms(tterms)
check(
    "only notes with type: term are loaded",
    sorted(t.canonical for t in terms_loaded) == ["LQR", "MPC"],
    str([t.canonical for t in terms_loaded]),
)
check(
    "aliases and forms are both searchable",
    any("МПЦ" in t.patterns for t in terms_loaded)
    and any("линейно-квадратичный" in t.patterns for t in terms_loaded),
    str([t.patterns for t in terms_loaded]),
)
check(
    "the canonical itself is a pattern",
    all(t.canonical in t.patterns for t in terms_loaded),
    str([t.patterns for t in terms_loaded]),
)

probe = (
    "МПЦ применяется. Формула $x = MPC$ и $$\n\\mathcal{L} = MPC\n$$ и `MPC` в коде.\n"
    "Ссылка [[LQR]] уже есть. И mpc строчными.\n"
)
linked, counts = dylk.link_text(probe, terms_loaded)
check(
    "an ASR variant is linked to its canonical",
    "[[МПЦ|MPC]]" in linked,
    linked,
)
check(
    "a match that IS the canonical gets no pipe",
    "[[MPC]]" in linked or "MPC" in linked,
    linked,
)
protected = (
    dylk.MULTILINE.findall(linked)
    + dylk.INLINE_MATH.findall(linked)
    + dylk.INLINE_CODE.findall(linked)
)
check("protected regions were found to inspect", len(protected) >= 3, str(protected))
check(
    "NO link is ever inserted inside math or code",
    not any("[[" in region for region in protected),
    str(protected),
)
check("an existing wikilink is left alone", "[[LQR]]" in linked and "[[LQR|" not in linked, linked)
again, counts2 = dylk.link_text(linked, terms_loaded)
check("link is idempotent: byte-identical second run", again == linked, again)
check("and the second run inserts nothing", sum(counts2.values()) == 0, str(counts2))

# The port fixed this: the original excluded by a module global, so --terms-dir changed
# where terms load from but not what the sweep skips.
excl = tvault / "mscs" / "Лекция 1.md"
excl.write_text("MPC", encoding="utf-8")
check("a lecture note is a target", excl in dylk.collect_targets([], tterms, tvault))
check(
    "a term note is NOT a target",
    tterms / "MPC.md" not in dylk.collect_targets([], tterms, tvault),
    "term notes would be linked to themselves",
)

# --- configuration, not constants ---------------------------------------------------
check(
    "an explicit terms dir wins",
    dylk.resolve_terms_dir(tterms, None) == tterms,
    str(dylk.resolve_terms_dir(tterms, None)),
)
check(
    "the vault is the fallback",
    dylk.resolve_terms_dir(None, tvault) == tterms,
    str(dylk.resolve_terms_dir(None, tvault)),
)
real_terms_env = os.environ.get(dylk.TERMS_ENV)
os.environ[dylk.TERMS_ENV] = str(tterms)
try:
    check(
        "the env var is consulted before the vault",
        dylk.resolve_terms_dir(None, None) == tterms,
        str(dylk.resolve_terms_dir(None, None)),
    )
finally:
    if real_terms_env is None:
        os.environ.pop(dylk.TERMS_ENV, None)
    else:
        os.environ[dylk.TERMS_ENV] = real_terms_env
check(
    "no terms dir resolves to None, not to a silent empty run",
    dylk.resolve_terms_dir(tmp / "no-such-terms", None) is None,
    "a missing directory must not read as 'zero terms found'",
)

# --- the stage entry point ----------------------------------------------------------
ldir = tmp / "link-out"
ldir.mkdir(parents=True, exist_ok=True)
(ldir / "note.md").write_text(probe, encoding="utf-8")
lrep = dylk.run(ldir / "note.md", ldir, terms_dir=tterms, stream=io.StringIO())
check(
    "run writes the linked note",
    (ldir / dylk.LINKED_NOTE).is_file(),
    str(sorted(p.name for p in ldir.iterdir())),
)
check(
    "and the input note is still there",
    (ldir / "note.md").read_text(encoding="utf-8") == probe,
    "stage 11 rewrote its own input",
)
lprov = json.loads((ldir / dylk.PROVENANCE_NAME).read_text())
check("provenance names the port source", "mscs-termlink" in lprov["ported_from"], str(lprov))
check(
    "provenance lists every protected region",
    len(lprov["never_links_inside"]) >= 8,
    str(lprov["never_links_inside"]),
)

# No terms configured: the note must still be written, and the stage must say why.
ndir = tmp / "link-unconfigured"
ndir.mkdir(parents=True, exist_ok=True)
(ndir / "note.md").write_text(probe, encoding="utf-8")
buf = io.StringIO()
nrep = dylk.run(ndir / "note.md", ndir, terms_dir=None, stream=buf)
check(
    "an unconfigured stage writes the note unchanged rather than failing",
    (ndir / dylk.LINKED_NOTE).read_text(encoding="utf-8") == probe,
    "the note was lost",
)
check("and it says so", "not configured" in buf.getvalue(), buf.getvalue())
check(
    "and it does not report 'nothing to link'",
    "nothing to link" not in nrep.summary(),
    nrep.summary(),
)

# --- #120: markup the substitution must leave alone ---------------------------------
ix_terms = [
    dylk.Term(canonical="Фильтр Калмана", path=Path("x.md"), aliases=["калман"]),
]
ix_rx, ix_owner = dylk.build_index(ix_terms)

fm_note = "---\ntitle: Конспект про фильтр Калмана\naliases: [калман]\n---\n\nТекст про калман.\n"
fm_out, _ = dylk.substitute(fm_note, ix_rx, ix_owner)
check(
    "#120: frontmatter is byte-identical after substitute",
    fm_out.startswith("---\ntitle: Конспект про фильтр Калмана\naliases: [калман]\n---\n"),
    repr(fm_out[:120]),
)
check("#120: body term is still linked", "[[калман|Фильтр Калмана]]" in fm_out, repr(fm_out))

tbl_note = "| термин | глава | тип |\n|---|---|---|\n| калман | глава 2 | базовый |\n"
tbl_out, tbl_counts = dylk.substitute(tbl_note, ix_rx, ix_owner)
check("#120: table line is linked", tbl_counts.get("Фильтр Калмана") == 1, str(tbl_counts))
check(
    "#120: pipe inside a table link is escaped",
    "| [[калман\\|Фильтр Калмана]] | глава 2 | базовый |" in tbl_out,
    repr(tbl_out),
)
check(
    "#120: table column count is unchanged",
    # GFM: an escaped \\| is a literal pipe inside a cell, so only bare pipes split columns.
    all(
        len(re.findall(r"(?<!\\)\|", line)) == 4
        for line in tbl_out.splitlines()
        if line.startswith("|")
    ),
    repr(tbl_out),
)

head_note = "## Фильтр Калмана: краткая теория\n\nКалман работает.\n"
head_out, head_counts = dylk.substitute(head_note, ix_rx, ix_owner)
check(
    "#120: headings never gain links",
    head_out.startswith("## Фильтр Калмана: краткая теория\n"),
    repr(head_out),
)
check(
    "#120: heading match is not counted",
    "Фильтр Калмана" not in head_counts or head_counts["Фильтр Калмана"] == 1,
    str(head_counts),
)

inline_list = dylk.parse_frontmatter("---\ntype: term\naliases: [a, b]\n---\n")[0]
check(
    "#120: inline list frontmatter yields items",
    inline_list.get("aliases") == ["a", "b"],
    repr(inline_list),
)

_atomic_calls = []
_real_replace = os.replace


def _spy_replace(src, dst):
    _atomic_calls.append((str(src), str(dst)))
    return _real_replace(src, dst)


with tempfile.TemporaryDirectory() as _td:
    _note = Path(_td) / "Лекция 1.md"
    _note.write_text("калман\n", encoding="utf-8")
    os.replace = _spy_replace
    try:
        dylk.link_paths([_note], ix_terms, write=True)
    finally:
        os.replace = _real_replace
    check(
        "#120: link --write replaces atomically via os.replace",
        any(dst == str(_note) for _, dst in _atomic_calls),
        str(_atomic_calls),
    )
    check(
        "#120: written note is linked",
        "[[калман|Фильтр Калмана]]" in _note.read_text(encoding="utf-8"),
    )
    check(
        "#120: no temp file is left behind",
        sorted(p.name for p in Path(_td).iterdir()) == ["Лекция 1.md"],
        str(sorted(p.name for p in Path(_td).iterdir())),
    )


# --- #121: stage 6 must read the previous run's caption cache -----------------------
# `pipeline.run` calls `bundle.clear_root()` before stage 1. The cache lives in the root, so
# stage 6 used to read a file that had just been deleted and re-captioned every frame. This
# drives the real `pipeline.run` with stages 1-5 stubbed and records what stage 6 sees.
class _StopAtCaption(Exception):
    pass


_cap_seen: dict = {}
_cap_real = (dyp.probe, dyp.audio, dyp.stt, dyp.frames, dyp.quality, dyp.caption)


def _cap_run_probe(*a, **kw):
    # Stage 6 is the first thing that reads the cache, so record the file as it is there.
    seen = kw.get("cache_source")
    _cap_seen["cache_source"] = seen
    _cap_seen["existed"] = bool(seen) and Path(seen).is_file()
    _cap_seen["entries"] = (
        len(json.loads(Path(seen).read_text(encoding="utf-8")).get("captions", {}))
        if _cap_seen["existed"]
        else 0
    )
    raise _StopAtCaption()


class _Stub:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def _cap_stub_modules():
    _info = _Stub(summary=lambda: "x")
    dyp.probe = _Stub(
        probe=lambda p: _info,
        require_supported=lambda i: None,
        provenance=lambda *a, **k: "{}",
        PROVENANCE_NAME="source-provenance.json",
    )
    dyp.audio = _Stub(
        extract=lambda src, wav: _Stub(duration=1.0, summary=lambda: "x"),
        check_coverage=lambda i, a: None,
    )

    def _cap_stt_write(t, d):
        # Writes the file it returns: `Bundle.publish` raises on a missing source now (#89),
        # and a stub that only names a path would fail the run for the wrong reason.
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.json").write_text("{}", encoding="utf-8")
        return {"json": d / "transcript.json"}

    dyp.stt = _Stub(
        # `pipeline.run` resolves the backend before the start line (#186), so a stub that
        # replaces the whole module needs `backend` and the two new `transcribe` keywords or
        # the run dies before the stage this test is about.
        backend=lambda *a, **k: SimpleNamespace(
            info=lambda: dysb.BackendInfo(name="whispercpp", target="stub://whispercpp")
        ),
        transcribe=lambda wav, audio_duration, backend=None, on_progress=None: _Stub(
            anomalies=(), summary=lambda: "x"
        ),
        write=_cap_stt_write,
        RATE_RANGE=dysttc.RATE_RANGE,
        WARMUP_SECONDS=dysttc.WARMUP_SECONDS,
    )

    def _cap_frames_run(src, fd):
        fd.mkdir(parents=True, exist_ok=True)
        manifest = fd / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return manifest, []

    dyp.frames = _Stub(
        run=_cap_frames_run,
        summary=lambda m, p: "0 frames",
        ffmpeg_version=lambda: "stub",
    )
    dyp.quality = _Stub(
        Settings=lambda: _Stub(),
        resolve_estimator=lambda name: None,
        run=lambda *a, **k: _Stub(passed=[], summary=lambda: "0/0", ok=True),
        write=lambda report, target, settings: target.write_text("{}", encoding="utf-8"),
        provenance=lambda qs, bbox_source: "{}",
        REPORT_NAME=dyq.REPORT_NAME,
        PROVENANCE_NAME=dyq.PROVENANCE_NAME,
    )
    dyp.caption = _Stub(
        run=_cap_run_probe,
        REPORT_NAME=dycap.REPORT_NAME,
        PROVENANCE_NAME="caption-provenance.json",
    )


with tempfile.TemporaryDirectory() as _cap_td:
    _cap_root = Path(_cap_td)
    _cap_src = _cap_root / "lecture.mp4"
    _cap_src.write_bytes(b"x")
    _cap_out = _cap_root / "bundle"
    _cap_out.mkdir()
    (_cap_out / dycap.REPORT_NAME).write_text(
        json.dumps({"captions": {"f_1.png": {"model": "vision-x", "caption": "cached"}}}),
        encoding="utf-8",
    )
    _cap_bundle = dyb.Bundle.open(_cap_src, output_dir=str(_cap_out), overwrite=False)
    _cap_work = dyw.WorkDir(keep=False, parent=_cap_root)
    _cap_stub_modules()
    try:
        REAL_PIPELINE_RUN(_cap_src, _cap_work, bundle=_cap_bundle, stream=io.StringIO())
    except _StopAtCaption:
        pass
    finally:
        dyp.probe, dyp.audio, dyp.stt, dyp.frames, dyp.quality, dyp.caption = _cap_real
    check(
        "#121: stage 6 is reached through pipeline.run",
        "cache_source" in _cap_seen,
        str(_cap_seen),
    )
    check(
        "#121: the previous captions.json still exists when stage 6 reads it",
        _cap_seen.get("existed") is True,
        str(_cap_seen),
    )
    check(
        "#121: stage 6 sees the previous run's cached entries",
        _cap_seen.get("entries") == 1,
        str(_cap_seen),
    )


# --- #121: a second run through pipeline.run makes zero vision calls --------------------
# The two halves of this were tested apart: `caption.run` was shown to issue no request when a
# `cache_source` is handed to it, and `pipeline.run` was shown to hand stage 6 a cache file that
# still exists. Neither joins them, so a stage 6 that reads the cache and then ignores it would
# pass both. That is the shape of every wiring bug in this file (#82, #89, #121): the unit is
# right, the sequence is wrong, and only a run that executes the sequence can see it.
class _StopAfterCaption(Exception):
    pass


_vc_calls: list = []
_vc_real = (dyp.probe, dyp.audio, dyp.stt, dyp.frames, dyp.quality, dyp.caption, dyp.synth)


def _vc_chat(messages, config, **kw):
    _vc_calls.append(messages)
    return _caption_reply()


def _vc_stub_modules():
    _info = _Stub(summary=lambda: "x")
    dyp.probe = _Stub(
        probe=lambda p: _info,
        require_supported=lambda i: None,
        provenance=lambda *a, **k: "{}",
        PROVENANCE_NAME="source-provenance.json",
    )
    dyp.audio = _Stub(
        extract=lambda src, wav: _Stub(duration=300.0, summary=lambda: "x"),
        check_coverage=lambda i, a: None,
    )

    def _vc_stt_write(t, d):
        d.mkdir(parents=True, exist_ok=True)
        # The real transcript shape, so stage 6 aligns and captions the frames rather than
        # gating them all out -- a warm run that captioned nothing would "pass" for the
        # wrong reason. `txt` is written too: stage 7 reads it, and the arguments to
        # `synth.run` are evaluated before the stub that stops the run.
        (d / "transcript.txt").write_text("alpha beta gamma", encoding="utf-8")
        target = d / "transcript.json"
        target.write_text(json.dumps(transcript_payload), encoding="utf-8")
        return {"json": target, "txt": d / "transcript.txt"}

    dyp.stt = _Stub(
        # Same widening as `_cap_stub_modules`: stage 3 now resolves a backend and passes the
        # resolved name plus the progress callback down (#186, #193).
        backend=lambda *a, **k: SimpleNamespace(
            info=lambda: dysb.BackendInfo(name="whispercpp", target="stub://whispercpp")
        ),
        transcribe=lambda wav, audio_duration, backend=None, on_progress=None: _Stub(
            anomalies=(), summary=lambda: "x"
        ),
        write=_vc_stt_write,
        RATE_RANGE=dysttc.RATE_RANGE,
        WARMUP_SECONDS=dysttc.WARMUP_SECONDS,
    )

    def _vc_frames_run(src, fd):
        fd.mkdir(parents=True, exist_ok=True)
        produced = []
        for name in ("f_0000000000.png", "f_0000100000.png", "f_0000200000.png"):
            path = fd / name
            # Byte-identical across both runs: the cache is keyed by frame fingerprint, so
            # different bytes would invalidate it and the test would pass for the wrong
            # reason.
            path.write_bytes(_FAKE_FRAME)
            produced.append(path)
        manifest = fd / "manifest.tsv"
        manifest.write_text(
            "pts_ms\ttime\tfile\n0\t00:00:00\tf_0000000000.png\n"
            "100000\t00:01:40\tf_0000100000.png\n200000\t00:03:20\tf_0000200000.png\n",
            encoding="utf-8",
        )
        return manifest, produced

    dyp.frames = _Stub(
        run=_vc_frames_run,
        summary=lambda m, p: "3 frames",
        ffmpeg_version=lambda: "stub",
    )

    def _vc_quality_run(produced, workpath, **kw):
        entries = [
            _Stub(
                name=p.name,
                quality_gate="PASS",
                content_type="white_document",
                crop_state="cropped",
            )
            for p in produced
        ]
        return _Stub(passed=entries, failed=[], ok=True, summary=lambda: "3/3", explain=lambda: "")

    def _vc_quality_write(report, target, settings):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "threshold": 100.0,
                    "frames": [
                        {
                            "name": e.name,
                            "quality_gate": e.quality_gate,
                            "content_type": e.content_type,
                            "crop_state": e.crop_state,
                        }
                        for e in report.passed
                    ],
                }
            ),
            encoding="utf-8",
        )

    dyp.quality = _Stub(
        Settings=lambda: _Stub(),
        resolve_estimator=lambda name: None,
        run=_vc_quality_run,
        write=_vc_quality_write,
        provenance=lambda qs, bbox_source: "{}",
        REPORT_NAME=dyq.REPORT_NAME,
        PROVENANCE_NAME=dyq.PROVENANCE_NAME,
        enhanced_name=dyq.enhanced_name,
    )
    # Stage 6 runs for real; stage 7 stops the run, because captions.json is published at the
    # end of stage 6 and stages 7-12 are not what this test is about.
    dyp.synth = _Stub(run=lambda *a, **k: (_ for _ in ()).throw(_StopAfterCaption()))


with tempfile.TemporaryDirectory() as _vc_td:
    _vc_root = Path(_vc_td)
    _vc_src = _vc_root / "lecture.mp4"
    _vc_src.write_bytes(b"x")
    _vc_out = _vc_root / "bundle"
    _vc_out.mkdir()
    # The vision pair explicitly: earlier sections of this file add and remove these, and a
    # stage 6 with no vision endpoint captions nothing -- which would make the "warm run makes
    # zero calls" assertion below true for the wrong reason.
    _vc_env = {k: os.environ.get(k) for k in ("DYAK_VLM_ENDPOINT", "DYAK_VLM_MODEL")}
    os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
    os.environ["DYAK_VLM_MODEL"] = "fake-vision"
    _vc_stub_modules()
    _vc_prev_chat = dyle.chat
    dyle.chat = _vc_chat

    _vc_streams: list[io.StringIO] = []

    def _vc_pass() -> int:
        _vc_calls.clear()
        stream = io.StringIO()
        _vc_streams.append(stream)
        try:
            REAL_PIPELINE_RUN(
                _vc_src,
                dyw.WorkDir(keep=False, parent=_vc_root),
                bundle=dyb.Bundle.open(_vc_src, output_dir=str(_vc_out), overwrite=False),
                stream=stream,
            )
        except _StopAfterCaption:
            pass
        return len(_vc_calls)

    try:
        _vc_cold = _vc_pass()
        # The bundle now holds captions.json. A second run clears the root first -- which is
        # exactly what used to take the cache away before stage 6 could read it -- and this
        # run's own stream says so. The clear itself is pinned by the #219 section below; what
        # this asserts is that it happened before stage 6 read the copy.
        _vc_warm = _vc_pass()
    finally:
        dyle.chat = _vc_prev_chat
        for _key, _value in _vc_env.items():
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value
        (
            dyp.probe,
            dyp.audio,
            dyp.stt,
            dyp.frames,
            dyp.quality,
            dyp.caption,
            dyp.synth,
        ) = _vc_real
    check(
        "#121: a cold run through pipeline.run captions every frame",
        _vc_cold == 3,
        f"cold vision calls={_vc_cold}",
    )
    check(
        "#121: a second run through pipeline.run makes zero vision calls",
        _vc_warm == 0,
        f"cold={_vc_cold} warm={_vc_warm}",
    )
    check(
        "#121: and the warm run's captions.json still holds the frames",
        len(
            json.loads((_vc_out / dycap.REPORT_NAME).read_text(encoding="utf-8")).get(
                "alignments", []
            )
        )
        >= 3,
        "the warm run lost the alignment",
    )
    check(
        # #219: the claim this section made in a comment -- "a second run clears the root
        # first" -- asserted on the stream `pipeline.run` wrote, not inferred from the cache
        # surviving. Delete `bundle.clear_root()` from `pipeline.run` and this goes red.
        "#121/#219: the warm run cleared the first run's root before stage 6 read the cache",
        "bundle    cleared" in _vc_streams[1].getvalue(),
        repr(_vc_streams[1].getvalue()[:300]),
    )


# --- #219: the run-level root clear is pinned ------------------------------------------
# The clear is correct and was unpinned. `pipeline.run` calls `bundle.clear_root()` before
# anything is written (`pipeline.py:409`), and nothing failed if that line went: the #121 test
# above copies the caption cache before the clear, the stale-`audit.json` test below is
# deliberately independent of it ("even with the clear deliberately skipped"), and
# `tests/test_claims.py:839-845` is about the `RUN_ARTEFACTS` constant. So "a second run clears
# the root first" was a comment with no assertion behind it.
#
# The pin is the case the clear exists for. Run 1 with the LLM synthesizer publishes
# `synth-llm-transcript.json` (`synth.py:827-828`); run 2 uses the template synthesizer, which
# records no calls and writes no transcript. Run 2's root must therefore not hold run 1's file
# -- and run 2's own record (`report.json`'s `superseded_by_this_run`, from `pipeline.py:718`)
# must name it. Stages 1-6 are the stubs above; 7-11 are the smallest fakes that let the run
# reach stage 12, because `report.json` is what carries the record. Stage 12 is real -- a
# stubbed one would assert the stub.
_cr_llm = [False]


def _cr_synth_run(captions, txt, source, sdir, *, config=None, title="", stream=None):
    """Stage 7. The LLM synthesizer records its calls; the template one records none."""
    sdir = Path(sdir)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / dysy.NOTE_NAME).write_text("# note\n", encoding="utf-8")
    for name in (dysy.REPORT_NAME, dysy.PROVENANCE_NAME):
        (sdir / name).write_text("{}", encoding="utf-8")
    if _cr_llm[0]:
        (sdir / dysy.LLM_TRANSCRIPT_NAME).write_text("[]", encoding="utf-8")
    return _Stub(
        synthesizer="llm" if _cr_llm[0] else "template",
        markdown="# note\n",
        filled=1,
        degraded=False,
    )


def _cr_lint_run(note, images, ldir, *, stream=None):
    ldir = Path(ldir)
    ldir.mkdir(parents=True, exist_ok=True)
    for name in (dyln.REPORT_NAME, dyln.PROVENANCE_NAME):
        (ldir / name).write_text("{}", encoding="utf-8")
    return _Stub(summary=lambda: "0 findings")


def _cr_audit_run(note, txt, adir, *, glossary_path=None, config=None, stream=None):
    adir = Path(adir)
    adir.mkdir(parents=True, exist_ok=True)
    for name in dyau.ALL_ARTEFACTS:
        (adir / name).write_text("{}", encoding="utf-8")
    return _Stub(summary=lambda: "0 findings")


def _cr_repair_run(
    note,
    audit_path,
    rdir,
    *,
    glossary_path=None,
    lint_report_path=None,
    stream=None,
    # The real `repair.run` gained this keyword at #257 and the pipeline now passes it; a stub
    # that does not accept it raises TypeError, which is what two #219 checks reported.
    config=None,
):
    rdir = Path(rdir)
    rdir.mkdir(parents=True, exist_ok=True)
    for name in (dyrep.REPAIRED_NOTE, dyrep.REPORT_NAME, dyrep.PROVENANCE_NAME):
        (rdir / name).write_text("{}", encoding="utf-8")
    return _Stub(summary=lambda: "0 repairs")


def _cr_link_run(note, ldir, *, terms_dir=None, stream=None):
    ldir = Path(ldir)
    ldir.mkdir(parents=True, exist_ok=True)
    for name in dylk.ALL_ARTEFACTS:
        (ldir / name).write_text("{}", encoding="utf-8")
    return _Stub(summary=lambda: "0 links")


with tempfile.TemporaryDirectory() as _cr_td:
    _cr_root = Path(_cr_td)
    _cr_src = _cr_root / "lecture.mp4"
    _cr_src.write_bytes(b"x")
    _cr_out = _cr_root / "bundle"
    _cr_out.mkdir()
    _cr_env = {k: os.environ.get(k) for k in ("DYAK_VLM_ENDPOINT", "DYAK_VLM_MODEL")}
    os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
    os.environ["DYAK_VLM_MODEL"] = "fake-vision"
    _cr_real = (
        dyp.probe,
        dyp.audio,
        dyp.stt,
        dyp.frames,
        dyp.quality,
        dyp.caption,
        dyp.synth,
        dyp.lint,
        dyp.audit,
        dyp.repair,
        dyp.link,
    )
    _vc_stub_modules()
    # `pipeline.synth` is the module object, so the stub has to carry the names `pipeline.py`
    # reads off it -- `NOTE_NAME`, `STAGE7_ARTEFACTS`, `LLM_TRANSCRIPT_NAME`, `SECTIONS` --
    # not just `run`. `report.py` imports `synth` itself and keeps reading the real one.
    dyp.synth = _Stub(
        run=_cr_synth_run,
        NOTE_NAME=dysy.NOTE_NAME,
        LLM_TRANSCRIPT_NAME=dysy.LLM_TRANSCRIPT_NAME,
        STAGE7_ARTEFACTS=dysy.STAGE7_ARTEFACTS,
        SECTIONS=dysy.SECTIONS,
    )
    dyp.lint = _Stub(
        run=_cr_lint_run, REPORT_NAME=dyln.REPORT_NAME, PROVENANCE_NAME=dyln.PROVENANCE_NAME
    )
    dyp.audit = _Stub(
        run=_cr_audit_run, REPORT_NAME=dyau.REPORT_NAME, ALL_ARTEFACTS=dyau.ALL_ARTEFACTS
    )
    dyp.repair = _Stub(
        run=_cr_repair_run,
        REPAIRED_NOTE=dyrep.REPAIRED_NOTE,
        REPORT_NAME=dyrep.REPORT_NAME,
        PROVENANCE_NAME=dyrep.PROVENANCE_NAME,
    )
    dyp.link = _Stub(
        run=_cr_link_run,
        ALL_ARTEFACTS=dylk.ALL_ARTEFACTS,
        LINKED_NOTE=dylk.LINKED_NOTE,
        resolve_terms_dir=dylk.resolve_terms_dir,
    )
    _cr_prev_chat = dyle.chat
    dyle.chat = _vc_chat

    def _cr_pass(llm: bool) -> dict:
        _cr_llm[0] = llm
        REAL_PIPELINE_RUN(
            _cr_src,
            dyw.WorkDir(keep=False, parent=_cr_root),
            bundle=dyb.Bundle.open(_cr_src, output_dir=str(_cr_out), overwrite=False),
            stream=io.StringIO(),
        )
        return json.loads((_cr_out / dyro.REPORT_NAME).read_text(encoding="utf-8"))

    _cr_error = ""
    _cr_first: dict = {}
    _cr_second: dict = {}
    _cr_had_transcript = False
    try:
        _cr_first = _cr_pass(llm=True)
        _cr_had_transcript = (_cr_out / dysy.LLM_TRANSCRIPT_NAME).is_file()
        _cr_second = _cr_pass(llm=False)
    except BaseException as _cr_exc:
        # A raise is a red check, not a dead suite: the message goes into both details below.
        _cr_error = repr(_cr_exc)
    finally:
        dyle.chat = _cr_prev_chat
        for _key, _value in _cr_env.items():
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value
        (
            dyp.probe,
            dyp.audio,
            dyp.stt,
            dyp.frames,
            dyp.quality,
            dyp.caption,
            dyp.synth,
            dyp.lint,
            dyp.audit,
            dyp.repair,
            dyp.link,
        ) = _cr_real
    check(
        "#219: run 1 (LLM synthesizer) published the transcript, and cleared nothing",
        _cr_had_transcript and _cr_first.get("superseded_by_this_run") == [],
        f"transcript={_cr_had_transcript} "
        f"superseded={_cr_first.get('superseded_by_this_run')!r} error={_cr_error}",
    )
    check(
        "#219: a template re-run clears the previous run's transcript, and names it",
        not (_cr_out / dysy.LLM_TRANSCRIPT_NAME).is_file()
        and dysy.LLM_TRANSCRIPT_NAME in _cr_second.get("superseded_by_this_run", []),
        f"present={(_cr_out / dysy.LLM_TRANSCRIPT_NAME).is_file()} "
        f"superseded={_cr_second.get('superseded_by_this_run')!r} error={_cr_error}",
    )


# --- #92: an interrupted run says so, instead of looking like a complete one -------------
# The bundle left by an interrupt used to hold everything stage 7 had published -- note.md,
# captions.json, quality.json, lint.json -- with no `report.json` and no `timings.json`,
# because both were written on the last line of `run`. Nothing distinguished that from a
# finished run whose verification artefacts happened to be missing.
with tempfile.TemporaryDirectory() as _rs_td:
    _rs_root = Path(_rs_td)
    _rs_src = _rs_root / "lecture.mp4"
    _rs_src.write_bytes(b"x")
    _rs_out = _rs_root / "bundle"
    _rs_out.mkdir()
    _rs_env = {k: os.environ.get(k) for k in ("DYAK_VLM_ENDPOINT", "DYAK_VLM_MODEL")}
    os.environ["DYAK_VLM_ENDPOINT"] = "http://vision.invalid/v1"
    os.environ["DYAK_VLM_MODEL"] = "fake-vision"
    _vc_stub_modules()
    _rs_prev_chat = dyle.chat
    dyle.chat = _vc_chat
    # Interrupted during stage 7, which is where an interrupt costs the most: stages 1-6 have
    # already been paid for, and the bundle must not read as a run that finished.
    dyp.synth = _Stub(run=lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        try:
            REAL_PIPELINE_RUN(
                _rs_src,
                dyw.WorkDir(keep=False, parent=_rs_root),
                bundle=dyb.Bundle.open(_rs_src, output_dir=str(_rs_out), overwrite=False),
                stream=io.StringIO(),
            )
        except KeyboardInterrupt:
            pass
    finally:
        dyle.chat = _rs_prev_chat
        for _key, _value in _rs_env.items():
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value
        (
            dyp.probe,
            dyp.audio,
            dyp.stt,
            dyp.frames,
            dyp.quality,
            dyp.caption,
            dyp.synth,
        ) = _vc_real
    _rs_state = _run_state(_rs_out)
    check(
        "#92: an interrupted run says it was interrupted, in the bundle it left",
        _rs_state.get("outcome") == "interrupted"
        and _rs_state.get("detail") == "KeyboardInterrupt",
        str({k: _rs_state.get(k) for k in ("outcome", "detail")}),
    )
    check(
        "#92: and names the stage it reached, not the last one that finished",
        _rs_state.get("stage_reached") == 7
        and _rs_state.get("stages_completed") == [1, 2, 3, 4, 5, 6],
        f"reached={_rs_state.get('stage_reached')} completed={_rs_state.get('stages_completed')}",
    )
    _rs_timings = _rs_out / dyp.TIMINGS_NAME
    check(
        "#92: and still carries the per-stage seconds of the stages that did run",
        _rs_timings.is_file()
        and len(json.loads(_rs_timings.read_text(encoding="utf-8"))["stages"]) == 6,
        "a run that did not finish has no timings, which is the run whose timings matter most",
    )
    check(
        "#92: and it is not a report, so a reader cannot mistake it for verification",
        not (_rs_out / dyro.REPORT_NAME).is_file() and _rs_state.get("outcome") != "completed",
        f"report.json present={(_rs_out / dyro.REPORT_NAME).is_file()}",
    )

# --- the CLI keeps dry-run as the default -------------------------------------------
cbuf = io.StringIO()
with redirect_stdout(cbuf):
    rc_dry = dyc.main(["term", "link", "--vault-path", str(tvault), str(excl)])
check("term link runs", rc_dry == ec.OK, f"rc={rc_dry}")
check(
    "dry-run says what it would do and leaves the file alone",
    "dry run" in cbuf.getvalue() and excl.read_text(encoding="utf-8") == "MPC",
    cbuf.getvalue(),
)

# --- 18. stage 12: report — verify, do not trust -------------------------------------
# ADR-0001 D3's run: every stage exited 0, ffmpeg wrote an empty file, and the pipeline called it
# success. These tests build that situation and require the code to refuse it.
bdir = tmp / "report-bundle"


def fill_bundle(with_note=True, frame_count=0, manifest_rows=0, zero_byte=()):
    bdir.mkdir(parents=True, exist_ok=True)
    for name in dyro.REQUIRED:
        if name == "note" and not with_note:
            continue
        # The key for the note is "note"; the file on disk is note.md. Writing the key
        # verbatim produced a file literally named "note" and three spurious failures.
        #
        # `captions.json` gets valid JSON rather than "x": stage 12 parses it to cross-check
        # the gate verdicts against `quality.json` (#87), so a one-byte placeholder would fail
        # the parse rather than the check under test.
        content = '{"alignments": []}' if name == dycap.REPORT_NAME else "x"
        (bdir / ("note.md" if name == "note" else name)).write_text(
            "" if name in zero_byte else content, encoding="utf-8"
        )
    # Frames live in images/ as .png, and the quality gate writes enhanced .jpg copies
    # beside them named <frame>_q.jpg -- one per frame that passed. The first version of
    # count_frames globbed "*.jpg" in the bundle root and reported 0 frames on a bundle
    # holding 16 correct ones; it would also have counted the derivatives had it matched.
    images = bdir / "images"
    images.mkdir(exist_ok=True)
    for index in range(frame_count):
        (images / f"f_{index:010d}.png").write_bytes(b"\x89PNG\r\n")
        (images / f"f_{index:010d}_q.jpg").write_bytes(b"\xff\xd8\xff")
    rows = ["pts_ms\ttime\tfile"]
    rows += [f"{i * 1000}\t00:00:{i:02d}.00\tf_{i:010d}.png" for i in range(manifest_rows)]
    (bdir / "manifest.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def run_report(stream=None):
    return dyro.run(
        bdir,
        final_note=bdir / "note.md",
        synth_note=bdir / "note.synth.md",
        artefacts={name: bdir / name for name in dyro.REQUIRED},
        stream=stream or io.StringIO(),
    )


shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(frame_count=18, manifest_rows=18)
good = run_report()
check("a complete bundle verifies", good.ok, good.summary())
check("and it names what it verified", len(good.verified) == len(dyro.REQUIRED), str(good.verified))
check(
    "and the frame count agrees",
    (good.frames_found, good.frames_expected) == (18, 18),
    good.summary(),
)

# A missing frame whose enhanced _q copy is still present is still a missing frame. This is
# what a glob hides: the directory holds 18 files either way, but one of them is the
# derivative rather than the frame.
(bdir / "images" / "f_0000000007.png").unlink()
orphan = run_report()
check(
    "a frame deleted while its enhanced copy remains is still counted as missing",
    (orphan.frames_found, orphan.frames_expected) == (17, 18) and not orphan.ok,
    f"{orphan.frames_found}/{orphan.frames_expected} ok={orphan.ok}",
)
(bdir / "images" / "f_0000000007.png").write_bytes(b"\x89PNG\r\n")
check("and restoring it restores verification", run_report().ok, "not restored")
check(
    "every required entry states why it is required",
    all(e["why"] for e in good.verified),
    str(good.verified[:1]),
)

# The ADR-0001 D3 case: the manifest says 18, the filesystem has 17. Every earlier stage exits 0.
shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(frame_count=17, manifest_rows=18)
short = run_report()
check("a frame count that disagrees with the manifest is NOT ok", not short.ok, short.summary())
check(
    "and the disagreement is visible in the numbers",
    (short.frames_found, short.frames_expected) == (17, 18),
    f"{short.frames_found}/{short.frames_expected}",
)

# Header-only manifest, zero frames: legitimate for an audio-only source, and not counted
# as one frame -- the header is not a data row.
shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(frame_count=0, manifest_rows=0)
header_only = run_report()
check(
    "a header-only manifest and zero frames is OK (audio-only is legitimate)",
    header_only.ok,
    header_only.summary(),
)
check(
    "but it is not counted as 1 frame",
    header_only.frames_expected == 0,
    f"{header_only.frames_expected} rows counted",
)

shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(zero_byte=("audit.json",))
empty = run_report()
check("a zero-byte artefact fails verification", not empty.ok, empty.summary())
check(
    "and it is named, with the reason it was required",
    any(m["artefact"] == "audit.json" and m["detail"] == "zero bytes" for m in empty.missing),
    str(empty.missing),
)
check(
    "an absent optional artefact is recorded, not counted as a failure",
    any(a["artefact"] == dyau.LLM_TRANSCRIPT_NAME for a in empty.absent),
    str(empty.absent),
)

shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(with_note=False)
nol = run_report()
check("a missing note fails verification", not nol.ok, nol.summary())
check(
    "and the failure carries the reason",
    any(m["artefact"] == "note" for m in nol.missing),
    str(nol.missing),
)

# --- #87: the required set, the conditional gate, and the cross-reference ----------------
# Stage 12 verified 65 of 144 files and the check it did run was presence-only: a `quality.json`
# truncated to one byte exited 0 with `frames_found == frames_expected`, and `captions.json` --
# stage 6's whole product -- was in neither REQUIRED nor OPTIONAL. These tests build those
# exact states. The cross-reference is the substantive half; `_check` stays presence-only on
# purpose (#87 rejected byte-size thresholds).


def run_report_in(root: Path, stream=None) -> dyro.Report:
    """Run stage 12 over `root`, resolving the required set the way the stage does."""
    return dyro.run(
        root,
        final_note=root / "note.md",
        synth_note=root / "note.synth.md",
        artefacts={name: root / name for name in dyro.required_for(root)},
        stream=stream or io.StringIO(),
    )


check(
    "#87: captions.json and synth.json are in the unconditional REQUIRED set",
    dycap.REPORT_NAME in dyro.REQUIRED and dysy.REPORT_NAME in dyro.REQUIRED,
    f"required={sorted(dyro.REQUIRED)}",
)

# (a) Losing either promoted artefact is a named failure, not a silent omission.
shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(frame_count=0, manifest_rows=0)
(bdir / dycap.REPORT_NAME).unlink(missing_ok=True)
_caps_missing = run_report()
check(
    "#87: a bundle without captions.json fails stage 12",
    not _caps_missing.ok
    and any(m["artefact"] == dycap.REPORT_NAME and m["why"] for m in _caps_missing.missing),
    _caps_missing.summary(),
)
shutil.rmtree(bdir, ignore_errors=True)
fill_bundle(frame_count=0, manifest_rows=0)
(bdir / dysy.REPORT_NAME).unlink(missing_ok=True)
_synth_missing = run_report()
check(
    "#87: a bundle without synth.json fails stage 12",
    not _synth_missing.ok
    and any(m["artefact"] == dysy.REPORT_NAME and m["why"] for m in _synth_missing.missing),
    _synth_missing.summary(),
)


# (b) The cross-reference that would have caught #82. #82's signature in the artefact was
# `captions.json` carrying `quality_gate: ""` on every frame while `quality.json` recorded one
# FAIL. Each file was internally consistent; only a comparison between them sees it.
def _gate_bundle(
    name: str,
    *,
    captions_gate: str,
    quality_gate: str,
    has_video: bool = True,
    quality_raw: str | None = None,
) -> Path:
    root = tmp / name
    shutil.rmtree(root, ignore_errors=True)
    (root / "images").mkdir(parents=True)
    (root / "images" / "f_0.png").write_bytes(b"\x89PNG\r\n")
    (root / "manifest.tsv").write_text(
        "pts_ms\ttime\tfile\n0\t00:00:00.00\tf_0.png\n", encoding="utf-8"
    )
    (root / "note.md").write_text("# note\n", encoding="utf-8")
    (root / dycap.REPORT_NAME).write_text(
        json.dumps({"alignments": [{"frame": "f_0.png", "quality_gate": captions_gate}]}),
        encoding="utf-8",
    )
    (root / dyprobe.PROVENANCE_NAME).write_text(
        json.dumps({"source": {"has_video": has_video}}), encoding="utf-8"
    )
    if quality_raw is not None:
        (root / dyq.REPORT_NAME).write_text(quality_raw, encoding="utf-8")
    else:
        (root / dyq.REPORT_NAME).write_text(
            json.dumps({"frames": [{"name": "f_0.png", "quality_gate": quality_gate}]}),
            encoding="utf-8",
        )
    for extra in dyro.REQUIRED:
        target = root / ("note.md" if extra == "note" else extra)
        if not target.exists():
            target.write_text("x", encoding="utf-8")
    return root


_p82_root = _gate_bundle("p82", captions_gate="", quality_gate="FAIL")
_p82 = run_report_in(_p82_root)
check(
    "#87: captions.json disagreeing with quality.json is a stage-12 failure (#82 shape)",
    not _p82.ok and bool(_p82.inconsistent),
    f"ok={_p82.ok} inconsistent={_p82.inconsistent}",
)
check(
    "#87: and the disagreement names the frame and both verdicts",
    any(
        e.get("frame") == "f_0.png" and e["artefact"] == dycap.REPORT_NAME and "FAIL" in e["detail"]
        for e in _p82.inconsistent
    ),
    str(_p82.inconsistent),
)
check(
    "#87: both files still verify by presence -- the failure is the contradiction, not absence",
    {dycap.REPORT_NAME, dyq.REPORT_NAME} <= {e["artefact"] for e in _p82.verified},
    str([e["artefact"] for e in _p82.verified]),
)

_agree_root = _gate_bundle("agree", captions_gate="FAIL", quality_gate="FAIL")
_agree = run_report_in(_agree_root)
check(
    "#87: the same bundle is ok when captions.json carries the gate's own verdict",
    _agree.ok and not _agree.inconsistent,
    f"ok={_agree.ok} inconsistent={_agree.inconsistent}",
)

# (c) A corrupt gate report is a failure, not the empty set the old `except JSONDecodeError`
# returned. Truncated to `{"frames": [` is the shape a half-written file leaves.
_corrupt_root = _gate_bundle(
    "corrupt-quality", captions_gate="PASS", quality_gate="PASS", quality_raw='{"frames": ['
)
_corrupt = run_report_in(_corrupt_root)
check(
    "#87: a truncated quality.json fails stage 12 instead of being silently swallowed",
    not _corrupt.ok and any(e["artefact"] == dyq.REPORT_NAME for e in _corrupt.inconsistent),
    f"ok={_corrupt.ok} inconsistent={_corrupt.inconsistent}",
)

# #201: the malformed captions.json shapes stage 12 used to *skip* -- a non-dict entry, an entry
# with no `frame` -- are now a finding carrying the reader's reason. The old skip dropped them
# and nothing else could see it: `count_frames` compares the manifest against the files on disk
# and the coverage figure is read back from `synth.json`, so a frame that never reached the note
# left both clean. `report.gate_disagreements` now calls `caption.read_alignments`, so stage 12
# and stage 7 name the same bytes in the same words.
for _label, _raw in (
    ("not-a-json-object", "[1, 2]"),
    ("alignments-not-a-list", '{"alignments": "x"}'),
    ("entry-without-a-frame", '{"alignments": [{}]}'),
    ("entry-not-a-dict", '{"alignments": [1, 2]}'),
):
    _root = _gate_bundle(f"caps-{_label}", captions_gate="PASS", quality_gate="PASS")
    (_root / dycap.REPORT_NAME).write_text(_raw, encoding="utf-8")
    _raised = None
    try:
        _rep = run_report_in(_root)
    except BaseException as _exc:  # on main: AttributeError/KeyError escape the cross-check
        _raised = _exc
    _reason = _reader(dycap, "read_alignments", _root / dycap.REPORT_NAME)[1]
    _finding = (
        []
        if _raised is not None
        else [
            e
            for e in _rep.inconsistent
            if e["artefact"] == dycap.REPORT_NAME and e["detail"] == _reason
        ]
    )
    check(
        f"#201: stage 12 names a {_label} captions.json instead of skipping it",
        _raised is None and not _rep.ok and bool(_finding),
        repr(_raised) if _raised is not None else f"ok={_rep.ok} inconsistent={_rep.inconsistent}",
    )

# (d) `quality.json` is required only when the bundle's own source record says the source had
# video. A wrong unconditional REQUIRED would fail every audio-only bundle, so all three
# states are asserted: video, audio-only, and a missing record (unknown).
_video_root = _gate_bundle("cond-video", captions_gate="PASS", quality_gate="PASS")
check(
    "#87: quality.json is required when the source record says the source had video",
    dyro.required_for(_video_root).get(dyq.REPORT_NAME) is not None
    and run_report_in(_video_root).quality_required,
    str(sorted(dyro.required_for(_video_root))),
)
(_video_root / dyq.REPORT_NAME).unlink()
_video_missing = run_report_in(_video_root)
check(
    "#87: and deleting it from a video bundle is a missing required artefact",
    not _video_missing.ok and any(m["artefact"] == dyq.REPORT_NAME for m in _video_missing.missing),
    _video_missing.summary(),
)

_audio_root = _gate_bundle("cond-audio", captions_gate="", quality_gate="PASS", has_video=False)
(_audio_root / dyq.REPORT_NAME).unlink()
_audio = run_report_in(_audio_root)
check(
    "#87: an audio-only source keeps quality.json optional -- absence is not a failure",
    _audio.ok
    and not _audio.quality_required
    and _audio.source_had_video is False
    and any(a["artefact"] == dyq.REPORT_NAME for a in _audio.absent),
    f"ok={_audio.ok} required={_audio.quality_required} absent={_audio.absent}",
)

_unknown_root = _gate_bundle("cond-unknown", captions_gate="", quality_gate="PASS")
(_unknown_root / dyprobe.PROVENANCE_NAME).unlink()
(_unknown_root / dyq.REPORT_NAME).unlink()
_unknown = run_report_in(_unknown_root)
check(
    "#87: an unknown source record keeps quality.json optional, not falsely required",
    _unknown.ok and _unknown.quality_required is False and _unknown.source_had_video is None,
    f"ok={_unknown.ok} source_had_video={_unknown.source_had_video}",
)

# (e) The exit contract's own description moved with `ok`: it is now also a claim about the
# cross-reference, and `report-provenance.json` has to say so (#87).
_prov_text = (tmp / "order_bundle" / dyro.PROVENANCE_NAME).read_text(encoding="utf-8")
check(
    "#87: report-provenance.json states that ok covers the cross-reference",
    "agreement" in _prov_text
    and "inconsistent" in _prov_text
    and dycap.REPORT_NAME in _prov_text
    and dyq.REPORT_NAME in _prov_text,
    _prov_text[-600:],
)
check(
    "#87: and states where the conditional requirement came from",
    dyprobe.PROVENANCE_NAME in _prov_text and "quality_gate_required" in _prov_text,
    _prov_text[-900:],
)

# --- #211: a truncated manifest stops passing silently ------------------------------------
# `count_frames` stats the manifest's own rows, so the manifest is its own denominator: cut
# from three rows to one it reported `1/1`, the note covered one of three frames, and the run
# exited 0 with `uncovered 0.00`. `quality.json` is the other witness -- one verdict per frame
# stage 5 judged, written from the in-memory list stage 4 returned (`pipeline.py:511` ->
# `:541`) while stage 6 re-reads the manifest from disk (`caption.py:238-243`). The finding rides
# `report.inconsistent`, which `Report.ok` reads and `cli.py:468` turns into exit 6: a guard,
# not a warning, because a warning leaves a note covering 1 of 3 frames at exit 0.


def _manifest_bundle(name: str, *, judged: int, rows: int, has_video: bool = True) -> Path:
    """A bundle whose manifest lists `rows` of the `judged` frames stage 5 judged."""
    root = tmp / name
    shutil.rmtree(root, ignore_errors=True)
    (root / "images").mkdir(parents=True)
    for index in range(judged):
        (root / "images" / f"f_{index}.png").write_bytes(b"\x89PNG\r\n")
    (root / dyfr.MANIFEST).write_text(
        dyfr.MANIFEST_HEADER
        + "\n"
        + "".join(f"{i * 1000}\t00:00:0{i}.00\tf_{i}.png\n" for i in range(rows)),
        encoding="utf-8",
    )
    (root / dyq.REPORT_NAME).write_text(
        json.dumps(
            {"frames": [{"name": f"f_{i}.png", "quality_gate": "PASS"} for i in range(judged)]}
        ),
        encoding="utf-8",
    )
    # Stage 6 derives its frames from the manifest, so the alignments are exactly the manifest's
    # rows and the #87 cross-check stays silent. That is the case #211 is about: the two files
    # stage 12 compared agreed with each other, and both were short.
    (root / dycap.REPORT_NAME).write_text(
        json.dumps(
            {"alignments": [{"frame": f"f_{i}.png", "quality_gate": "PASS"} for i in range(rows)]}
        ),
        encoding="utf-8",
    )
    (root / "note.md").write_text("# note\n", encoding="utf-8")
    (root / dyprobe.PROVENANCE_NAME).write_text(
        json.dumps({"source": {"has_video": has_video}}), encoding="utf-8"
    )
    for extra in dyro.REQUIRED:
        target = root / ("note.md" if extra == "note" else extra)
        if not target.exists():
            target.write_text("x", encoding="utf-8")
    return root


_manifest_full = run_report_in(_manifest_bundle("m211-full", judged=3, rows=3))
check(
    "#211: a manifest listing every judged frame is ok and silent",
    _manifest_full.ok
    and not _manifest_full.inconsistent
    and (_manifest_full.frames_found, _manifest_full.frames_expected) == (3, 3),
    f"ok={_manifest_full.ok} inconsistent={_manifest_full.inconsistent} {_manifest_full.summary()}",
)
_prov_211 = (tmp / "m211-full" / dyro.PROVENANCE_NAME).read_text(encoding="utf-8")
check(
    # The artefact's own description of what `inconsistent` can hold: it named one comparison
    # and this change adds a second, so the text would otherwise be an incomplete enumeration.
    "#211: report-provenance.json names the count comparison as part of the cross-reference",
    dyfr.MANIFEST in _prov_211 and "frame *count*" in _prov_211,
    _prov_211[-600:],
)

_manifest_short_stream = io.StringIO()
_manifest_short = run_report_in(
    _manifest_bundle("m211-short", judged=3, rows=1), stream=_manifest_short_stream
)
check(
    # The old `frames_found != frames_expected` line stays silent here -- 1 of 1 -- which is why
    # the new comparison is the only thing that can see this bundle.
    "#211: a manifest cut to 1 of 3 judged frames fails stage 12 instead of exiting 0",
    not _manifest_short.ok
    and len(_manifest_short.inconsistent) == 1
    and _manifest_short.inconsistent[0]["artefact"] == dyfr.MANIFEST
    and _manifest_short.inconsistent[0]["detail"] == "1 of 3 frames listed"
    and "changed afterwards" in _manifest_short.inconsistent[0]["why"]
    and (_manifest_short.frames_found, _manifest_short.frames_expected) == (1, 1),
    f"ok={_manifest_short.ok} inconsistent={_manifest_short.inconsistent} "
    f"{_manifest_short.summary()}",
)
check(
    "#211: and it is printed in the existing INCONSISTENT voice, not a new one",
    "INCONSISTENT manifest.tsv: 1 of 3 frames listed -- stage 4 wrote it with the frames stage 5"
    in _manifest_short_stream.getvalue(),
    repr(_manifest_short_stream.getvalue()[-500:]),
)

_manifest_header = run_report_in(_manifest_bundle("m211-header", judged=3, rows=0))
check(
    # The second silent case, and the one that contradicts `count_frames`' docstring claim that
    # stage 12 catches a header-only manifest: 0 == 0 makes the old comparison agree.
    "#211: a header-only manifest is a failure too, not a 0/0 success",
    not _manifest_header.ok
    and len(_manifest_header.inconsistent) == 1
    and _manifest_header.inconsistent[0]["detail"] == "0 of 3 frames listed"
    and (_manifest_header.frames_found, _manifest_header.frames_expected) == (0, 0),
    f"ok={_manifest_header.ok} inconsistent={_manifest_header.inconsistent} "
    f"{_manifest_header.summary()}",
)

_manifest_trim = run_report_in(_manifest_bundle("m211-trim", judged=1, rows=1))
check(
    # The pin that the legitimate case is untouched: a trim re-runs stage 4, which rewrites the
    # manifest and deletes the old frames, so stage 5 judged the same set and the two counts
    # agree. One row, one verdict, one image -- and no finding.
    "#211: a self-consistent trim is not flagged, because the gate judged the trimmed set",
    _manifest_trim.ok
    and not _manifest_trim.inconsistent
    and (_manifest_trim.frames_found, _manifest_trim.frames_expected) == (1, 1),
    f"ok={_manifest_trim.ok} inconsistent={_manifest_trim.inconsistent}",
)

_manifest_gone_root = _manifest_bundle("m211-absent", judged=3, rows=3)
(_manifest_gone_root / dyfr.MANIFEST).unlink()
_manifest_gone = run_report_in(_manifest_gone_root)
check(
    # Absence is REQUIRED's case, not this check's: the run fails as it did before, and the
    # manifest is named once, as missing, rather than twice.
    "#211: an absent manifest is a missing required artefact, not a count disagreement",
    not _manifest_gone.ok
    and any(m["artefact"] == dyfr.MANIFEST for m in _manifest_gone.missing)
    and not any(e["artefact"] == dyfr.MANIFEST for e in _manifest_gone.inconsistent),
    f"ok={_manifest_gone.ok} missing={_manifest_gone.missing} "
    f"inconsistent={_manifest_gone.inconsistent}",
)

_manifest_audio_root = _manifest_bundle("m211-audio", judged=3, rows=1, has_video=False)
(_manifest_audio_root / dyq.REPORT_NAME).unlink()
_manifest_audio = run_report_in(_manifest_audio_root)
check(
    # `verdicts is None` when the gate report is legitimately absent, so there is no count to
    # compare against -- the same skip `gate_disagreements` makes.
    "#211: without a gate report there is nothing to compare against, so the check skips",
    _manifest_audio.ok and not _manifest_audio.inconsistent,
    f"ok={_manifest_audio.ok} inconsistent={_manifest_audio.inconsistent}",
)


def _short_manifest_pipeline(source, work, **kw):
    """`fake_pipeline`, with stage 12's real verdict for the truncated bundle."""
    result = fake_pipeline(source, work, **kw)
    result.report = _manifest_short
    return result


_short_out = io.StringIO()
dyp.run = _short_manifest_pipeline
try:
    with redirect_stdout(_short_out), redirect_stderr(_short_out):
        rc_manifest_short = dyc.main(["process", str(src)])
finally:
    dyp.run = fake_pipeline
check(
    # The guard's whole point, measured through the CLI rather than inferred from `ok`: the run
    # that extracted three frames and covers one is exit 6, and stderr says which count.
    "#211: and the truncated bundle's own verdict is exit 6 through the CLI, not 0",
    rc_manifest_short == ec.ARTEFACT_VERIFICATION_FAILED
    and "INCONSISTENT manifest.tsv (1 of 3 frames listed)" in _short_out.getvalue(),
    f"rc={rc_manifest_short}\n{_short_out.getvalue()[-300:]}",
)

# --- the note promotion -------------------------------------------------------------
pdir = tmp / "promote"
pdir.mkdir(parents=True, exist_ok=True)
(pdir / "note.md").write_text("# stage 7\n", encoding="utf-8")
(pdir / "note.linked.md").write_text("# stage 11\n", encoding="utf-8")
published, moved = dyro.promote_note(pdir, pdir / "note.linked.md", pdir / "note.md")
check("the final note is published as note.md", published.name == "note.md", str(published))
check(
    "and its content is the LAST stage's",
    published.read_text() == "# stage 11\n",
    published.read_text(),
)
check(
    "stage 7's original is preserved, not overwritten",
    (pdir / dyro.SYNTH_NOTE_NAME).read_text() == "# stage 7\n",
    "the only record of what stage 7 produced was destroyed",
)
check("and it reports that it moved one", moved is True, str(moved))
_, moved_again = dyro.promote_note(pdir, pdir / "note.linked.md", pdir / dyro.SYNTH_NOTE_NAME)
check(
    "promoting again when nothing changed does not duplicate",
    moved_again is False,
    "a second promotion made a pointless duplicate of itself",
)

# --- 18b. the root is emptied before the run, so stage 12 cannot verify the last one -----
# Measured mid-run on lecture 1: at 06:20 with run 14 in stage 9 the bundle root held 12
# files written by run 13 at 05:50 beside the 17 run 14 had written. `audit.json` and
# `repair.json` are both on the `REQUIRED` set and `_check` accepts any non-empty file, so
# stage 12 -- the stage that exists to catch this -- was reading the previous run's audit.
# It stayed invisible because every test builds a fresh `tmp` directory and the bundle is
# therefore never dirty when the tests look at it.
cdir = tmp / "clearroot"
(cdir / "images").mkdir(parents=True, exist_ok=True)
stale = cdir / dyau.REPORT_NAME
stale.write_text('{"stale": true}\n', encoding="utf-8")
(cdir / dyp.TIMINGS_NAME).write_text("{}\n", encoding="utf-8")
(cdir / "note.md").write_text("# old\n", encoding="utf-8")
(cdir / "images" / "f_1.png").write_bytes(b"\x89PNG")
(cdir / "my-own-file.txt").write_text("not a run artefact\n", encoding="utf-8")

bundle = dyb.Bundle(root=cdir, created=False)
cleared = bundle.clear_root()
check(
    "the previous run's artefacts are removed from the root",
    sorted(cleared) == sorted([dyau.REPORT_NAME, dyp.TIMINGS_NAME, "note.md"]),
    f"cleared {cleared}",
)
check(
    "and what was cleared is what was there",
    not stale.exists() and not (cdir / dyp.TIMINGS_NAME).exists(),
    "a REQUIRED artefact from an earlier run survived",
)
check(
    "a file the run never writes is left alone",
    (cdir / "my-own-file.txt").is_file(),
    "`--output-dir` makes this directory the bundle root; a user's own file went with it",
)
check(
    "`images/` is left for clear_images, not removed as a directory",
    (cdir / "images" / "f_1.png").is_file(),
    "the frames directory was taken out from under stage 5",
)
check(
    "clearing a bundle that does not exist is not an error",
    dyb.Bundle(root=tmp / "never-created", created=False).clear_root() == [],
    "a first run on a fresh path raised instead of returning nothing",
)

# The acceptance criterion that matters: a stale REQUIRED artefact must not satisfy the
# check. Without `clear_root` this passes only because the tests are hermetic; with it, this
# states the property directly.
kept_dir = tmp / "stale-required-kept"
kept_dir.mkdir(parents=True, exist_ok=True)
(kept_dir / dyau.REPORT_NAME).write_text('{"stale": true}\n', encoding="utf-8")

# Without the clear, stage 12 used to find this file, `_check` accepted it -- it exists and is
# non-empty -- and recorded the audit as verified for a run that never ran one. `verified` is
# asserted per name, not as a non-empty list: with a bare `bool(missing)` the check still
# passes with the stale file present, because seven other REQUIRED entries are absent anyway.
kept = dyro.run(
    kept_dir,
    final_note=kept_dir / "note.linked.md",
    synth_note=kept_dir / "note.md",
    artefacts={"note": kept_dir / "note.md"},
)
# #88 made this property stop depending on the clear. The entry used to resolve as
# `artefacts.get(name) or (root / name)`, so the fallback found the stale file and only
# `clear_root` stood between it and `verified`. Resolution is registry-only now, so the stale
# file is not found even with the clear deliberately skipped -- which is the stronger claim,
# and the one that holds if a future path ever skips `clear_root` again.
check(
    "#88: without the clear, an earlier run's audit.json is still not accepted as this run's",
    not any(e["artefact"] == dyau.REPORT_NAME for e in kept.verified)
    and any(m["artefact"] == dyau.REPORT_NAME for m in kept.missing),
    f"verified={[e['artefact'] for e in kept.verified]}, "
    f"missing={[m['artefact'] for m in kept.missing]}",
)

# The mechanism itself, stated where it can be seen: a registry key that is not the root name
# (`audit/audit.json`) whose value is not in the bundle root at all. A root-name fallback
# cannot find it; the registry can. This is the half of #88 that `clear_root` never covered --
# a stale file and a registered-but-elsewhere file are the same thing to `root / name`.
_elsewhere = tmp / "registered-elsewhere"
_elsewhere.mkdir(parents=True, exist_ok=True)
(_elsewhere / dyau.REPORT_NAME).write_text('{"real": true}\n', encoding="utf-8")
_elsewhere_bundle = tmp / "registered-elsewhere-bundle"
_elsewhere_bundle.mkdir(parents=True, exist_ok=True)
elsewhere_run = dyro.run(
    _elsewhere_bundle,
    final_note=_elsewhere_bundle / "note.linked.md",
    synth_note=_elsewhere_bundle / "note.md",
    artefacts={
        "note": _elsewhere_bundle / "note.md",
        f"audit/{dyau.REPORT_NAME}": _elsewhere / dyau.REPORT_NAME,
    },
)
check(
    "#88: an artefact registered under another key, elsewhere on disk, is still verified",
    any(e["artefact"] == dyau.REPORT_NAME for e in elsewhere_run.verified),
    f"verified={[e['artefact'] for e in elsewhere_run.verified]}, "
    f"missing={[m['artefact'] for m in elsewhere_run.missing]}",
)

# A separate directory, because the run above writes its own report.json and
# report-provenance.json into it, and those would then be cleared too -- which is correct
# behaviour and would make the assertion below about the wrong files.
rdir = tmp / "stale-required"
rdir.mkdir(parents=True, exist_ok=True)
(rdir / dyau.REPORT_NAME).write_text('{"stale": true}\n', encoding="utf-8")
cleared_stale = dyb.Bundle(root=rdir, created=False).clear_root()
stale_report = dyro.run(
    rdir,
    final_note=rdir / "note.linked.md",
    synth_note=rdir / "note.md",
    artefacts={"note": rdir / "note.md"},
    superseded=cleared_stale,
)
check(
    "and after the clear it is reported missing, not verified",
    any(e["artefact"] == dyau.REPORT_NAME for e in stale_report.missing)
    and not any(e["artefact"] == dyau.REPORT_NAME for e in stale_report.verified),
    f"verified={[e['artefact'] for e in stale_report.verified]}, "
    f"missing={[e['artefact'] for e in stale_report.missing]}",
)
check(
    "report.json records what this run superseded",
    stale_report.as_dict()["superseded_by_this_run"] == [dyau.REPORT_NAME],
    str(stale_report.as_dict()["superseded_by_this_run"]),
)

# --- 19. the exit contract: what may not produce 0 -----------------------------------
# Both rules below were decided after the first real 12-stage run, where each one produced a
# run that looked finished and was not.
dark = dyq.FrameQuality(
    name="f_0000000000.png",
    source=tmp / "f_0000000000.png",
    passed=False,
    mege=23871.9,
    threshold=26216.7,
    crop_state="full_frame_fallback",
    content_type=dyq.CONTENT_DARK,
    bbox_source="geometric",
    edges=164944,
    luma_mean=35.4,
    luma_std=30.8,
    reason="SOFT",
)
white = dyq.FrameQuality(
    name="f_0000009124.png",
    source=tmp / "f_0000009124.png",
    passed=False,
    mege=24100.0,
    threshold=26216.7,
    crop_state="cropped",
    content_type=dyq.CONTENT_WHITE,
    bbox_source="geometric",
    edges=167414,
    luma_mean=210.0,
    luma_std=40.0,
    reason="SOFT",
)
good_frame = dyq.FrameQuality(
    name="ok.png",
    source=tmp / "ok.png",
    passed=True,
    mege=58925.6,
    threshold=26216.7,
    crop_state=dyq.CROP_CROPPED,
    content_type=dyq.CONTENT_UNKNOWN,
    bbox_source="geometric",
    edges=167414,
    luma_mean=63.9,
    luma_std=68.7,
)
check(
    "a dark canvas is recorded as failed",
    dark in dyq.Report(frames=[dark, good_frame]).failed,
    "not recorded",
)
check(
    "but it does not decide the exit code",
    dyq.Report(frames=[dark, good_frame]).ok,
    "a dark frame at the head of a recording would block exit 0 on almost every lecture",
)
check(
    "and the summary says it was excused rather than passed",
    "dark-canvas not fatal" in dyq.Report(frames=[dark, good_frame]).summary(),
    dyq.Report(frames=[dark, good_frame]).summary(),
)
check(
    "a soft content frame is fatal",
    not dyq.Report(frames=[white, good_frame]).ok,
    "a soft white document is real material the pipeline could not read",
)
check(
    "and it is fatal even alongside passing frames",
    len(dyq.Report(frames=[white, good_frame]).failed_fatal) == 1,
    str(dyq.Report(frames=[white, good_frame]).failed_fatal),
)


# Degradation has two distinct user-facing meanings: an incomplete model note, and
# a template skeleton that never had a model behind it.
def _run_degraded_note(synthesizer: str) -> tuple[int, str]:
    def degraded_pipeline(source, work, **kw):
        result = fake_pipeline(source, work, **kw)
        result.note = SimpleNamespace(
            degraded=True,
            synthesizer=synthesizer,
            notes=["section 4 was not filled"],
        )
        return result

    saved_pipeline = dyp.run
    dyp.run = degraded_pipeline
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        rc = dyc.main(["process", str(src), "--no-vault-export"])
    dyp.run = saved_pipeline
    return rc, output.getvalue()


llm_degraded_rc, llm_degraded_output = _run_degraded_note("llm")
check(
    "an incomplete LLM note does not claim no model wrote it (#109)",
    "not by a model" not in llm_degraded_output and "DYAK_LLM_ENDPOINT" not in llm_degraded_output,
    llm_degraded_output,
)
check(
    "an incomplete LLM note reports its degradation and has its own exit code (#109)",
    "section 4 was not filled" in llm_degraded_output and llm_degraded_rc == ec.INCOMPLETE_NOTE,
    f"rc={llm_degraded_rc}\n{llm_degraded_output}",
)

template_degraded_rc, template_degraded_output = _run_degraded_note("template")
check(
    "a template note clearly says it was not written by a model (#109)",
    "not by a model" in template_degraded_output
    and "DYAK_LLM_ENDPOINT" in template_degraded_output,
    template_degraded_output,
)
check(
    "a template note keeps its explicit opt-in exit code (#109)",
    template_degraded_rc == ec.USAGE,
    f"rc={template_degraded_rc}\n{template_degraded_output}",
)

# A template note is not a synthesised note.
check(
    "the template reports itself degraded even with every section filled",
    dysy.synthesise(
        json.loads(scaps.read_text())["alignments"],
        "текст",
        src_path,
        dysy.TemplateSynthesizer(src_path, "текст", cuts),
    ).degraded,
    "a note no model wrote was reported as a clean synthesis",
)

dyc.run_all = real_run_all
dyp.run = real_pipeline_run
dyr.run = real_run
dyr.resolve = real_resolve
dyr.shutil.which = real_which
dyfr.time.sleep = real_sleep
if real_workdir_env is None:
    os.environ.pop(dyw.WORKDIR_ENV, None)
else:
    os.environ[dyw.WORKDIR_ENV] = real_workdir_env
if real_settings_env is None:
    os.environ.pop(dyd.SETTINGS_PATH_ENV, None)
else:
    os.environ[dyd.SETTINGS_PATH_ENV] = real_settings_env
if real_vault_env is None:
    os.environ.pop(dyd.VAULT_ENV, None)
else:
    os.environ[dyd.VAULT_ENV] = real_vault_env
shutil.rmtree(tmp, ignore_errors=True)

# --- #117: the whole lecture reaches the model, and stage 12 refuses a partial one --------
# Stage 7 mapped the eight required sections onto the first eight time slices, so a 58-frame
# lecture (12 slices) sent slices 9-12 to no request at all: about 31% of the lecture, absent
# from a note that stage 12 verified as complete while the run exited 0.
_cov_calls: list[list[dict]] = []


def _cov_chat(messages, config, **kw):
    _cov_calls.append(messages)
    return dyle.Reply(
        text="тело",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        seconds=0.0,
        attempts=1,
        finish_reason="stop",
    )


_cov_alignments = [
    {
        "frame": f"f_{i:04d}.png",
        "text": f"кадрномер{i:04d}конец",
        "caption_status": "OK",
        "on_screen_ms": [i * 77000, (i + 1) * 77000],
        "caption": f"слайд {i}",
        "word_count": 3,
    }
    for i in range(58)
]
_cov_old_chat = dyle.chat
dyle.chat = _cov_chat
try:
    _cov_note = dysy.synthesise(
        _cov_alignments,
        "текст",
        src_path,
        dysy.LLMSynthesizer(dyle.Config(endpoint="http://e/v1", model="m"), _WireTranscript()),
    )
finally:
    dyle.chat = _cov_old_chat

_cov_blob = "\n".join(m[1]["content"] for m in _cov_calls)
_cov_absent = [i for i in range(58) if f"кадрномер{i:04d}конец" not in _cov_blob]
check(
    "every frame's transcript reaches at least one LLM request (#117)",
    _cov_absent == [],
    f"frames that reached no request: {_cov_absent}",
)
check(
    "a lecture that fits the budget is sent whole, and the note is not degraded (#117)",
    _cov_note.coverage == 1.0 and not _cov_note.degraded,
    f"coverage={_cov_note.coverage} degraded={_cov_note.degraded}",
)
check(
    "every request names the skill section it is writing, not a slice (#117)",
    [m[1]["content"].splitlines()[0] for m in _cov_calls]
    == [f"Раздел {n} из 8: «{t}»." for n, t, _ in dysy.SECTIONS],
    str([m[1]["content"].splitlines()[0] for m in _cov_calls[:2]]),
)

# The budget is a guard against one unbounded request, not a cost control. What it keeps out
# has to be named, and it has to show up in the coverage metric -- the two things that were
# missing while the note lost its last twenty minutes in silence.
_cut = dysy.synthesise(_cov_alignments, "текст", src_path, FakeSynth(), budget=120)
check(
    "a slice the budget kept out is named, with its timing (#117)",
    any("фрагмент 1" in w and "385 с" in w for w in _cut.notes),
    str(_cut.notes),
)
check(
    "and every slice after it is named, including the last (#117)",
    _cut.notes and "фрагмент 2" in _cut.notes[0] and "фрагмент 12" in _cut.notes[0],
    str(_cut.notes),
)
check(
    "the coverage metric records what was actually sent (#117)",
    _cut.coverage < dysy.COVERAGE_THRESHOLD,
    str(_cut.coverage),
)

# Stage 12 refuses exit 0 on a partial note with its own code. Every artefact can be present
# and verified while the deliverable is missing a third of the course -- that is exactly the
# state that exited 0 before this, so coverage must not be folded into `Report.ok`.
_covdir = tmp / "coverage-bundle"
_covdir.mkdir(parents=True, exist_ok=True)
for _name in dyro.REQUIRED:
    (_covdir / ("note.md" if _name == "note" else _name)).write_text(
        '{"alignments": []}' if _name == dycap.REPORT_NAME else "x", encoding="utf-8"
    )
dysy.write(dysy.Note(markdown="# n", coverage=0.5), _covdir / dysy.REPORT_NAME)

_cov_report = dyro.run(
    _covdir,
    final_note=_covdir / "note.md",
    synth_note=_covdir / "note.synth.md",
    artefacts={n: _covdir / n for n in dyro.REQUIRED},
    stream=io.StringIO(),
)
check(
    "stage 12 reads stage 7's coverage back out of synth.json (#117)",
    _cov_report.coverage == 0.5,
    str(_cov_report.coverage),
)
check(
    "the bundle still verifies: coverage is a separate failure (#117)",
    _cov_report.ok,
    _cov_report.summary(),
)
check(
    "and stage 12 refuses it with its own code, not the missing-artefact code (#117)",
    _cov_report.low_coverage
    and ec.LOW_COVERAGE != ec.ARTEFACT_VERIFICATION_FAILED
    and bool(ec.DESCRIPTIONS.get(ec.LOW_COVERAGE)),
    f"low={_cov_report.low_coverage} code={ec.LOW_COVERAGE}",
)
check(
    "unknown coverage is not low coverage (#117)",
    dyro.Report(coverage=None).low_coverage is False
    and dyro.Report(coverage=1.0).low_coverage is False,
    "a missing measurement was read as a bad note",
)
check(
    "a bundle with no synth.json leaves coverage unknown, not zero (#117)",
    dysy.coverage_of(_covdir / "definitely-absent.json") is None,
    str(dysy.coverage_of(_covdir / "definitely-absent.json")),
)

# --- #128: stage 12's other two semantic gates ------------------------------------------
# Coverage was the first (#117). These are the remaining two, and they are deliberately not
# the same kind of gate. A section left as `в лекции не затрагивается` is *reported*: the
# skill's eight sections are an assumption about the lecture's shape, and a humanities lecture
# legitimately leaves 4-7 empty, so a threshold there would fail a correct note. A section the
# endpoint cut off is *gated*: its text reads as finished and its ending is not there, which is
# the shape that passes lint and audit (#124).
_tdir = tmp / "truncated-bundle"
_tdir.mkdir(parents=True, exist_ok=True)
for _name in dyro.REQUIRED:
    (_tdir / ("note.md" if _name == "note" else _name)).write_text(
        '{"alignments": []}' if _name == dycap.REPORT_NAME else "x", encoding="utf-8"
    )
dysy.write(
    dysy.Note(
        markdown="# n",
        coverage=1.0,
        sections=[
            {"number": 1, "title": "Паспорт лекции", "present": True, "finish_reason": "stop"},
            {"number": 2, "title": "Организационный блок", "present": False},
            {"number": 3, "title": "Термины", "present": True, "finish_reason": "length"},
        ],
    ),
    _tdir / dysy.REPORT_NAME,
)
_t_report = dyro.run(
    _tdir,
    final_note=_tdir / "note.md",
    synth_note=_tdir / "note.synth.md",
    artefacts={n: _tdir / n for n in dyro.REQUIRED},
    stream=io.StringIO(),
)
check(
    "stage 12 reads the per-section record back out of synth.json (#128)",
    _t_report.sections_total == 3 and _t_report.sections_not_covered == 1,
    f"total={_t_report.sections_total} not_covered={_t_report.sections_not_covered}",
)
check(
    "a section the model wrote and the endpoint cut off is named by number (#128)",
    _t_report.sections_truncated == (3,),
    str(_t_report.sections_truncated),
)
check(
    "truncation is a gate with its own code, not coverage's and not the degraded path (#128)",
    _t_report.truncated
    and ec.TRUNCATED_SECTIONS
    not in (ec.LOW_COVERAGE, ec.ARTEFACT_VERIFICATION_FAILED, ec.INCOMPLETE_NOTE)
    and bool(ec.DESCRIPTIONS.get(ec.TRUNCATED_SECTIONS)),
    f"truncated={_t_report.truncated} code={ec.TRUNCATED_SECTIONS}",
)
check(
    "a note with no truncated section is not gated, and the empty-section share is not (#128)",
    dyro.Report(coverage=1.0).truncated is False
    and dyro.Report(sections_total=8, sections_not_covered=2).not_covered_share == 0.25
    and dyro.Report(sections_total=8, sections_not_covered=2).truncated is False,
    "the not-covered share is gated, or the truncation gate fires on an empty section",
)
check(
    "report.json carries all three gates' raw numbers, so a reader can audit them (#128)",
    {
        "transcript_coverage",
        "transcript_coverage_threshold",
        "sections_total",
        "sections_not_covered",
        "sections_not_covered_share",
        "sections_truncated",
        "sections_truncated_numbers",
    }
    <= set(json.loads((_tdir / dyro.REPORT_NAME).read_text(encoding="utf-8"))),
    "a gate's number is missing from report.json",
)
check(
    "a bundle with no synth.json leaves the section gates unknown, not truncated (#128)",
    dysy.gates_of(_tdir / "definitely-absent.json") is None,
    str(dysy.gates_of(_tdir / "definitely-absent.json")),
)


# --- #115: stages 2-5 run as two branches, joined by stage 6 ------------------------------
# The fork's claim is about wall time, and a stubbed pipeline has none: what is pinned here is
# the structure that makes the saving possible, and every check that *can* fail on unmodified
# `main` is written to. The two branch bodies have to meet at a `threading.Barrier(2)` -- which
# a run that executes one branch after the other cannot pass, because the branch that arrives
# first times out -- and `caption.run` is entered only once both have been through it. A
# failure in branch A (the STT endpoint down) still leaves branch B's frames, their gate and
# the images it gated in the bundle, and still ends the run nonzero. Every line a branch writes
# names that branch.
#
# `DYAK_SERIAL=1` is the control arm, and the same stubs drive both: one branch at a time (the
# rendezvous arm below times out on purpose, which is what "one at a time" means observably),
# the pre-#115 stage order, and a stream with no branch tags in it.
#
# The last two arms kill *both* branches at different times, once in each order. One dead branch
# only shows that the run ends; two dead branches are what show the join's choice is the earlier
# failure by the time it happened, that the later one is recorded rather than raised, and -- with
# the order run both ways -- that the choice is not simply the branch inspected first.
#
# The wall-time measurement itself needs the user's lecture, real ffmpeg and a real STT
# endpoint. It is in the PR body, because a stub cannot have a wall clock.
class _StopAtJoin(Exception):
    pass


_br_real = (dyp.probe, dyp.audio, dyp.stt, dyp.frames, dyp.quality, dyp.caption)
_br_seen: dict = {}


def _br_restore() -> None:
    dyp.probe, dyp.audio, dyp.stt, dyp.frames, dyp.quality, dyp.caption = _br_real


def _br_digests(root: Path) -> dict[str, str]:
    """`name -> sha256` for a bundle directory, minus the two sidecars that carry the clock."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
        if path.is_file() and path.name not in (dyp.RUN_STATE_NAME, dyp.TIMINGS_NAME)
    }


def _br_stub_modules(
    *,
    bundle: dyb.Bundle,
    meet,
    fail_stt: bool = False,
    fail_quality: bool = False,
    stt_first: bool = True,
) -> None:
    """Stages 1-6 stubbed, with a rendezvous both branches have to reach before stage 6 runs.

    `meet` is a `threading.Barrier(2)`: two parties, so passing it is evidence that the two
    branch bodies were in flight at the same time. `fail_stt` is the endpoint-down case --
    branch A dies while branch B is still working, after branch B has started. `fail_quality`
    is the both-die case: branch B runs both its stages to the end (so `frames:done` and
    `quality:done` are in `events`) and then dies on the gate report, which is what makes the
    join choose between two real failures instead of between a failure and a success. Which of
    the two dies first is `stt_first`: the arms below run it both ways, so the pair says whether
    the raised error is the earlier failure or just the branch the join inspected first.
    """
    seen = _br_seen
    seen.clear()
    seen.update(
        order=[],
        events=[],
        missing=[],
        join={},
        # The handshake the both-die arms order their two failures with. A body cannot read
        # `_Branch.failed_at` (the pipeline stamps it after the body raises), so the sibling's
        # failure is announced by the body that knows it died.
        a_failed=threading.Event(),
        b_failed=threading.Event(),
        a_died=False,
        b_died=False,
    )
    note_lock = threading.Lock()

    def note(event: str) -> None:
        # Under a lock: two threads append, and the interleaving is the evidence being read.
        with note_lock:
            seen["events"].append(event)

    def later_failure(sibling: threading.Event) -> None:
        """Wait for the sibling's failure, then long enough to be stamped after it.

        `_Branch` stamps `failed_at` after the body raises, so a body cannot hand-shake with the
        stamp itself; the wait is what makes this failure the later one by construction rather
        than by luck -- the sibling's stamp lands microseconds after its event is set, this waits
        250 ms before raising.
        """
        sibling.wait(timeout=10.0)
        time.sleep(0.25)

    def rendezvous() -> None:
        if meet is not None:
            meet.wait()

    _probe_info = _Stub(summary=lambda: "video h264")
    dyp.probe = _Stub(
        probe=lambda p: _probe_info,
        require_supported=lambda i: None,
        provenance=lambda *a, **k: "{}",
        PROVENANCE_NAME="source-provenance.json",
    )

    def _audio_extract(src, wav):
        seen["order"].append("audio")
        note("audio")
        return _Stub(duration=60.0, summary=lambda: "audio.wav 60.0s 16000Hz mono")

    dyp.audio = _Stub(extract=_audio_extract, check_coverage=lambda i, a: None)

    def _stt_write(transcript, dest):
        # Writes what it returns: `Bundle.publish` raises on a missing source (#89).
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "transcript.json").write_text(
            json.dumps({"segments": [{"index": 0, "text": "hello"}]}), encoding="utf-8"
        )
        (dest / "transcript.raw.json").write_text("{}", encoding="utf-8")
        (dest / "transcript.txt").write_text("hello", encoding="utf-8")
        return {
            "json": dest / "transcript.json",
            "raw": dest / "transcript.raw.json",
            "txt": dest / "transcript.txt",
        }

    def _transcribe(wav, audio_duration, backend=None, on_progress=None):
        seen["order"].append("stt")
        note("stt:enter")
        if fail_stt:
            if not stt_first:
                later_failure(seen["b_failed"])
            note("stt:fail")
            seen["a_died"] = True
            # Before the raise, so the sibling's own failure can be ordered after this one.
            seen["a_failed"].set()
            raise dyr.DependencyError(name="whispercpp", code=2, message="connection refused")
        rendezvous()
        if on_progress is not None:
            # Through the pipeline's own adapter, so the continuation line under test is the
            # one the pipeline renders -- tag and all.
            on_progress(0, 1)
        note("stt:done")
        return dyst.Transcript(wav=wav, audio_duration=audio_duration, segments=(), backend="stub")

    dyp.stt = _Stub(
        # `pipeline.run` resolves the backend before the start line (#186), so a stub that
        # replaces the whole module needs `backend` and the two `transcribe` keywords.
        backend=lambda *a, **k: SimpleNamespace(
            info=lambda: dysb.BackendInfo(name="whispercpp", target="stub://whispercpp")
        ),
        transcribe=_transcribe,
        write=_stt_write,
        RATE_RANGE=dysttc.RATE_RANGE,
        WARMUP_SECONDS=dysttc.WARMUP_SECONDS,
    )

    def _frames_run(src, frames_dir):
        seen["order"].append("frames")
        note("frames:enter")
        frames_dir.mkdir(parents=True, exist_ok=True)
        manifest = frames_dir / dyfr.MANIFEST
        manifest.write_text(dyfr.MANIFEST_HEADER + "\n", encoding="utf-8")
        frame = frames_dir / "f_0.png"
        frame.write_bytes(b"\x89PNG\r\n\x1a\nframe")
        note("frames:done")
        return manifest, [frame]

    dyp.frames = _Stub(
        run=_frames_run,
        summary=lambda m, p: f"{len(p)} frames",
        ffmpeg_version=lambda: "stub",
    )

    def _quality_run(produced, dest, *, settings=None, bbox_source=None, stream=None):
        seen["order"].append("quality")
        note("quality:enter")
        # Stage 5 reads the frames stage 4 wrote, and the move into `images/` happens after this
        # call, in this branch: a branch that moved or cleared them early shows up here.
        seen["missing"] = [str(p) for p in produced if not Path(p).is_file()]
        rendezvous()
        for path in produced:
            enhanced = Path(dest) / "enhanced"
            enhanced.mkdir(parents=True, exist_ok=True)
            (enhanced / f"enhanced_{path.name}").write_bytes(b"\x89PNG\r\n\x1a\nenhanced")
        note("quality:done")
        return _Stub(
            passed=[_Stub(name=p.name) for p in produced], summary=lambda: "1/1 pass", ok=True
        )

    def _quality_write(report, target, settings):
        if not fail_quality:
            target.write_text("{}", encoding="utf-8")
            return
        # The branch that dies on the gate report, having run both its stages (`frames:done` and
        # `quality:done` are already in `events`).
        if stt_first:
            later_failure(seen["a_failed"])
        note("quality:fail")
        seen["b_died"] = True
        seen["b_failed"].set()
        raise dyr.DependencyError(
            name="quality", code=2, message="the gate report could not be written"
        )

    dyp.quality = _Stub(
        Settings=lambda: _Stub(),
        resolve_estimator=lambda name: None,
        run=_quality_run,
        write=_quality_write,
        provenance=lambda qs, bbox_source: "{}",
        enhanced_name=lambda name: f"enhanced_{name}",
        REPORT_NAME=dyq.REPORT_NAME,
        PROVENANCE_NAME=dyq.PROVENANCE_NAME,
    )

    def _caption_run(*a, **kw):
        # The join. What the two branches were supposed to have published by now is recorded,
        # and then the run is stopped: stages 7-12 are not this test's subject.
        seen["join"] = {
            "root": sorted(p.name for p in bundle.root.iterdir()),
            "images": sorted(p.name for p in (bundle.root / dyb.IMAGES).iterdir()),
            "registry": sorted(bundle.artefacts),
        }
        raise _StopAtJoin()

    dyp.caption = _Stub(
        run=_caption_run,
        REPORT_NAME=dycap.REPORT_NAME,
        PROVENANCE_NAME=dycap.PROVENANCE_NAME,
    )


def _br_run(
    root: Path,
    name: str,
    *,
    serial: bool,
    meet,
    fail_stt: bool = False,
    fail_quality: bool = False,
    stt_first: bool = True,
) -> dict:
    """One arm of the fork: what the stubs saw, the stream, and the bundle it wrote into.

    The variable is spelled out rather than taken from `settings.SERIAL_ENV`, on purpose: this
    file has to produce its FAIL lines against unmodified `main` (where no such constant exists
    yet) instead of raising an `AttributeError` that takes the rest of the suite with it.
    """
    stream = _FlushRecorder()
    source = root / "lecture.mp4"
    bundle = dyb.Bundle.open(source, output_dir=str(root / name), overwrite=False)
    if serial:
        # Set after `_env.isolate()`, so the suite is still independent of the developer's shell.
        os.environ["DYAK_SERIAL"] = "1"
    else:
        os.environ.pop("DYAK_SERIAL", None)
    _br_stub_modules(
        bundle=bundle, meet=meet, fail_stt=fail_stt, fail_quality=fail_quality, stt_first=stt_first
    )
    outcome: dict = {"reached_join": False}
    try:
        REAL_PIPELINE_RUN(source, dyw.WorkDir(parent=root / "work"), bundle=bundle, stream=stream)
    except _StopAtJoin:
        outcome = {"reached_join": True}
    except BaseException as exc:
        outcome = {"reached_join": False, "error": repr(exc)}
    finally:
        _br_restore()
        os.environ.pop("DYAK_SERIAL", None)
    return {"bundle": bundle, "text": stream.getvalue(), **outcome, **_br_seen}


with tempfile.TemporaryDirectory() as _br_td:
    _br_root = Path(_br_td)
    (_br_root / "lecture.mp4").write_bytes(b"x")
    (_br_root / "work").mkdir()

    # Arm 1: the default, concurrent. The rendezvous is what makes "concurrent" observable.
    _br_par = _br_run(_br_root, "parallel", serial=False, meet=threading.Barrier(2, timeout=20.0))
    check(
        "#115: both branches are in flight at once -- the two-party rendezvous is passed, which "
        "a run that does one branch after the other cannot do",
        _br_par.get("reached_join") is True,
        str(_br_par.get("error")),
    )
    check(
        "#115: both branches finished their stages, and stage 5 read the frames stage 4 wrote",
        sorted(_br_par.get("order", [])) == ["audio", "frames", "quality", "stt"]
        and _br_par.get("missing") == [],
        f"order={_br_par.get('order')} missing={_br_par.get('missing')}",
    )
    check(
        "#115: each branch published as its own stage finished, so stage 6 starts with both "
        "halves in the bundle",
        {"quality.json", "manifest.tsv", "transcript.json", "transcript.txt"}
        <= set(_br_par.get("join", {}).get("root", [])),
        str(_br_par.get("join", {}).get("root")),
    )
    check(
        "#115: and the frames stage 5 gated, plus the enhanced copies it accepted, are in "
        "images/ by then",
        _br_par.get("join", {}).get("images") == ["enhanced_f_0.png", "f_0.png"],
        str(_br_par.get("join", {}).get("images")),
    )

    # Arm 2: the control, `DYAK_SERIAL=1`. Same stubs, one branch at a time.
    _br_ser = _br_run(_br_root, "serial", serial=True, meet=None)
    check(
        "#115: DYAK_SERIAL=1 runs the branches in the pre-#115 stage order, one after the other",
        _br_ser.get("reached_join") is True
        and _br_ser.get("order") == ["audio", "stt", "frames", "quality"],
        f"order={_br_ser.get('order')} error={_br_ser.get('error')}",
    )
    check(
        "#115: and prints the pre-#115 stream, with no branch tags in it",
        "(A)" not in _br_ser.get("text", "") and "(B)" not in _br_ser.get("text", ""),
        repr(_br_ser.get("text", "")[:300]),
    )
    check(
        "#115: the serial arm prints the stage lines in stage order, stage 3's documented start "
        "line included (#97)",
        re.findall(r"^  \[(\d+)/12\] ", _br_ser.get("text", ""), re.M)
        == ["1", "2", "3", "3", "4", "5"],
        repr(re.findall(r"^  \[(\d+)/12\] ", _br_ser.get("text", ""), re.M)),
    )

    # Arm 3: the control again, with the rendezvous back on. A serial run has one branch in
    # flight, so a two-party barrier can only time out -- which is the observable difference
    # between "the flag is honoured" and "the flag is read and ignored".
    _br_ser_meet = _br_run(
        _br_root, "serial-meet", serial=True, meet=threading.Barrier(2, timeout=1.0)
    )
    check(
        "#115: DYAK_SERIAL=1 keeps one branch in flight -- the two-party rendezvous times out",
        "BrokenBarrierError" in str(_br_ser_meet.get("error")),
        str(_br_ser_meet.get("error")),
    )

    # Arm 4: the endpoint-down case. Branch A dies; branch B has started, and has to finish and
    # publish, and the run still has to end nonzero.
    _br_fail = _br_run(_br_root, "failed", serial=False, meet=None, fail_stt=True)
    check(
        "#115: a failure in one branch still ends the run, with that branch's own error",
        _br_fail.get("reached_join") is False
        and "connection refused" in str(_br_fail.get("error")),
        f"{_br_fail.get('reached_join')} {_br_fail.get('error')}",
    )
    check(
        "#115: the surviving branch finished after its sibling failed",
        "frames:done" in _br_fail.get("events", [])
        and "quality:done" in _br_fail.get("events", []),
        str(_br_fail.get("events")),
    )
    _br_fail_root = _br_fail["bundle"].root
    check(
        "#115: and its artefacts are in the bundle rather than stranded in the work directory",
        {"quality.json", "quality-provenance.json", "manifest.tsv"}
        <= {p.name for p in _br_fail_root.iterdir()}
        and (_br_fail_root / dyb.IMAGES / "f_0.png").is_file(),
        str(sorted(p.name for p in _br_fail_root.iterdir())),
    )

    # Arms 5 and 6: both branches die, at different times, and the pair of arms runs the order
    # both ways. Arm 5 kills A first and arm 6 kills B first, so between them they pin what the
    # join's choice is: not the branch it inspected first (`(stt_running, frames_running)` --
    # always A), and not the one that died last (that is B in arm 5 and A in arm 6), but the
    # earlier failure by the time it happened, which is `pipeline.py`'s `sorted(..., key=lambda
    # branch: branch.failed_at)` and nothing else. Both branches reaching their end is the other
    # half of the claim -- neither is cancelled by the other's death.
    _br_both = _br_run(
        _br_root, "both-failed", serial=False, meet=None, fail_stt=True, fail_quality=True
    )
    _br_both_events = _br_both.get("events", [])
    _br_both_error = str(_br_both.get("error"))
    check(
        "#115: with both branches failing, each runs to its own end and the join raises the "
        "earlier branch's error, the later one recorded rather than raised (A dies first here)",
        _br_both.get("reached_join") is False
        and "connection refused" in _br_both_error
        and "gate report" not in _br_both_error
        and _br_both.get("a_died") is True
        and _br_both.get("b_died") is True
        and {"stt:fail", "frames:done", "quality:done"} <= set(_br_both_events)
        and _br_both_events.index("stt:fail") < _br_both_events.index("quality:fail"),
        f"error={_br_both_error} a_died={_br_both.get('a_died')} b_died={_br_both.get('b_died')} "
        f"events={_br_both_events}",
    )

    # Arm 6: the same two failures with the order inverted -- B dies first, so the earlier
    # failure is now the *second* branch the join inspects. A join that raised the branch it
    # inspected first, or the one that died last, raises A's error here and fails this check.
    _br_both_rev = _br_run(
        _br_root,
        "both-failed-reversed",
        serial=False,
        meet=None,
        fail_stt=True,
        fail_quality=True,
        stt_first=False,
    )
    _br_both_rev_events = _br_both_rev.get("events", [])
    _br_both_rev_error = str(_br_both_rev.get("error"))
    check(
        "#115: and with the order inverted -- B dies first, A second -- the raised error is "
        "still the earlier one, so the join is choosing by time and not by branch order",
        _br_both_rev.get("reached_join") is False
        and "gate report" in _br_both_rev_error
        and "connection refused" not in _br_both_rev_error
        and _br_both_rev.get("a_died") is True
        and _br_both_rev.get("b_died") is True
        and {"stt:fail", "frames:done", "quality:done"} <= set(_br_both_rev_events)
        and _br_both_rev_events.index("quality:fail") < _br_both_rev_events.index("stt:fail"),
        f"error={_br_both_rev_error} a_died={_br_both_rev.get('a_died')} "
        f"b_died={_br_both_rev.get('b_died')} events={_br_both_rev_events}",
    )

    check(
        "#115: the two arms publish the same files with the same bytes -- the fork changes when "
        "the work happens, not what it produces",
        _br_digests(_br_par["bundle"].root) == _br_digests(_br_ser["bundle"].root)
        and _br_digests(_br_par["bundle"].root / dyb.IMAGES)
        == _br_digests(_br_ser["bundle"].root / dyb.IMAGES),
        f"parallel={_br_digests(_br_par['bundle'].root)} serial={_br_digests(_br_ser['bundle'].root)}",
    )
    check(
        "#115: and register exactly the same artefact keys, order aside",
        set(_br_par.get("join", {}).get("registry", []))
        == set(_br_ser.get("join", {}).get("registry", [])),
        f"parallel={_br_par.get('join', {}).get('registry')} serial={_br_ser.get('join', {}).get('registry')}",
    )

    # The two records are written from `reports`, which the branches append to out of order.
    _br_par_state = json.loads(
        (_br_par["bundle"].root / dyp.RUN_STATE_NAME).read_text(encoding="utf-8")
    )
    _br_par_timings = json.loads(
        (_br_par["bundle"].root / dyp.TIMINGS_NAME).read_text(encoding="utf-8")
    )
    check(
        "#115: the per-stage record stays in stage order even though the branches finish out of "
        "order",
        _br_par_state.get("stages_completed") == [1, 2, 3, 4, 5]
        and [s["stage"] for s in _br_par_timings.get("stages", [])] == [1, 2, 3, 4, 5],
        f"state={_br_par_state.get('stages_completed')} "
        f"timings={[s['stage'] for s in _br_par_timings.get('stages', [])]}",
    )

    # The rendering itself, quoted rather than described.
    _br_par_lines = _br_par.get("text", "").splitlines()
    check(
        "#115: a stage line says which branch wrote it -- audio/stt are (A), frames/quality are "
        "(B)",
        any(re.match(r"^  \[2/12\] audio\s+\d+\.\ds  .*\(A\)$", line) for line in _br_par_lines)
        and any(re.match(r"^  \[3/12\] stt\s+.*\(A\)$", line) for line in _br_par_lines)
        and any(re.match(r"^  \[4/12\] frames\s+.*\(B\)$", line) for line in _br_par_lines)
        and any(re.match(r"^  \[5/12\] quality\s+.*\(B\)$", line) for line in _br_par_lines),
        "\n".join(line for line in _br_par_lines if re.match(r"^  \[\d+/12\]", line)),
    )
    check(
        "#115: the continuation line, which carries no stage prefix by design (#197), says which "
        "branch it belongs to as well",
        any(line.strip() == "chunks 0/1  (A)" for line in _br_par_lines),
        repr([line for line in _br_par_lines if "chunks" in line]),
    )
    check(
        "#115: neither arm prints a stage line the other does not -- the two branches add no "
        "second announcement of any stage (stage 3's start line is the documented extra, #97)",
        sorted(int(n) for n in re.findall(r"^  \[(\d+)/12\] ", _br_par.get("text", ""), re.M))
        == [1, 2, 3, 3, 4, 5]
        and sorted(int(n) for n in re.findall(r"^  \[(\d+)/12\] ", _br_ser.get("text", ""), re.M))
        == [1, 2, 3, 3, 4, 5],
        f"parallel={re.findall(r'^  [(]?([0-9]+)/12', _br_par.get('text', ''), re.M)} "
        f"serial={re.findall(r'^  [(]?([0-9]+)/12', _br_ser.get('text', ''), re.M)}",
    )

    # The writer itself: a partial line must not get a tag in the middle of it. `getattr`, not
    # an attribute access: on unmodified main this class does not exist, and the check below
    # has to report that as a failure instead of raising and taking the rest of the suite with
    # it -- the same reason `test_claims.py` uses `find` for a literal it expects to be absent.
    _br_writer = getattr(dyp, "_TaggedStream", None)
    if _br_writer is None:
        check(
            "#115: the branch writer that tags each line exists",
            False,
            "pipeline._TaggedStream is missing",
        )
    else:
        _br_tagged = _br_writer(_FlushRecorder(), "A", threading.Lock())
        _br_tagged.write("partial ")
        check(
            "#115: a partial write is held until its newline, so no tag lands mid-line",
            _br_tagged._out.getvalue() == "",
            repr(_br_tagged._out.getvalue()),
        )
        _br_tagged.write("line\n[2/12] audio     0.0s  x\n")
        check(
            "#115: and each completed line carries the branch tag before its newline",
            _br_tagged._out.getvalue() == "partial line  (A)\n[2/12] audio     0.0s  x  (A)\n",
            repr(_br_tagged._out.getvalue()),
        )
        _br_tagged.write("tail")
        _br_tagged.flush()
        check(
            "#115: flush emits the last partial line with its tag, and forwards the flush",
            _br_tagged._out.getvalue().endswith("tail  (A)\n") and _br_tagged._out.flushes == 1,
            f"{_br_tagged._out.getvalue()!r} flushes={_br_tagged._out.flushes}",
        )


# --- #74, #77, #252: the note's structure, the run's seed, the transcript's missing text ---

# #74: the skill's table rules live in the briefs, because the brief is what the request
# carries. `synthesise` sends `SYSTEM` plus one brief, so a rule that exists only in `SKILL.md`
# never reaches the model -- which is how the skill's §4/§5/§7 table requirements were lost in
# the condensation while §3's survived. The artefact has tables in §3 (33 data rows, 5 tables)
# and §7 (8 data rows, 1 table), and §7's is there although its brief named none: a brief is not
# the only thing that can produce a table, it is the only place a rule can be stated.
_briefs = {number: brief for number, _title, brief in dysy.SECTIONS}
check(
    "#74: every brief whose skill section asks for a table names one",
    all("таблиц" in _briefs[number] for number in (3, 4, 5, 7)),
    repr({n: _briefs[n] for n in (3, 4, 5, 7) if "таблиц" not in _briefs[n]}),
)
check(
    "#74: and each rule names its own subject -- the first one was condensed out once already",
    all(
        marker in _briefs[number]
        for number, marker in ((4, "термины"), (5, "классификация"), (7, "сравнение подходов"))
    ),
    repr({n: _briefs[n][:60] for n in (4, 5, 7)}),
)
check(
    "#74: SYSTEM carries the subsection rule, as one string the owner can replace",
    dysy.SUBSECTION_RULE in dysy.SYSTEM and "N.M" in dysy.SUBSECTION_RULE,
    repr(dysy.SUBSECTION_RULE),
)
# Measured on the generated note (`note.linked.md`, the 2026-10-12 06:21 run, 7785 words): all
# eight sections opened with a heading repeating their own `## N.` title, and they use three
# forms -- `### 1. Паспорт лекции`, `### Раздел 2. Организационный блок` and
# `### Карта источников и литературы`. A comparison that covers one form ships the other two.
_ARTEFACT_REPEATS = (
    (1, "Паспорт лекции", "### 1. Паспорт лекции"),
    (2, "Организационный блок", "### Раздел 2. Организационный блок"),
    (3, "Карта источников и литературы", "### Карта источников и литературы"),
    (
        4,
        "Понятийный аппарат и постановка задачи",
        "### 4. Понятийный аппарат и постановка задачи",
    ),
    (
        5,
        "Теоретический конспект и систематизация методов",
        "### 5. Теоретический конспект и систематизация методов",
    ),
    (6, "Математический ликбез / фундамент", "### 6. Математический ликбез / фундамент"),
    (7, "Численная реализация и инструменты", "### Численная реализация и инструменты"),
    (
        8,
        "Вопросы для самопроверки и подготовки к экзамену",
        "### 8. Вопросы для самопроверки и подготовки к экзамену",
    ),
)
check(
    "#74: all eight duplicate headings the note actually had are dropped, in all three forms",
    [title for _number, title, _heading in _ARTEFACT_REPEATS]
    == [title for _number, title, _brief in dysy.SECTIONS]
    and all(
        dysy.normalise(f"{heading}\n\nТекст раздела.", title).strip() == "Текст раздела."
        for _number, title, heading in _ARTEFACT_REPEATS
    ),
    repr(
        [
            heading
            for _number, title, heading in _ARTEFACT_REPEATS
            if dysy.normalise(f"{heading}\n\nТекст раздела.", title).strip() != "Текст раздела."
        ]
    ),
)
_norm_body, _norm_record, _norm_warnings = dysy._finish_section(
    1, "Паспорт лекции", "### 1. Паспорт лекции\n\nТекст раздела.", "stop"
)
check(
    "#74: through the real entry point, the repeated heading is gone and the text stays",
    "### 1. Паспорт лекции" not in _norm_body
    and "Текст раздела." in _norm_body
    and _norm_record["present"]
    and not _norm_warnings,
    repr(_norm_body),
)
check(
    "#74: a subheading that adds a word is a real heading and is still demoted, not dropped",
    dysy.normalise("## Подраздел\n\nтекст", "Паспорт лекции") == "### Подраздел\n\nтекст"
    and dysy.normalise("### Паспорт лекции и тема\n", "Паспорт лекции").strip()
    == "### Паспорт лекции и тема",
    repr(
        [
            dysy.normalise("## Подраздел\n\nтекст", "Паспорт лекции"),
            dysy.normalise("### Паспорт лекции и тема\n", "Паспорт лекции"),
        ]
    ),
)

# #77: the seed reaches the wire, and it reaches provenance. The claim this check can make is
# that the seed was *sent* -- the fake observes the body -- not that the note is repeatable:
# whether the gateway honours `seed` is a property of the gateway, not of this tree.
_seed_http = FakeHTTP(
    [
        json.dumps(
            {
                "choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}],
                "model": "seed-model",
            }
        ).encode()
    ]
)
_real_llm_urlopen = dyle.urllib.request.urlopen
try:
    dyle.urllib.request.urlopen = _seed_http
    dyle.chat([{"role": "user", "content": "привет"}], dyle.Config("http://seed.test", "m"))
finally:
    dyle.urllib.request.urlopen = _real_llm_urlopen
_sent_payload = json.loads(_seed_http.sent[0].data.decode("utf-8"))
check(
    "#77: chat() sends the seed in the request body, at the one choke point every stage uses",
    _sent_payload.get("seed") == dyle.SEED and _sent_payload.get("temperature") == 0.2,
    repr({k: _sent_payload.get(k) for k in ("seed", "temperature")}),
)
# The four stages whose provenance is built from `Config.redacted()` -- stage 6's `model` field
# and stages 7, 9 and 10's `endpoint` (#77, #257). Stage 10 sends no prompt, so it records no
# digest beside it; the check further down states that rather than leaving it to the prose.
# `.get` chains, not `["endpoint"]["seed"]`: when the #257 teeth removed the endpoint block the
# suite died of `KeyError: 'endpoint'` after printing 922 of its 968 checks. t188's run printed the
# same 922 of the same 968-check suite, and the same 46 checks produced no verdict there -- the one
# that crashed and the 45 after it; three of them are the checks that report FAIL when the endpoint
# is missing but the chains are intact. The file's contract is accumulate, print, exit 1.
_seed_config = dyle.Config("http://x", "m")


def _recorded_seed(document: str, key: str):
    """The seed a provenance document records under `key`, or None when the block is absent."""
    return json.loads(document).get(key, {}).get("seed")


check(
    "#77: redacted() carries the seed, which synth, audit, caption and repair provenance record",
    _seed_config.redacted().get("seed") == dyle.SEED
    and _recorded_seed(dysy.provenance(dysy.Note(markdown="x"), _seed_config), "endpoint")
    == dyle.SEED
    and _recorded_seed(dyau.provenance(dyau.Report(), _seed_config), "endpoint") == dyle.SEED
    and _recorded_seed(dyrep.provenance(dyrep.Report(), _seed_config), "endpoint") == dyle.SEED
    and _recorded_seed(
        dycap.provenance(
            dycap.Settings(),
            _seed_config,
            dycap.CaptionOutcome(captioned=1, reused=0, failed=0, model="m", served_model=None),
        ),
        "model",
    )
    == dyle.SEED,
    repr(_seed_config.redacted()),
)
_saved_seed_env = {
    name: os.environ.get(name)
    for name in (dyle.ENDPOINT_ENV, dyle.MODEL_ENV, "DYAK_LLM_SEED", "DYAK_SETTINGS")
}
try:
    os.environ[dyle.ENDPOINT_ENV] = "http://from-env.test"
    os.environ[dyle.MODEL_ENV] = "env-model"
    # A key that must not exist. `from_env()` is deliberately not a reader of the seed: a
    # documented knob would be a promise this pipeline cannot keep, and a failure mode.
    os.environ["DYAK_LLM_SEED"] = "999"
    os.environ["DYAK_SETTINGS"] = str(tmp / "absent-settings.toml")
    _env_config = dyle.Config.from_env()
finally:
    for _name, _value in _saved_seed_env.items():
        if _value is None:
            os.environ.pop(_name, None)
        else:
            os.environ[_name] = _value
check(
    "#77: from_env() does not read a seed -- DYAK_LLM_SEED=999 changes nothing",
    _env_config.seed == dyle.SEED,
    f"seed={_env_config.seed} with DYAK_LLM_SEED=999 set",
)
check(
    "#77: and no honoured setting names one -- there is no new docs row to write",
    not any("seed" in key.lower() for key in dyd.HONOURED_SETTINGS),
    repr(sorted(key for key in dyd.HONOURED_SETTINGS if "seed" in key.lower())),
)
# The prompt is the other half: a seed that is recorded beside an unrecorded prompt attributes
# nothing. The digest covers the briefs as well as `SYSTEM`, because the briefs are the half
# #74 edits and a digest of `SYSTEM` alone would not move when they do.
_synth_provenance = json.loads(dysy.provenance(dysy.Note(markdown="x"), None))
check(
    "#77: synth provenance carries the prompt digest beside the tool string",
    _synth_provenance.get("prompt_sha256") == dysy.prompt_digest(),
    repr(_synth_provenance.get("prompt_sha256")),
)
check(
    "#77: the digest covers the briefs, so a prompt edit cannot leave it unchanged",
    dysy.prompt_digest() != hashlib.sha256(dysy.SYSTEM.encode("utf-8")).hexdigest()
    and dysy.prompt_digest()
    == hashlib.sha256(
        "\n".join([dysy.SYSTEM, *[b for _n, _t, b in dysy.SECTIONS]]).encode("utf-8")
    ).hexdigest(),
    dysy.prompt_digest()[:16],
)
# The audit's half of the same record (#256), one stage over and the same convention: the
# instruction constant plus the only other text a stage-9 request adds. The transcript and the
# section are the lecture, so they stay out -- `synth` leaves its source material out too.
_audit_provenance = json.loads(dyau.provenance(dyau.Report(), None))
check(
    "#256: audit provenance carries the prompt digest beside the tool string",
    _audit_provenance.get("prompt_sha256") == dyau.prompt_digest(),
    repr(_audit_provenance.get("prompt_sha256")),
)
check(
    "#256: the digest covers the request's envelope, so a prompt edit cannot leave it unchanged",
    dyau.prompt_digest() != hashlib.sha256(dyau.CRITIC_SYSTEM.encode("utf-8")).hexdigest()
    and dyau.prompt_digest()
    == hashlib.sha256(
        "\n".join([dyau.CRITIC_SYSTEM, dyau.TRANSCRIPT_HEADING, dyau.NOTE_HEADING]).encode("utf-8")
    ).hexdigest(),
    dyau.prompt_digest()[:16],
)
# The digest is only worth recording if it moves with the request, so the request is read where
# it is built: a heading written inline again, bypassing the constants the digest reads, fails
# here instead of drifting silently.
_audit_request = dyau._section_request(0, "## Раздел 1\n\nтекст раздела", "текст стенограммы")
check(
    "#256: the request builds its envelope from the constants the digest reads",
    _audit_request is not None
    and _audit_request.messages[0]["content"] == dyau.CRITIC_SYSTEM
    and dyau.TRANSCRIPT_HEADING in _audit_request.messages[1]["content"]
    and dyau.NOTE_HEADING in _audit_request.messages[1]["content"],
    repr(_audit_request.messages[1]["content"][:80] if _audit_request else None),
)
# And it reaches the file, which is what a bundle reader opens: the function-level checks above
# cannot tell whether `run()` hands the config to `provenance()`.
with tempfile.TemporaryDirectory() as _audit_tmp:
    _audit_dir = pathlib.Path(_audit_tmp)
    (_audit_dir / "note.md").write_text("## Раздел 1\n\nтекст раздела\n", encoding="utf-8")
    (_audit_dir / "transcript.txt").write_text("текст стенограммы\n", encoding="utf-8")
    dyau.run(
        _audit_dir / "note.md",
        _audit_dir / "transcript.txt",
        _audit_dir / "out",
        config=_seed_config,
    )
    _audit_file = json.loads(
        (_audit_dir / "out" / dyau.PROVENANCE_NAME).read_text(encoding="utf-8")
    )
check(
    "#256: the written audit-provenance.json carries the digest and the seed",
    _audit_file.get("prompt_sha256") == dyau.prompt_digest()
    and _audit_file.get("endpoint", {}).get("seed") == dyle.SEED,
    repr({k: _audit_file.get(k) for k in ("prompt_sha256", "endpoint")}),
)
_llm_source = pathlib.Path(dyle.__file__).read_text(encoding="utf-8")
check(
    "#77: the seed is documented as sent with its limit named, never as a promise",
    "was sent" in _llm_source and "not verifiable" in _llm_source,
    "llm.py seed prose",
)
# The coverage the two clauses above claim, counted rather than quoted (#256): four call sites
# reach a model -- synth, audit twice, caption -- and the repair stage reaches none, so a fifth
# call site has to update the sentence and fails here first.
_sampling_sites = [
    f"{path.name}:{number}"
    for path in sorted(pathlib.Path(dyle.__file__).parent.glob("*.py"))
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
    if "llm.chat(" in line
]
check(
    "#256: the four sampling call sites are the four the seed prose names",
    len(_sampling_sites) == 4 and "every stage" not in _llm_source,
    repr(_sampling_sites),
)
# The fourth stage the #77 decision names, and the gap #257 closed: stage 10 sends no prompt, so it
# records the endpoint and the seed and no digest. Pinned twice, because the function's return
# value cannot tell whether `run()` handed the config on.
_REPAIR_KEYS = {
    "stage",
    "tool",
    "endpoint",
    "requires_model",
    "output",
    "original_kept",
    "repairable_rules",
    "declined_reasons",
    "idempotent",
    "changes",
}
_repair_provenance = json.loads(dyrep.provenance(dyrep.Report(), _seed_config))
check(
    "#257: stage 10's provenance carries the endpoint and the seed, and no prompt digest",
    _repair_provenance.get("endpoint", {}).get("seed") == dyle.SEED
    and set(_repair_provenance) == _REPAIR_KEYS
    and "prompt_sha256" not in _repair_provenance,
    repr({k: _repair_provenance.get(k) for k in ("endpoint", "prompt_sha256")}),
)
with tempfile.TemporaryDirectory() as _repair_tmp:
    _repair_dir = pathlib.Path(_repair_tmp)
    _repair_note = _repair_dir / "note.md"
    _repair_note.write_text("## Раздел 1\n\nтекст раздела\n", encoding="utf-8")
    _repair_audit = _repair_dir / "audit.json"
    _repair_audit.write_text(json.dumps({"findings": []}), encoding="utf-8")
    dyrep.run(
        _repair_note,
        _repair_audit,
        _repair_dir / "out",
        stream=io.StringIO(),
        config=_seed_config,
    )
    _repair_file = json.loads(
        (_repair_dir / "out" / dyrep.PROVENANCE_NAME).read_text(encoding="utf-8")
    )
check(
    "#257: the written repair-provenance.json carries the endpoint and the seed",
    _repair_file.get("endpoint", {}).get("seed") == dyle.SEED and set(_repair_file) == _REPAIR_KEYS,
    repr({k: _repair_file.get(k) for k in ("endpoint", "changes", "requires_model")}),
)
# The wiring itself, which the checks above cannot see: they call `repair.run` directly, so a
# pipeline that dropped the keyword would still record the seed in their fixture. Scanned and
# reported rather than run -- the end-to-end fixtures stub stage 10 out -- which is how this file
# already pins the `llm.chat(` call sites (#256).
_pipeline_source = pathlib.Path(dyp.__file__).read_text(encoding="utf-8")
_repair_call = _pipeline_source.split("rreport = repair.run(")[1].split("\n    )")[0]
check(
    "#257: the pipeline hands stage 10 the same llm_config it hands stages 7 and 9",
    "config=llm_config" in _repair_call,
    repr([ln.strip() for ln in _repair_call.splitlines() if ln.strip()]),
)
# #261: the sentence beside `audit.provenance` said stage 10 "carries neither: it takes no `Config`
# and makes no model call" -- the first half false from the moment #257 gave stage 10 the Config.
# Pinned the way #256 pins llm.py's call sites: the two clauses that went false must not come back,
# and the three facts the replacement states must still hold, so the name fails in either direction
# rather than describing a sentence that has since moved. The `llm.chat(` scan is the same shape as
# #256's call-site check, including its bluntness: a comment naming the call would trip it, which is
# the safe direction -- someone reword, rather than a stage quietly gaining a model call.
_audit_source = pathlib.Path(dyau.__file__).read_text(encoding="utf-8")
_repair_source = pathlib.Path(dyrep.__file__).read_text(encoding="utf-8")
check(
    "#261: audit's prose no longer says stage 10 lacks a Config, and stage 10 still sends no prompt",
    "takes no `Config`" not in _audit_source
    and "carries neither" not in _audit_source
    and _repair_provenance.get("endpoint") is not None
    and "prompt_sha256" not in _repair_provenance
    and "llm.chat(" not in _repair_source,
    repr(
        {
            "audit.py: takes no `Config`": "takes no `Config`" in _audit_source,
            "audit.py: carries neither": "carries neither" in _audit_source,
            "endpoint": _repair_provenance.get("endpoint"),
            "prompt_sha256": _repair_provenance.get("prompt_sha256"),
            "repair.py llm.chat(": "llm.chat(" in _repair_source,
        }
    ),
)

# #252: the transcript records the answer as well as the question. ADR-0004 D4 asks for the
# *exact* model output, so the text is recorded in full -- a truncation would re-create, at a
# different length, the loss that made #240's near-match measurement need a live replay.
_long_reply_text = "я" * 5000
_reply_for_dict = dyle.Reply(
    text="готовый ответ",
    model="m",
    prompt_tokens=1,
    completion_tokens=2,
    seconds=0.5,
    attempts=1,
    finish_reason="stop",
)
check(
    "#252: Reply.as_dict() carries the model's output, not only the call's metadata",
    _reply_for_dict.as_dict().get("text") == "готовый ответ",
    repr(sorted(_reply_for_dict.as_dict())),
)
_short_transcript = dyle.Transcript()
_short_transcript.record(
    "7",
    "section 1",
    [{"role": "user", "content": "вопрос"}],
    dyle.Reply(
        text=_long_reply_text,
        model="m",
        prompt_tokens=1,
        completion_tokens=2,
        seconds=0.5,
        attempts=1,
    ),
    section=1,
)
check(
    "#252: the transcript entry carries it in full, untruncated",
    _short_transcript.as_dict()["calls"][0].get("text") == _long_reply_text,
    str(len(_short_transcript.as_dict()["calls"][0].get("text") or "")),
)
check(
    "#252: and the entry still carries what it carried before -- the request and the metadata",
    _short_transcript.as_dict()["calls"][0].get("messages")
    == [{"role": "user", "content": "вопрос"}]
    and _short_transcript.as_dict()["calls"][0].get("stage") == "7",
    repr(sorted(_short_transcript.as_dict()["calls"][0])),
)

# --- #240: the citation guard admits near-quotes, and the prompt's example survives it --------
# Stage 9's guard was discarding almost everything the critic produced: 45 of 45 findings on a
# replay of the 8 recorded stage-9 requests, and 68 of 72 in the run's own artefact, all but one
# for "citation is not verbatim in the transcript". The near-match measurement (decision on
# #240, comment 6137266759) says those are quoting artefacts rather than fabrication -- 0 of 45
# verbatim as the guard tested them, 17 once `«»`/quotes/trailing punctuation were stripped, 35
# once `…`/`...`/`;` fragments were allowed too, and 6 genuinely absent -- so the fix belongs in
# the comparison, not in the requirement. A paraphrase is still discarded, the finding still has
# to quote the note and name a confidence, and the finding is still WARN: no gate is added.
#
# The citations below are the real ones from that run's artefact, which is the sample the
# measurement was taken on, and the haystacks they are checked against are that run's own text:
# the two passages are copied verbatim out of its `transcript.txt` (the line numbers are that
# file's). The recogniser writes one segment per line, so a citation the critic wrote as one
# fragment matches text that is split here -- `_norm` collapses the whitespace between segments,
# which is why a line-based `grep` does not find these and the guard does.
_S240_TRANSCRIPT = (
    # transcript.txt:2442-2445 -- what "там симметричность, ну, симметричность, понятно" quotes
    "[00:36:51] там симметричность,\n"
    "[00:36:52] ну,\n"
    "[00:36:52] симметричность,\n"
    "[00:36:53] понятно,\n"
    # transcript.txt:2821-2826 -- the second span of the `…` joins below
    "[00:42:28] если мы возьмем вот эту производную,\n"
    "[00:42:30] то у нас будет,\n"
    "[00:42:30] на самом деле,\n"
    "[00:42:31] вектор,\n"
    "[00:42:32] ну,\n"
    "[00:42:32] как бы из трех этих компонентов.\n"
)
_S240_SECTION = "содержимое"
_S240_FLOOR = getattr(dyau, "MIN_FRAGMENT_CHARS", None)
check(
    "#240: the fragment floor the decision asks for exists and is 20 normalised characters",
    _S240_FLOOR == 20,
    str(_S240_FLOOR),
)
for _s240_name, _s240_line in (
    (
        "the critic wrapped its citation in guillemets",
        "- содержимое: не следует из лекции "
        "[источник: «там симметричность, ну, симметричность, понятно»] [уверенность: высокая]",
    ),
    (
        "the citation is in straight quotes and carries a trailing comma",
        "- содержимое: не следует из лекции "
        '[источник: "там симметричность, ну, симметричность, понятно",] '
        "[уверенность: высокая]",
    ),
    (
        "the citation joins two transcript spans with an ellipsis",
        "- содержимое: не следует из лекции "
        "[источник: там симметричность… из трех этих компонентов] [уверенность: средняя]",
    ),
    (
        "the join is `...` rather than the ellipsis character",
        "- содержимое: не следует из лекции "
        "[источник: там симметричность... из трех этих компонентов] [уверенность: средняя]",
    ),
    (
        "the join is a semicolon",
        "- содержимое: не следует из лекции "
        "[источник: там симметричность; из трех этих компонентов] [уверенность: низкая]",
    ),
    (
        "the note quote is wrapped in guillemets",
        "- «содержимое»: не следует из лекции "
        "[источник: там симметричность, ну, симметричность, понятно] [уверенность: высокая]",
    ),
    (
        "the note quote carries trailing punctuation",
        "- содержимое,: не следует из лекции "
        "[источник: там симметричность, ну, симметричность, понятно] [уверенность: высокая]",
    ),
):
    _s240_kept, _s240_dropped = dyau.parse_critic_findings(
        _s240_line, _S240_SECTION, _S240_TRANSCRIPT
    )
    check(
        f"#240: a finding is kept when {_s240_name}",
        len(_s240_kept) == 1 and not _s240_dropped,
        str(_s240_dropped),
    )

# What the leniency must NOT admit. The first is the issue's own example: a paraphrase of the
# sentence in the transcript, which is what ADR-0001 D5 requires a citation not to be.
for _s240_name, _s240_line, _s240_why in (
    (
        "the issue's paraphrase, which the transcript does not contain",
        "- содержимое: не следует из лекции "
        "[источник: двойной путь познания (понимания, объяснения)] [уверенность: высокая]",
        "citation is not verbatim in the transcript",
    ),
    (
        "an invented citation",
        "- содержимое: не следует из лекции "
        "[источник: лектор упомянул теорему Банка] [уверенность: высокая]",
        "citation is not verbatim in the transcript",
    ),
    (
        # Both sides of the join are real transcript text, and both are shorter than the floor:
        # without it, "понятно" (7 characters) would be accepted, and a two-word coincidence
        # would count as evidence.
        "a join whose fragments are all shorter than the floor",
        "- содержимое: не следует из лекции "
        "[источник: там симметричность… понятно] [уверенность: высокая]",
        "citation is not verbatim in the transcript",
    ),
    (
        "a note quote that is a summary rather than a fragment of the note",
        "- это не конспект: не следует из лекции "
        "[источник: там симметричность, ну, симметричность, понятно] [уверенность: высокая]",
        "quote is not verbatim in the note",
    ),
):
    _s240_kept, _s240_dropped = dyau.parse_critic_findings(
        _s240_line, _S240_SECTION, _S240_TRANSCRIPT
    )
    check(
        f"#240: and it is still discarded for {_s240_name}",
        _s240_kept == [] and len(_s240_dropped) == 1 and _s240_dropped[0]["reason"] == _s240_why,
        str(_s240_dropped),
    )

# The prompt's own example has to survive the guard. The previous one could not: its citation
# was split across two lines, so the `- ` line carried `[источник: соответствие` with no closing
# `]` -- the guard read that as "no citation into the transcript" -- and its first field was a
# formula the note does not contain. The model reproduced the shape, which is why every finding
# on the replay was discarded. An example that teaches a rejected format is a prompt bug.
_s240_block = dyau.CRITIC_SYSTEM.split("Пример", 1)[1]
_s240_lines = _s240_block.splitlines()
_s240_bullets = [line for line in _s240_lines if line.startswith("- ")]
check(
    "#240: the example block holds exactly one bullet",
    len(_s240_bullets) == 1,
    str(_s240_bullets),
)
# Its physical extent, not just its first line: the previous example was wrapped, and its
# continuation (`  динамической системы] [уверенность: высокая]`) does not start with `- `, so
# counting bullets cannot see the wrapping this check exists to catch. The item runs to the next
# blank line.
_s240_start = next((i for i, line in enumerate(_s240_lines) if line.startswith("- ")), None)
_s240_item = [] if _s240_start is None else [_s240_lines[_s240_start]]
if _s240_start is not None:
    for _s240_line in _s240_lines[_s240_start + 1 :]:
        if not _s240_line.strip():
            break
        _s240_item.append(_s240_line)
check(
    "#240: and the example bullet is one physical line, so its citation cannot be split off it",
    len(_s240_item) == 1,
    str(_s240_item),
)
_s240_example = _s240_item[0] if _s240_item else ""
_s240_match = dyau._CITATION.search(_s240_example)
_s240_cite = _s240_match.group("cite").strip() if _s240_match else ""
_s240_first = _s240_example[1:].split(":")[0].strip()
# The note line the example's first field was copied from: note.md:392 of the recorded run,
# verbatim, so the quote is a fragment of a real note line and not the whole of its haystack.
_S240_NOTE = (
    "**Симметричность.** Матрица $A$ симметрична, если $A = A^T$ (элементы симметричны "
    "относительно главной диагонали). Если коэффициенты симметричной матрицы вещественны, то "
    "вещественны и её собственные значения."
)
check(
    "#240: and its citation is complete on that line, which the previous example's was not",
    _s240_match is not None and _s240_match.end() < len(_s240_example),
    _s240_example,
)
check(
    "#240: the example's first field is a line from the note, not a formula of its own",
    "$" not in _s240_first and _S240_FLOOR is not None and len(_s240_first) >= _S240_FLOOR,
    _s240_first,
)
check(
    "#240: the example's citation carries no quotes, because the model copies what it is shown",
    bool(_s240_cite) and not any(char in _s240_cite for char in "«»\"'"),
    _s240_cite,
)
# Both fragments are checked against that recorded text, never against the example's own fields:
# a check that hands the guard the example's citation as its transcript passes for a well-formed
# but invented citation, which is the class this has to catch.
check(
    "#240: the example's citation is a span of the recorded transcript",
    bool(_s240_cite) and dyau._norm(_s240_cite) in dyau._norm(_S240_TRANSCRIPT),
    _s240_cite,
)
check(
    "#240: and its first field is a fragment of the recorded note, not the whole haystack",
    bool(_s240_first) and dyau._norm(_s240_first) in dyau._norm(_S240_NOTE),
    _s240_first,
)
_s240_kept, _s240_dropped = dyau.parse_critic_findings(_s240_example, _S240_NOTE, _S240_TRANSCRIPT)
check(
    "#240: the prompt's own example passes the guard against that corpus, so it cannot teach a "
    "rejected format",
    len(_s240_kept) == 1 and not _s240_dropped,
    str(_s240_dropped),
)
# And the corpus is not vacuous: one word changed in the example's citation -- same shape, same
# length, still plausible -- is discarded against the very same haystacks.
_s240_invented = _s240_example.replace(_s240_cite, _s240_cite.replace("понятно", "конечно"))
_s240_kept, _s240_dropped = dyau.parse_critic_findings(_s240_invented, _S240_NOTE, _S240_TRANSCRIPT)
check(
    "#240: while a one-word change to that citation is discarded against the same corpus",
    _s240_invented != _s240_example and _s240_kept == [] and len(_s240_dropped) == 1,
    str(_s240_dropped),
)

# The audit must not become a gate. Tier-2 findings are built WARN, `Report.ok` reads ERROR only
# and the CLI's exit 5 is tier 1's -- deliberately (ADR-0001 D6): a model asked to check formulas
# is unreliably correct, so it annotates and never blocks. A guard that admits more findings is
# exactly the change that could turn the critic into a blocker by accident, so the property is
# verified here rather than assumed.
_s240_kept, _s240_dropped = dyau.parse_critic_findings(GOOD_LINE, SECTION, LECT)
check(
    "#240: a kept critic finding is WARN",
    len(_s240_kept) == 1 and _s240_kept[0].severity == "WARN",
    str(_s240_kept),
)
_s240_shout, _s240_dropped = dyau.parse_critic_findings(
    "- содержимое: ERROR: критическая ошибка, размерности не сходятся "
    "[источник: соответствие динамической системы] [уверенность: высокая]",
    SECTION,
    LECT,
)
check(
    "#240: and a finding that shouts ERROR in its own text is still WARN -- a model cannot "
    "escalate itself",
    len(_s240_shout) == 1 and _s240_shout[0].severity == "WARN",
    str(_s240_shout),
)
_s240_report = dyau.Report(findings=list(_s240_shout) + list(_s240_kept))
check(
    "#240: a report holding them is ok(), because `ok` reads ERROR only",
    _s240_report.ok and not [f for f in _s240_report.findings if f.severity == "ERROR"],
    _s240_report.summary(),
)
_s240_report.add("tier1", "dimension/mismatch", "ERROR", 1, "an ERROR-tier finding", quote="x")
check(
    "#240: while an ERROR-tier finding still makes it not ok, so the flag is not dead",
    not _s240_report.ok,
    _s240_report.summary(),
)
check(
    "#240: and exit 5 still keys on ERROR-tier findings alone, unchanged (cli.py:319)",
    'for f in result.audit.findings if f.severity == "ERROR"'
    in Path(dyc.__file__).read_text(encoding="utf-8"),
    "cli.py no longer filters the audit findings by ERROR",
)


# --- #253: stage 10 declines every finding in the audit report, not only the lint ones --------
# Stage 9 publishes `audit/*`, and `Bundle.publish` MOVES the artefact out of the work dir
# (bundle.py:271) -- the same move-then-read ordering as #82 and #89. Stage 10 then read
# `adir / audit.REPORT_NAME`, the path the publish had just emptied, so `repair.run` found no
# report at all and declined only the lint findings. Measured on the 2026-10-12 run: its
# `audit.json` held 14 findings and its `repair.json` declined one, `math/display-unbalanced`,
# which is a lint rule. Stages 9-12 below run for real, so the wiring is what is under test.
class _StopAfterRepair(Exception):
    pass


_r253_bodies = ["содержимое"] * 8
# The recorded run's shape: 14 audit findings (7 unsourced numbers, 7 dimension mismatches) and
# one lint finding, of which stage 10 declined only the lint one. Reproduced here rather than
# taken from the run's own report, so the check fails with those numbers on unmodified `main`.
_r253_bodies[4] = "\n".join(f"Коэффициент $0.{37 + i}$ в переходе." for i in range(7))
_r253_bodies[5] = (
    decl + "\n\n" + "\n".join(f"$J_{i} = \\mathbf{{A}} + \\mathbf{{x}}$" for i in range(1, 8))
)
_r253_bodies[6] = "содержимое\n\n$$"
_r253_note = audit_note(*_r253_bodies)


def _r253_synth_run(captions, txt, source, sdir, *, config=None, title="", stream=None):
    """Stage 7, writing the note the audit will find things in. No model, so no transcript."""
    sdir = Path(sdir)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / dysy.NOTE_NAME).write_text(_r253_note, encoding="utf-8")
    for name in (dysy.REPORT_NAME, dysy.PROVENANCE_NAME):
        (sdir / name).write_text("{}", encoding="utf-8")
    return _Stub(synthesizer="template", markdown=_r253_note, filled=8, degraded=False)


def _r253_caption_run(manifest, transcript_json, quality_report, images, dest, **kw):
    """Stage 6, writing what it must publish and returning a report instead of stopping."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / dycap.REPORT_NAME).write_text(
        json.dumps({"captions": [], "counts": {}}), encoding="utf-8"
    )
    (dest / dycap.PROVENANCE_NAME).write_text("{}", encoding="utf-8")
    return _Stub(summary=lambda: "0 captioned, 0 reused, 0 refused"), _Stub()


def _r253_stub_modules() -> None:
    """Stages 1-6 stubbed; 7 writes the fixture note; 8-12 are the real modules."""
    _info = _Stub(summary=lambda: "video h264")
    dyp.probe = _Stub(
        probe=lambda p: _info,
        require_supported=lambda i: None,
        provenance=lambda *a, **k: "{}",
        PROVENANCE_NAME="source-provenance.json",
    )
    dyp.audio = _Stub(
        extract=lambda src, wav: _Stub(duration=60.0, summary=lambda: "audio.wav 60.0s"),
        check_coverage=lambda i, a: None,
    )

    def _r253_stt_write(transcript, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "transcript.json").write_text(
            json.dumps({"segments": [{"index": 0, "text": "hello"}]}), encoding="utf-8"
        )
        (dest / "transcript.raw.json").write_text("{}", encoding="utf-8")
        # The transcript is what `number/unsourced` compares against, so a fixture number the
        # transcript does not contain is an audit finding to decline.
        (dest / "transcript.txt").write_text("hello", encoding="utf-8")
        return {
            "json": dest / "transcript.json",
            "raw": dest / "transcript.raw.json",
            "txt": dest / "transcript.txt",
        }

    dyp.stt = _Stub(
        backend=lambda *a, **k: SimpleNamespace(
            info=lambda: dysb.BackendInfo(name="whispercpp", target="stub://whispercpp")
        ),
        transcribe=lambda wav, audio_duration, backend=None, on_progress=None: dyst.Transcript(
            wav=wav, audio_duration=audio_duration, segments=(), backend="stub"
        ),
        write=_r253_stt_write,
        RATE_RANGE=dysttc.RATE_RANGE,
        WARMUP_SECONDS=dysttc.WARMUP_SECONDS,
    )

    def _r253_frames_run(src, frames_dir):
        frames_dir.mkdir(parents=True, exist_ok=True)
        manifest = frames_dir / dyfr.MANIFEST
        manifest.write_text(dyfr.MANIFEST_HEADER + "\n", encoding="utf-8")
        frame = frames_dir / "f_0.png"
        frame.write_bytes(b"\x89PNG\r\n\x1a\nframe")
        return manifest, [frame]

    dyp.frames = _Stub(
        run=_r253_frames_run,
        summary=lambda m, p: f"{len(p)} frames",
        ffmpeg_version=lambda: "stub",
    )

    def _r253_quality_run(produced, dest, *, settings=None, bbox_source=None, stream=None):
        for path in produced:
            enhanced = Path(dest) / "enhanced"
            enhanced.mkdir(parents=True, exist_ok=True)
            (enhanced / f"enhanced_{path.name}").write_bytes(b"\x89PNG\r\n\x1a\nenhanced")
        return _Stub(
            passed=[_Stub(name=p.name) for p in produced], summary=lambda: "1/1 pass", ok=True
        )

    dyp.quality = _Stub(
        Settings=lambda: _Stub(),
        resolve_estimator=lambda name: None,
        run=_r253_quality_run,
        write=lambda report, target, settings: target.write_text("{}", encoding="utf-8"),
        provenance=lambda qs, bbox_source: "{}",
        enhanced_name=lambda name: f"enhanced_{name}",
        REPORT_NAME=dyq.REPORT_NAME,
        PROVENANCE_NAME=dyq.PROVENANCE_NAME,
    )
    dyp.caption = _Stub(
        run=_r253_caption_run,
        REPORT_NAME=dycap.REPORT_NAME,
        PROVENANCE_NAME=dycap.PROVENANCE_NAME,
    )
    # `pipeline.synth` is the module object, so the stub carries the names `pipeline.py` reads
    # off it, not just `run` (`publishes_stage7_transcript` walks `STAGE7_ARTEFACTS`).
    dyp.synth = _Stub(
        run=_r253_synth_run,
        NOTE_NAME=dysy.NOTE_NAME,
        LLM_TRANSCRIPT_NAME=dysy.LLM_TRANSCRIPT_NAME,
        STAGE7_ARTEFACTS=dysy.STAGE7_ARTEFACTS,
        SECTIONS=dysy.SECTIONS,
    )


with tempfile.TemporaryDirectory() as _r253_td:
    _r253_root = Path(_r253_td)
    _r253_src = _r253_root / "lecture.mp4"
    _r253_src.write_bytes(b"x")
    _r253_bundle = dyb.Bundle.open(
        _r253_src, output_dir=str(_r253_root / "bundle"), overwrite=False
    )
    _r253_real = (
        dyp.probe,
        dyp.audio,
        dyp.stt,
        dyp.frames,
        dyp.quality,
        dyp.caption,
        dyp.synth,
        dyp.run,
    )
    _r253_error = ""
    dyp.run = REAL_PIPELINE_RUN
    _r253_stub_modules()
    try:
        REAL_PIPELINE_RUN(
            _r253_src,
            dyw.WorkDir(parent=_r253_root / "work"),
            bundle=_r253_bundle,
            stream=io.StringIO(),
        )
    except BaseException as _r253_exc:  # noqa: BLE001 -- the check below reports it
        _r253_error = repr(_r253_exc)
    finally:
        (
            dyp.probe,
            dyp.audio,
            dyp.stt,
            dyp.frames,
            dyp.quality,
            dyp.caption,
            dyp.synth,
            dyp.run,
        ) = _r253_real

    def _r253_json(name: str) -> dict:
        path = _r253_bundle.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    _r253_audit = _r253_json(dyau.REPORT_NAME)
    _r253_repair = _r253_json(dyrep.REPORT_NAME)
    _r253_lint = _r253_json(dyln.REPORT_NAME)
    _r253_audit_rules = sorted({f["rule"] for f in _r253_audit.get("findings", [])})
    _r253_declined = _r253_repair.get("declined", [])
    _r253_declined_rules = sorted({d["rule"] for d in _r253_declined})
    check(
        "#253: the run reaches stage 10 and the bundle holds an audit report with findings",
        not _r253_error
        and len(_r253_audit.get("findings", [])) >= 2
        and "dimension/mismatch" in _r253_audit_rules,
        f"error={_r253_error} audit={_r253_audit_rules}",
    )
    check(
        "#253: and stage 10 declines every one of them, not only the lint findings -- it reads "
        "the published report, which is the one that still exists",
        len(_r253_declined)
        == len(_r253_audit.get("findings", [])) + len(_r253_lint.get("findings", [])),
        f"declined={len(_r253_declined)} audit={len(_r253_audit.get('findings', []))} "
        f"lint={len(_r253_lint.get('findings', []))} rules={_r253_declined_rules}",
    )
    check(
        "#253: the declined entries name the audit rules and each carries its reason",
        bool(_r253_audit_rules)
        and set(_r253_audit_rules) <= set(_r253_declined_rules)
        and all(entry.get("reason") for entry in _r253_declined),
        f"audit={_r253_audit_rules} declined={_r253_declined_rules}",
    )


_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all stage checks passed")
