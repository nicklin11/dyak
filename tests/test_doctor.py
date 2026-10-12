#!/usr/bin/env python3
"""Regression: `dyak doctor` honours the ADR-0001 exit-code contract.

Stage 0 exists to pin the error contract before four other stages grow their own
ad-hoc reporting. So the thing under test is mostly the distinction between:

  * not on PATH            -> 2, named, with a remediation line
  * on PATH but failing    -> 3, its stderr reproduced VERBATIM
  * not configured at all  -> not a failure (the gateway is optional config)

PATH and `subprocess.run` are stubbed, and every HTTP probe is answered by a stub: the STT
endpoint by one stub for the whole suite, the text and vision roles by per-block stubs
installed through `urlopen_stub`, which restores the previous one in `finally`. One thing
here is real: the `sys.executable -c` subprocess in the redirected-log test at the bottom,
which `subprocess.Popen` is not stubbed for. No test opens a socket any more (#184), so no
verdict here depends on a live endpoint or on the ambient proxy environment.
"""

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
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
import dyak.stt.openai_compat as dyoc  # noqa: E402
import dyak.stt.whispercpp as dywsc  # noqa: E402

dyd.shutil.which = fake_which
dyd.subprocess.run = fake_run

# The model endpoint must never be probed against the real network in this test, and
# neither must the STT endpoint. Before this pin, `doctor` reached whatever was listening
# on 127.0.0.1:10302, so the suite's verdict depended on container state.
#
# #95 removed the built-in 127.0.0.1:10302 default: an unset URL is now "not configured",
# not a probe of a raw container port. The failure this suite exercises is the *configured
# and down* one, so the endpoint is named explicitly; the fake below intercepts every
# request regardless of the URL.
os.environ.pop(dyd.LLM_ENV, None)
real_stt_backend = os.environ.pop(gld_stt.ENV_BACKEND, None)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
os.environ[dywsc.WHISPERCPP_URL_ENV] = "http://127.0.0.1:10302"
real_stt_urlopen = dywsc.urllib.request.urlopen

stt_reachable = True
#: Every call the STT stub saw, as `(url, timeout)`. #189 pins the probe *budget* from this:
#: no container, no listener and no wall clock are involved, so the check cannot be decided
#: by whether the proxy happened to be warm.
stt_probe_calls: list[tuple[str, float | None]] = []
#: When set, the stub raises this instead of answering. Used to pin that a probe which times
#: out is still a failure carrying its own exit code (#189), not a fabricated success.
stt_probe_error: BaseException | None = None


def fake_stt_urlopen(request, timeout=None):
    stt_probe_calls.append((getattr(request, "full_url", str(request)), timeout))
    if stt_probe_error is not None:
        raise stt_probe_error
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


@contextmanager
def urlopen_stub(stub):
    """Install `stub` as `urlopen` for one block, restoring the previous one (#184).

    `dyd.urllib.request.urlopen` *is* `urllib.request.urlopen` -- the shared stdlib module
    object -- so the assignment is process-global. The name this helper replaces,
    `real_urlopen`, was a lie: it held the STT stub installed above, not the genuine
    `urlopen`, so restoring it left the vision stub answering probes it had no business
    answering, and the genuine `urlopen` came back only at `restore()`. Restoration is in
    `finally`, so a future edit that raises inside the block cannot leave a stub installed.
    """
    previous_urlopen = dyd.urllib.request.urlopen
    dyd.urllib.request.urlopen = stub
    try:
        yield
    finally:
        dyd.urllib.request.urlopen = previous_urlopen


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
# #95: the failure path must name the trap the default used to walk into -- the raw
# container port is not the supported target, the proxy is, and the raw one is reclaimed
# by the idle timer. Without this the operator gets a URL and no reason.
check(
    "the STT remediation names the proxy and the 300 s reclaim (#95)",
    "100.64.0.1:10301" in msg and "300 s" in msg and "whisper-idle-stop" in msg,
    msg,
)
check(
    "present deps still reported as ok",
    "[ok  ] ffmpeg" in out.getvalue() and "[ok  ] ffprobe" in out.getvalue(),
    out.getvalue(),
)

# --- 2b. an unset URL is "not configured", not a probe of a dead port (#95) ---
# #95 removed the built-in 127.0.0.1:10302 default, so this is a third state with its own
# verdict: exit 2, and the message names the missing configuration rather than a refused
# connection to a port nobody started. `stt_reachable` is irrelevant here -- an
# unconfigured backend must fail before it opens a socket.
_saved_stt_url = os.environ.pop(dywsc.WHISPERCPP_URL_ENV, None)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc_unset = dyc.main(["doctor"])
if _saved_stt_url is not None:
    os.environ[dywsc.WHISPERCPP_URL_ENV] = _saved_stt_url
unset_out, unset_err = out.getvalue(), err.getvalue()
check(
    "an unconfigured STT endpoint -> exit 2, not 3 (#95)",
    rc_unset == ec.MISSING_DEPENDENCY,
    f"rc={rc_unset}",
)
check(
    "the unconfigured STT failure says it is not configured (#95)",
    "not configured" in unset_out.lower(),
    unset_out,
)
check(
    "the unconfigured failure is not a refused connection to the dead port (#95)",
    "not usable: the whisper.cpp endpoint is not configured" in unset_out
    and "not reachable" not in unset_err.lower(),
    unset_out + " || " + unset_err,
)

# --- 2c. the STT probe spends the stage-0 budget, not a bare 5 s (#189) -------
# `check()` probes the endpoint before a run commits to it, and the endpoint #95 names as
# supported -- the shipboard proxy on 100.64.0.1:10301 -- is wake-on-first-request: the
# first `GET /health` after `whisper-idle-stop.timer` reclaimed the container is spent
# waiting for the wake. A bare `timeout=5.0` therefore reported a working endpoint as
# unreachable, and `_preflight` refused the run. The budget is now `PROBE_TIMEOUT`, the
# constant `check_llm` and `check_vlm` already spend, so the three probes cannot disagree.
#
# The stub records the kwarg it was called with, so this pins the budget with no container,
# no listener and no wall clock -- and goes red the moment a literal comes back.
stt_probe_calls.clear()
stt_reachable = True
c = dyd.check_stt()
whisper_timeouts = [t for url, t in stt_probe_calls if url.endswith("/health")]
check(
    "the whisper.cpp health probe spends PROBE_TIMEOUT (#189)",
    c.ok and whisper_timeouts == [dyd.PROBE_TIMEOUT],
    f"ok={c.ok} timeouts={whisper_timeouts!r} PROBE_TIMEOUT={dyd.PROBE_TIMEOUT!r}",
)

stt_probe_calls.clear()
_saved_openai_url = os.environ.get(dyoc.OPENAI_URL_ENV)
os.environ[gld_stt.ENV_BACKEND] = "openai"
os.environ[dyoc.OPENAI_URL_ENV] = "http://127.0.0.1:10302"
c = dyd.check_stt()
openai_timeouts = [t for url, t in stt_probe_calls if url.endswith("/v1/models")]
check(
    "the OpenAI-compatible probe spends the same PROBE_TIMEOUT (#189)",
    c.ok and openai_timeouts == [dyd.PROBE_TIMEOUT],
    f"ok={c.ok} timeouts={openai_timeouts!r}",
)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
if _saved_openai_url is None:
    os.environ.pop(dyoc.OPENAI_URL_ENV, None)
else:
    os.environ[dyoc.OPENAI_URL_ENV] = _saved_openai_url

# A probe that times out is still a failure carrying the backend's own code, so raising the
# budget cannot drift into "the endpoint is merely asleep, let the run start" (#189).
stt_probe_error = urllib.error.URLError(socket.timeout("timed out"))
c = dyd.check_stt()
check(
    "a probe timeout is still exit 3, not a fabricated success (#189)",
    c.code == ec.DEPENDENCY_FAILED and "timed out" in c.detail,
    f"code={c.code} detail={c.detail}",
)
stt_probe_error = None

# A non-2xx answer is a verdict, not a licence to proceed (#196). `HTTPError` is raised for
# every non-2xx status and the probe used to `return` on all of them, so a proxy answering
# 502/503 while the container boots read as "reachable": measured against a local server,
# `doctor` printed `[ok  ] stt` and exited 0 for 502, 503 and 500 alike. 404/405 stay
# reachable -- a build with no /health to answer is what that branch was written for -- and
# the sibling OpenAI-compatible probe narrows to the same shape (openai_compat.py:178-190).
for _status in (500, 502, 503):
    stt_probe_error = urllib.error.HTTPError(
        "u", _status, "err", {}, io.BytesIO(b'{"error":"backend not ready"}')
    )
    c = dyd.check_stt()
    check(
        f"an HTTP {_status} from the health endpoint is exit 3, not 'reachable' (#196)",
        c.code == ec.DEPENDENCY_FAILED and f"HTTP {_status}" in c.detail,
        f"code={c.code} detail={c.detail}",
    )
stt_probe_error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
c = dyd.check_stt()
check(
    "a 404 on /health is still reachable -- that build has no /health (#196)",
    c.ok and c.code == ec.OK,
    f"ok={c.ok} code={c.code} detail={c.detail}",
)
os.environ.pop(gld_stt.ENV_BACKEND, None)
stt_probe_error = urllib.error.HTTPError(
    "u", 502, "Bad Gateway", {}, io.BytesIO(b'{"error":"backend not ready"}')
)
c = dyd.check_stt()
check(
    "a 502 is exit 2 on the auto path, where a refused probe already is (#196)",
    c.code == ec.MISSING_DEPENDENCY,
    f"code={c.code} detail={c.detail}",
)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
stt_probe_error = None

# The refused case is unchanged: 3 on the explicit backend, 2 on the `auto` path a default
# install takes. Both are refusals; only the code differs, and this fix must not move either.
stt_reachable = False
c = dyd.check_stt()
check(
    "a refused probe is still exit 3 on an explicit backend (#189)",
    c.code == ec.DEPENDENCY_FAILED,
    f"code={c.code} detail={c.detail}",
)
os.environ.pop(gld_stt.ENV_BACKEND, None)
c = dyd.check_stt()
check(
    "a refused probe is still exit 2 on the auto path (#189)",
    c.code == ec.MISSING_DEPENDENCY,
    f"code={c.code} detail={c.detail}",
)
os.environ[gld_stt.ENV_BACKEND] = "whispercpp"
stt_reachable = True

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


# This checked `check_gateway()` against `DYAK_GATEWAY_URL`, a variable nothing wrote.
# The endpoint the pipeline actually uses is `DYAK_LLM_ENDPOINT`, and `check_llm` is
# the check that observes it -- so the unreachability assertions moved onto the check that
# is real (#68). Same behaviour under test, no phantom dependency.
with urlopen_stub(boom_urlopen):
    os.environ[dyd.LLM_ENV] = "http://127.0.0.1:1/inference"
    os.environ[dyd.LLM_MODEL_ENV] = "some/model"
    c = dyd.check_llm()
    check("unreachable endpoint -> 3", c.code == ec.DEPENDENCY_FAILED, f"{c.code}")
    check("unreachable endpoint explains itself", "unreachable" in c.detail, c.detail)
    check("unreachable endpoint has a remediation", bool(c.remediation), str(c.remediation))
os.environ.pop(dyd.LLM_ENV, None)
os.environ.pop(dyd.LLM_MODEL_ENV, None)


# --- 6b. the vision role: unset falls back, configured-and-dead is a failure ---
# #167: `deps.py:501` claimed `check_llm` covered `DYAK_VLM_ENDPOINT`. It never did, so a
# dead vision endpoint produced a green `doctor` and a stage-6 failure after the
# transcription and frame extraction had already run -- #81 measured that stage at ~396k
# input tokens and ~7.7 minutes on a 75-minute lecture. `check_vlm` is the probe, and the
# two states it has to tell apart are "unset" (a state, stage 6 falls back) and "configured
# and unreachable" (a dependency failure, refused before the run starts).


def vision_dead_urlopen(request, timeout=None):
    """Make the vision role dead -- and refuse every other probe instead of answering it.

    This used to return a fabricated 200 for any URL without `/vision` (a `_ProbeOk`), so the
    STT and LLM probes that run inside this block were decided by the *vision* stub: a check
    added there would be told a lie (#184). The stub now answers nothing it does not own, so
    no fabricated verdict exists inside its block -- and `urlopen_stub` scopes the install, so
    none exists outside it either.
    """
    raise dyd.urllib.error.URLError("connection refused")


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
with urlopen_stub(vision_dead_urlopen):
    os.environ[dyd.VLM_ENV] = "http://127.0.0.1:1/vision"
    os.environ[dyd.VLM_MODEL_ENV] = "some/vision-model"
    c = dyd.check_vlm()
    check(
        "unreachable vision endpoint -> 3 (#167)",
        c.code == ec.DEPENDENCY_FAILED,
        f"{c.code}",
    )
    check(
        "unreachable vision endpoint explains itself (#167)",
        "unreachable" in c.detail,
        c.detail,
    )
    check(
        "unreachable vision endpoint has a remediation (#167)",
        bool(c.remediation),
        str(c.remediation),
    )
    check(
        "a configured vision endpoint is fatal (#167)",
        c.failed,
        f"ok={c.ok} fatal={c.fatal}",
    )
    check(
        "the unreachable target named is the vision one (#167)",
        "127.0.0.1:1/vision" in c.detail,
        c.detail,
    )

    # The stub must not fabricate an answer for a probe it does not own. It used to return a
    # 200 for any URL without `/vision`, so the STT and LLM probes that run inside this block
    # were decided by the *vision* stub (#184); this goes red if that fabrication comes back.
    try:
        dyd.urllib.request.urlopen(dyd.urllib.request.Request("http://127.0.0.1:10302/health"))
        fabricated = True
    except urllib.error.URLError:
        fabricated = False
    check(
        "the vision stub refuses a non-vision probe instead of fabricating a 200 (#184)",
        not fabricated,
        f"fabricated={fabricated}",
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
    check(
        "run_all carries the vision check, so doctor reports it (#167)",
        "vlm" in names,
        str(names),
    )

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
    check(
        "doctor names the vision role as the failure (#167)",
        "vlm" in err.getvalue(),
        err.getvalue(),
    )
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
    check(
        "the refusal names the vision role (#167)",
        "vlm" in err_buf.getvalue(),
        err_buf.getvalue(),
    )

# The stub is gone with its block: the next probe must not be answered by it (#184).
check(
    "the vision stub is out of scope once its block ends (#184)",
    dyd.urllib.request.urlopen is fake_stt_urlopen,
    f"urlopen={dyd.urllib.request.urlopen!r}",
)

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
# `DYAK_STT` stays unset so the `auto` path is exercised. Before #95 `auto` fell back to a
# *built-in* `127.0.0.1:10302`, so a host with whisper.cpp there made these checks measure
# the host instead of the code (#175). There is no built-in default any more, and the case
# this block pins is still "configured and unreachable" -- a different state from "not
# configured" -- so both URLs stay set. The refusal is produced by the STT stub, not by a
# socket: the block used to point both URLs at a port it bound but never `listen()`ed, which
# left the verdict depending on the host after all -- with `http_proxy` exported and a proxy
# answering 502 the connection succeeded, and the refusal carried `code=0`. No socket is
# opened here now, so a listener on the port, ECONNREFUSED timing and the ambient proxy
# environment cannot decide this block (#184).
os.environ.pop("DYAK_STT", None)
os.environ["DYAK_WHISPERCPP_URL"] = "http://127.0.0.1:10302"
os.environ["DYAK_OPENAI_URL"] = "http://127.0.0.1:10302"
os.environ.pop(dyd.LLM_ENV, None)
os.environ[dyd.VAULT_ENV] = str(Path(tempfile.mkdtemp()) / "no-vault")

stt_reachable = False
with urlopen_stub(fake_stt_urlopen):
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
    check(
        # The `auto` path reaches this failure too, and it is the path a default install
        # takes, so the proxy/300 s trap must be named here and not only on the
        # explicit-backend path.
        "the auto STT refusal names the proxy and the 300 s reclaim (#95)",
        stt_check is not None
        and "100.64.0.1:10301" in (stt_check.remediation or "")
        and "300 s" in (stt_check.remediation or ""),
        getattr(stt_check, "remediation", ""),
    )

    # The verdict must come from the stub, not from whatever the host has listening or
    # proxying: this is the check that goes red on the tree where the block opened a socket.
    stt_probe_calls.clear()
    dyd.check_stt()
    check(
        "the STT refusal is decided by the stub, not by a socket on the host (#184)",
        bool(stt_probe_calls),
        f"calls={stt_probe_calls!r}",
    )

    # And it must reach the exit code through `_preflight`, the function that returns it.
    saved_run_all_stt = dyc.run_all
    dyc.run_all = lambda *, required=None: [stt_check]
    err_buf_stt = io.StringIO()
    with redirect_stderr(err_buf_stt):
        rc_stt = dyc._preflight()
    dyc.run_all = saved_run_all_stt
    check(
        "a refused STT makes preflight return exit 2, so `process` refuses (#126)",
        rc_stt == ec.MISSING_DEPENDENCY,
        f"rc={rc_stt}",
    )
    check(
        "the STT refusal is printed, not silent (#126)",
        "refusing to start" in err_buf_stt.getvalue() and "stt" in err_buf_stt.getvalue(),
        err_buf_stt.getvalue(),
    )

    # (b) an unconfigured LLM refuses by default, before any STT or frame cost.
    llm_check = next(
        c for c in dyd.run_all(required=dyd.PROCESS_REQUIRES | {"llm"}) if c.name == "llm"
    )
    check(
        "an unconfigured LLM is fatal for process by default (#126)",
        llm_check.failed,
        f"ok={llm_check.ok} fatal={llm_check.fatal}",
    )
    # #172: `failed` alone was not enough, and that is exactly why this survived. `run_all`
    # promotes the check to fatal but never touches `code`, so an unconfigured endpoint
    # produced a refusal that `worst_code` scored as OK -- `_preflight` printed "refusing to
    # start" and returned 0, and `process` went on to run stages 1-6 with no text endpoint.
    # The exit code is the entire point of the gate, so assert it, and assert it through
    # `_preflight` (the function that returns it) rather than on the Check alone.
    check(
        "the promoted LLM refusal carries exit 2, not the OK default (#172)",
        llm_check.code == ec.MISSING_DEPENDENCY,
        f"failed={llm_check.failed} code={llm_check.code!r}",
    )
    saved_run_all_172 = dyc.run_all
    dyc.run_all = lambda *, required=None: [llm_check]
    err_buf_172 = io.StringIO()
    with redirect_stderr(err_buf_172):
        rc_llm_default = dyc._preflight(no_llm=False)
    dyc.run_all = saved_run_all_172
    check(
        "an unconfigured LLM makes preflight return exit 2, so `process` refuses (#172)",
        rc_llm_default == ec.MISSING_DEPENDENCY,
        f"rc={rc_llm_default}",
    )
    check(
        "the exit-2 refusal is printed, not silent (#172)",
        "refusing to start" in err_buf_172.getvalue(),
        err_buf_172.getvalue(),
    )
stt_reachable = True

# (c) --no-llm is the explicit consent: an unconfigured LLM is a note, not a refusal.
# The *real* check, not a stand-in: its `code` is now MISSING_DEPENDENCY (#172), so this
# also proves the new code cannot leak into the exit code on the deliberate path -- only
# `failed` (which needs `fatal`) may move it.
saved_run_all = dyc.run_all
dyc.run_all = lambda *, required=None: [dyd.check_llm()]
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

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all doctor checks passed")
