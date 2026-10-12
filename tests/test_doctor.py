#!/usr/bin/env python3
"""Regression: `dyak doctor` honours the ADR-0001 exit-code contract.

Stage 0 exists to pin the error contract before four other stages grow their own
ad-hoc reporting. So the thing under test is mostly the distinction between:

  * not on PATH            -> 2, named, with a remediation line
  * on PATH but failing    -> 3, its stderr reproduced VERBATIM
  * not configured at all  -> not a failure (the gateway is optional config)

PATH and subprocess are stubbed; nothing here touches the real network or the
real binaries.
"""

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()
sys.path.insert(0, str(REPO / "src"))
from dyak import cli as dyc  # noqa: E402
from dyak import deps as dyd  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


# --- stubs -------------------------------------------------------------------
PRESENT = {"ffmpeg", "ffprobe"}
real_which = dyd.shutil.which
real_run = dyd.subprocess.run

which_set: set[str] = set(PRESENT)
run_rc = 0
# Keyed by executable: a single shared blob would make ffprobe report ffmpeg's version
# as its own.
run_stdout: dict[str, str] = {
    "ffmpeg": "ffmpeg version 7.1.1-1 Copyright (c) 2000-2024 the FFmpeg developers\n",
    "ffprobe": "ffprobe version 7.1.1-1 Copyright (c) 2007-2024\n",
}
run_stderr = ""


def fake_which(name):
    if name in which_set:
        return f"/usr/bin/{name}"
    return None


def fake_run(argv, **kwargs):
    out = run_stdout.get(Path(argv[0]).name, "")
    return subprocess.CompletedProcess(argv, run_rc, out, run_stderr)


import dyak.stt as gld_stt  # noqa: E402
import dyak.stt.whispercpp as dywsc  # noqa: E402

dyd.shutil.which = fake_which
dyd.subprocess.run = fake_run

# The model endpoint must never be probed against the real network in this test, and
# neither must the STT endpoint. Before this pin, `doctor` reached whatever was listening
# on 127.0.0.1:10302, so the suite's verdict depended on container state.
os.environ.pop(dyd.LLM_ENV, None)
real_stt_backend = os.environ.pop(gld_stt.ENV_BACKEND, None)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
real_stt_urlopen = dywsc.urllib.request.urlopen

stt_reachable = True


def fake_stt_urlopen(request, timeout=None):
    if not stt_reachable:
        raise urllib.error.URLError(OSError(111, "Connection refused"))
    return _HealthResponse()


class _HealthResponse:
    def read(self, _n=-1):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


dywsc.urllib.request.urlopen = fake_stt_urlopen
tmp = Path(tempfile.mkdtemp())
os.environ[dyd.VAULT_ENV] = str(tmp)
# `dyak process` on a first run writes the vault it used to the settings file. Keep that
# inside the temp dir: a suite must not create files in the developer's home.
real_settings_env = os.environ.get(dyd.SETTINGS_PATH_ENV)
os.environ[dyd.SETTINGS_PATH_ENV] = str(tmp / "settings.toml")


def restore():
    dyd.shutil.which = real_which
    dyd.subprocess.run = real_run
    dywsc.urllib.request.urlopen = real_stt_urlopen
    if real_stt_backend is None:
        os.environ.pop(gld_stt.ENV_BACKEND, None)
    else:
        os.environ[gld_stt.ENV_BACKEND] = real_stt_backend
    if real_settings_env is None:
        os.environ.pop(dyd.SETTINGS_PATH_ENV, None)
    else:
        os.environ[dyd.SETTINGS_PATH_ENV] = real_settings_env


# --- 1. everything present -> 0, with path and version listed -----------------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
text = out.getvalue()
check("all present -> exit 0", rc == ec.OK, f"rc={rc}")
check("nothing on stderr when healthy", err.getvalue() == "", err.getvalue())
check(
    # `gateway` was here until #68. The list is what `doctor` exists to be: an
    # environment report naming a dependency the pipeline does not have is the same
    # failure as omitting one it does.
    "every dependency is listed, and none that does not exist",
    all(n in text for n in ("ffmpeg", "ffprobe", "stt", "llm", "vault")) and "gateway" not in text,
    text,
)
check("resolved path is reported", "/usr/bin/ffmpeg" in text, text)
check("version is reported", "ffmpeg version 7.1.1-1" in text, text)
check("an unconfigured model endpoint is skipped, not failed", "[skip]" in text, text)

# --- 2. the STT endpoint is unreachable -> 3, named, with remediation ---------
# This replaced the old "shipboard is not on PATH -> exit 2" case. A PATH check could not
# detect an endpoint that is configured and down, which is the failure that actually costs
# a 6 s audio extraction before stage 3 reports it.
stt_reachable = False
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
msg = err.getvalue()
check("unreachable STT endpoint -> exit 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("the failing check is named", "stt" in msg, msg)
# The detail line (with the target URL) is rendered on stdout by _render; stderr
# carries the name and the remediation.
check("the unreachable target is named", "127.0.0.1" in out.getvalue(), out.getvalue())
check("the remediation names the env var", "DYAK_WHISPERCPP_URL" in msg, msg)
check("shipboard is no longer the reason", "pipx install shipboard" not in msg, msg)
check(
    "present deps still reported as ok",
    "[ok  ] ffmpeg" in out.getvalue() and "[ok  ] ffprobe" in out.getvalue(),
    out.getvalue(),
)

# A binary genuinely missing still earns exit 2, distinct from a broken endpoint.
which_set = {"ffmpeg"}  # ffprobe gone, shipboard is not consulted any more
stt_reachable = True
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
msg = err.getvalue()
check("missing binary -> exit 2", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
check("missing binary is named", "ffprobe" in msg, msg)
check("its remediation names the package", "pacman -S ffmpeg" in msg, msg)
which_set = set(PRESENT)

# --- 3. present but failing -> 3, stderr VERBATIM ----------------------------
which_set = set(PRESENT)
run_rc = 1
# distinctive, multi-line, with leading whitespace and punctuation that a
# paraphrase would mangle.
run_stderr = (
    "ffmpeg: error while loading shared libraries: libavutil.so.60: "
    "cannot open shared object file\n"
    "  extra indented line: '{\"a\": 1}'\n"
)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
msg = err.getvalue()
check("present-but-failing -> exit 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("failure names the dependency", "ffmpeg" in msg, msg)
check(
    "stderr reproduced verbatim (full first line)",
    "ffmpeg: error while loading shared libraries: libavutil.so.60: "
    "cannot open shared object file" in msg,
    msg,
)
check(
    "stderr reproduced verbatim (indented line, quotes intact)",
    "  extra indented line: '{\"a\": 1}'" in msg,
    msg,
)
check("a present-but-broken dep is NOT reported as missing", "remediation: pacman" not in msg, msg)
run_rc = 0
run_stderr = ""

# --- 4. missing beats failing when both are true ------------------------------
which_set = {"ffmpeg"}  # shipboard missing, ffprobe missing
run_rc = 1
run_stderr = "boom\n"
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
check("worst-code wins over present-but-failing", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
run_rc = 0
run_stderr = ""

# --- 5. vault: missing path, not a dir, unwritable ---------------------------
which_set = set(PRESENT)
os.environ[dyd.VAULT_ENV] = str(tmp / "nope")
check(
    "absent vault -> 2", dyd.check_vault().code == ec.MISSING_DEPENDENCY, dyd.check_vault().detail
)

a_file = tmp / "a-file"
a_file.write_text("x")
os.environ[dyd.VAULT_ENV] = str(a_file)
check(
    "vault that is a file -> 2",
    dyd.check_vault().code == ec.MISSING_DEPENDENCY,
    dyd.check_vault().detail,
)

# unwritable: chmod 500 leaves the dir non-writable for a non-root user
locked = tmp / "locked"
locked.mkdir()
locked.chmod(0o500)
os.environ[dyd.VAULT_ENV] = str(locked)
c = dyd.check_vault()
if os.geteuid() == 0:
    check("unwritable vault -> 3 (skipped: running as root)", True, "root bypasses perms")
else:
    check("unwritable vault -> 3", c.code == ec.DEPENDENCY_FAILED, f"{c.code} {c.detail}")
locked.chmod(0o700)
os.environ[dyd.VAULT_ENV] = str(tmp)

# the write probe must not leave litter behind
check("vault probe cleans up after itself", not (tmp / ".dyak-write-probe").exists())


# --- 6. gateway: configured but unreachable -> 3, no real network in tests ----
def boom_urlopen(url, timeout=None):
    raise dyd.urllib.error.URLError("connection refused")


real_urlopen = dyd.urllib.request.urlopen
dyd.urllib.request.urlopen = boom_urlopen
# This checked `check_gateway()` against `DYAK_GATEWAY_URL`, a variable nothing wrote.
# The endpoint the pipeline actually uses is `DYAK_LLM_ENDPOINT`, and `check_llm` is
# the check that observes it -- so the unreachability assertions moved onto the check that
# is real (#68). Same behaviour under test, no phantom dependency.
os.environ[dyd.LLM_ENV] = "http://127.0.0.1:1/inference"
os.environ[dyd.LLM_MODEL_ENV] = "some/model"
c = dyd.check_llm()
check("unreachable endpoint -> 3", c.code == ec.DEPENDENCY_FAILED, f"{c.code}")
check("unreachable endpoint explains itself", "unreachable" in c.detail, c.detail)
check("unreachable endpoint has a remediation", bool(c.remediation), str(c.remediation))
dyd.urllib.request.urlopen = real_urlopen
os.environ.pop(dyd.LLM_ENV, None)
os.environ.pop(dyd.LLM_MODEL_ENV, None)


# --- 6b. the vision role: unset falls back, configured-and-dead is a failure ---
# #167: `deps.py:561` claimed `check_llm` covered `DYAK_VLM_ENDPOINT`. It never did, so a
# dead vision endpoint produced a green `doctor` and a stage-6 failure after the
# transcription and frame extraction had already run -- #81 measured that stage at ~396k
# input tokens and ~7.7 minutes on a 75-minute lecture. `check_vlm` is the probe, and the
# two states it has to tell apart are "unset" (a state, stage 6 falls back) and "configured
# and unreachable" (a dependency failure, refused before the run starts).
class _ProbeOk:
    status = 200

    def read(self, _n=-1):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def vision_dead_urlopen(request, timeout=None):
    """Refuse the vision URL only, so the other probes stay healthy and the failure is isolated."""
    url = getattr(request, "full_url", str(request))
    if "/vision" in url:
        raise dyd.urllib.error.URLError("connection refused")
    return _ProbeOk()


saved_vlm = {k: os.environ.get(k) for k in (dyd.VLM_ENV, dyd.VLM_MODEL_ENV, dyd.VLM_KEY_ENV)}
for key in saved_vlm:
    os.environ.pop(key, None)
os.environ.pop(dyd.LLM_ENV, None)
os.environ.pop(dyd.LLM_MODEL_ENV, None)

# (a) nothing configured anywhere: a state to report, not a fault.
c = dyd.check_vlm()
check("unset vision endpoint is not a failure (#167)", not c.failed, f"ok={c.ok} fatal={c.fatal}")
check("unset vision endpoint says so (#167)", "not configured" in c.detail, c.detail)

# (b) unset, with a text endpoint to fall back to: stage 6 is served, so still not a fault.
os.environ[dyd.LLM_ENV] = "http://127.0.0.1:1/inference"
c = dyd.check_vlm()
check(
    "unset vision endpoint reports the text fallback (#167)",
    c.ok and dyd.LLM_ENV in c.detail,
    f"ok={c.ok} detail={c.detail}",
)
check(
    "the fallback is not fatal for a run (#167)",
    not c.failed,
    f"ok={c.ok} fatal={c.fatal}",
)

# (c) configured but unreachable: exit 3, fatal, with a remediation -- the whole point.
os.environ[dyd.VLM_ENV] = "http://127.0.0.1:1/vision"
os.environ[dyd.VLM_MODEL_ENV] = "some/vision-model"
dyd.urllib.request.urlopen = vision_dead_urlopen
c = dyd.check_vlm()
check("unreachable vision endpoint -> 3 (#167)", c.code == ec.DEPENDENCY_FAILED, f"{c.code}")
check("unreachable vision endpoint explains itself (#167)", "unreachable" in c.detail, c.detail)
check(
    "unreachable vision endpoint has a remediation (#167)", bool(c.remediation), str(c.remediation)
)
check("a configured vision endpoint is fatal (#167)", c.failed, f"ok={c.ok} fatal={c.fatal}")
check(
    "the unreachable target named is the vision one (#167)",
    "127.0.0.1:1/vision" in c.detail,
    c.detail,
)

# (d) half-configured is a missing dependency, like the text role's equivalent.
os.environ.pop(dyd.VLM_MODEL_ENV, None)
c = dyd.check_vlm()
check(
    "vision endpoint without a model -> 2 (#167)",
    c.code == ec.MISSING_DEPENDENCY and bool(c.remediation),
    f"{c.code} {c.detail}",
)
os.environ[dyd.VLM_MODEL_ENV] = "some/vision-model"

# (e) wired into run_all, so `doctor` prints it and its exit code can move.
names = [c.name for c in dyd.run_all()]
check("run_all carries the vision check, so doctor reports it (#167)", "vlm" in names, str(names))

# (f) `doctor` itself moves on the unreachable case: reported as a failure, not passed.
os.environ[dyd.LLM_MODEL_ENV] = "some/model"
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_doctor = dyc.main(["doctor"])
check(
    "doctor with a dead configured vision endpoint -> exit 3 (#167)",
    rc_doctor == ec.DEPENDENCY_FAILED,
    f"rc={rc_doctor}",
)
check("doctor names the vision role as the failure (#167)", "vlm" in err.getvalue(), err.getvalue())
check(
    "doctor renders the vision check as failed (#167)",
    "[FAIL] vlm" in out.getvalue(),
    out.getvalue(),
)

# (g) `process` refuses before the run when the vision role is configured and dead.
err_buf = io.StringIO()
with redirect_stderr(err_buf):
    rc_vlm = dyc._preflight()
check(
    "a dead configured vision endpoint refuses the run before it starts (#167)",
    rc_vlm == ec.DEPENDENCY_FAILED,
    f"rc={rc_vlm}",
)
check("the refusal names the vision role (#167)", "vlm" in err_buf.getvalue(), err_buf.getvalue())
dyd.urllib.request.urlopen = real_urlopen

# (h) an unconfigured vision endpoint is NOT a refusal: stage 6 degrades, the run proceeds.
for key in (dyd.VLM_ENV, dyd.VLM_MODEL_ENV, dyd.VLM_KEY_ENV, dyd.LLM_ENV, dyd.LLM_MODEL_ENV):
    os.environ.pop(key, None)
err_buf = io.StringIO()
with redirect_stderr(err_buf):
    rc_vlm_unset = dyc._preflight(no_llm=True)
check(
    "an unconfigured vision endpoint does not refuse the run (#167)",
    rc_vlm_unset == ec.OK,
    f"rc={rc_vlm_unset} err={err_buf.getvalue()}",
)
check(
    "the unconfigured-vision note names stage 6, not 0-12 (#167)",
    "stage 6" in err_buf.getvalue() and "0-12" not in err_buf.getvalue(),
    err_buf.getvalue(),
)

for key, value in saved_vlm.items():
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value

# --- 7. a tool with no --version must not report usage text AS a version -----
# shipboard has no --version flag; `shipboard --help` prints argparse usage.
# Labelling that "version" tells the reader nothing and looks like a bug.
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["doctor"])
text = out.getvalue()
# The shipboard check is gone entirely. This inverts what the suite asserted before #44:
# it used to require the name to be absent from *its own check line*, which kept the check
# alive as long as the backend was optional. Now the name must be absent from `doctor`
# output altogether, and the STT line must disclose where the audio goes.
check("shipboard is not a check at all any more", "shipboard" not in text, text)
check(
    "the configured endpoint is what gets probed",
    "127.0.0.1:10302" in text,
    text,
)
# #45: the two remaining backends have opposite trust properties, and the operator
# should not have to read the README to learn which one is in use.
check(
    "a local endpoint says the audio stays on this machine",
    "audio stays on this machine" in text,
    text,
)

# ffmpeg DOES have a real version line, so it must still be reported
check("a real version command is still reported", "ffmpeg version 7.1.1-1" in text, text)
check("ffprobe version reported separately", "ffprobe version 7.1.1-1" in text, text)

# --- 8. unimplemented subcommands point at their issue, not a dead end -------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["audit"])
check("`audit` -> exit 1", rc == ec.USAGE, f"rc={rc}")
check("`audit` names its tracking issue", "#11" in err.getvalue(), err.getvalue())
check("`audit` suggests doctor", "doctor" in err.getvalue(), err.getvalue())

# `process` IS implemented, but only for stages 0-3. With no path it is still a
# usage error, and it must print the form rather than a dead end.
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = dyc.main(["process"])
check("`process` with no path -> exit 1", rc == ec.USAGE, f"rc={rc}")
check(
    "`process` with no path prints the form",
    "dyak process PATH" in err.getvalue(),
    err.getvalue(),
)

# unknown subcommand is a usage error, and must not surface as argparse's own 2,
# which is MISSING_DEPENDENCY in the ADR table
try:
    with redirect_stderr(io.StringIO()):
        rc = dyc.main(["nonsense"])
    check("unknown subcommand -> exit 1, not 2", rc == ec.USAGE, f"rc={rc}")
except SystemExit as exc:
    check("unknown subcommand -> exit 1, not 2", False, f"argparse exited {exc.code} uncaught")

restore()

# --- a redirected log is live, not flushed at exit ----------------------------
# `pipeline` flushes exactly once, at `pipeline.py:281`, immediately before stage 3.
# Every line after that rides on the stream's own buffering, and CPython block-buffers
# `sys.stdout` in 8192-byte chunks whenever fd 1 is not a tty. Measured on run 14: the
# log sat at `[3/12] stt starting` for 16m18s while stages 1-8 were finished on disk
# and the process was in stage 9.
#
# Block-buffered output is discarded when a process dies without flushing, so this
# test kills it. Letting it exit normally cannot tell the two apart -- the flush at exit
# happens either way, which is exactly why the bug survived thirteen runs.
KILL_SCRIPT = (
    "import sys, time\n"
    "from dyak.cli import _line_buffer\n"
    "_line_buffer()\n"
    "sys.stdout.write('[1/12] probe  0.3s\\n')\n"
    "time.sleep(60)\n"
)
size_while_running = -1
with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "run.log"
    child_env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    with log.open("wb") as handle:
        proc = subprocess.Popen([sys.executable, "-c", KILL_SCRIPT], stdout=handle, env=child_env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            size_while_running = log.stat().st_size
            if size_while_running:
                break
            time.sleep(0.05)
        proc.kill()
        proc.wait()
    landed = log.read_text(encoding="utf-8", errors="replace")
check(
    "a redirected log is written before the process exits",
    "[1/12] probe" in landed,
    f"size while running={size_while_running}B, after SIGKILL={len(landed)}B",
)

# --- settings file is TOML: comments, quotes and escapes mean what TOML says ---
# Issue #129: a flat line scanner read `vault = "/x"  # note` as the value
# `/x"  # note`, and wrote paths unescaped. Both halves are pinned here.
settings_file = tmp / "toml-settings.toml"
os.environ[dyd.SETTINGS_PATH_ENV] = str(settings_file)
settings_file.write_text('vault = "/home/u/notes"  # my vault\n', encoding="utf-8")
check(
    "a trailing comment after a quoted value is not part of the value",
    dyd.read_settings().get("vault") == "/home/u/notes",
    repr(dyd.read_settings()),
)

tricky = '/home/u/"quoted" \\ back\ttab/notes'
written = dyd.write_settings(Path(tricky))
check(
    "a path with quotes, backslashes and tabs round-trips through write -> read",
    dyd.read_settings().get("vault") == tricky,
    repr(dyd.read_settings().get("vault")),
)
check("write_settings returns the file it wrote", written == settings_file)

settings_file.write_text('vault = "/ok"\nvault = = broken\n', encoding="utf-8")
err_buf = io.StringIO()
with redirect_stderr(err_buf):
    malformed = dyd.read_settings()
check(
    "malformed settings yield nothing and name the file on stderr",
    malformed == {} and str(settings_file) in err_buf.getvalue(),
    f"read={malformed!r} stderr={err_buf.getvalue()!r}",
)

settings_file.write_text('vault = 42\nother = "x"\n', encoding="utf-8")
check(
    "non-string values are dropped, not stringified",
    dyd.read_settings() == {"other": "x"},
    repr(dyd.read_settings()),
)

# --- #126: preflight must not treat a missing LLM as benign, and must not crash on STT ---
# Isolated: no STT endpoint reachable, no LLM configured, a vault that does not exist.
#
# `DYAK_STT` stays unset so the `auto` path is exercised, but `auto` falls back to the
# *built-in* endpoints in `stt/backends.py` when nothing overrides them -- and a host with
# whisper.cpp on the default 127.0.0.1:10302 makes these checks measure the host instead of
# the code (#175). Both URL variables are honoured on the `auto` path (`_env_endpoint`),
# so they are pointed at a port that is provably dead: bound but never `listen()`ed, which
# the kernel refuses at once, and held for the rest of this block so no other process can
# take it. A fixed port (e.g. :1) is not provable, and a bound-then-closed port leaves a gap
# in which it could be occupied.
_dead = socket.socket()
_dead.bind(("127.0.0.1", 0))
_dead_url = f"http://127.0.0.1:{_dead.getsockname()[1]}"
os.environ.pop("DYAK_STT", None)
os.environ["DYAK_WHISPERCPP_URL"] = _dead_url
os.environ["DYAK_OPENAI_URL"] = _dead_url
os.environ.pop(dyd.LLM_ENV, None)
os.environ[dyd.VAULT_ENV] = str(Path(tempfile.mkdtemp()) / "no-vault")

# (a) with DYAK_STT unset and nothing reachable, run_all must return a Check, not raise.
try:
    stt_checks = dyd.run_all(required=dyd.PROCESS_REQUIRES)
    stt_raised = None
except Exception as exc:
    stt_checks, stt_raised = [], exc
stt_check = next((c for c in stt_checks if c.name == "stt"), None)
check(
    "no STT endpoint reachable -> run_all returns a Check, does not raise (#126)",
    stt_raised is None and stt_check is not None and stt_check.failed,
    f"raised={stt_raised!r}",
)
check(
    "the STT refusal carries exit 2 and a remediation line (#126)",
    stt_check is not None
    and stt_check.code == ec.MISSING_DEPENDENCY
    and bool(stt_check.remediation),
    f"code={getattr(stt_check, 'code', None)!r}",
)

# (b) an unconfigured LLM refuses by default, before any STT or frame cost.
llm_check = next(c for c in dyd.run_all(required=dyd.PROCESS_REQUIRES | {"llm"}) if c.name == "llm")
check(
    "an unconfigured LLM is fatal for process by default (#126)",
    llm_check.failed,
    f"ok={llm_check.ok} fatal={llm_check.fatal}",
)

# (c) --no-llm is the explicit consent: an unconfigured LLM is a note, not a refusal.
saved_run_all = dyc.run_all
dyc.run_all = lambda *, required=None: [
    dyd.Check(name="llm", ok=False, detail="not configured", fatal=False)
]
err_buf = io.StringIO()
with redirect_stderr(err_buf):
    rc_no_llm = dyc._preflight(no_llm=True)
dyc.run_all = saved_run_all
check(
    "--no-llm lets the run proceed to the template skeleton (#126)",
    rc_no_llm == ec.OK,
    f"rc={rc_no_llm}",
)
check(
    "the --no-llm note names the stages that need the endpoint, not 0-12 (#126)",
    "stages 6, 7 and 9" in err_buf.getvalue() and "0-12" not in err_buf.getvalue(),
    err_buf.getvalue(),
)

_dead.close()
_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all doctor checks passed")
