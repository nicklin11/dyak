"""Dependency detection for `dyak doctor` (ADR-0001 stage 0).

Two failure modes, and keeping them apart is load-bearing:

  * not on PATH              -> MISSING_DEPENDENCY (2), name + remediation line
  * on PATH but errors out   -> DEPENDENCY_FAILED (3), its stderr VERBATIM

Collapsing them into one "not working" state would lose exactly the
information a user needs: whether to install something or fix something.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import exitcodes as ec
from . import runner, stt
from .llm import VISION_ENDPOINT_ENV as VLM_ENV
from .llm import VISION_KEY_ENV as VLM_KEY_ENV
from .llm import VISION_MODEL_ENV as VLM_MODEL_ENV

VAULT_ENV = "DYAK_VAULT"
LLM_ENV = "DYAK_LLM_ENDPOINT"
LLM_MODEL_ENV = "DYAK_LLM_MODEL"
LLM_KEY_ENV = "DYAK_LLM_KEY"
#: The vision role's names are imported rather than retyped: ADR-0004 D3 keeps the two roles
#: separate, and a second literal here could drift from the one `llm.py` actually reads.
#: `check_vlm` and `vlm_configured` are the readers (#167).
DEFAULT_VAULT = Path.home() / "Documents/obs_notes"
PROBE_TIMEOUT = 10.0

SETTINGS_PATH_ENV = "DYAK_SETTINGS"
SETTINGS_KEY = "vault"
#: Written by `write_settings`, read by `resolve_vault`. One key, one file.
SETTINGS_TEMPLATE = "# dyak settings. Written on first run; edit by hand.\n"

#: The settings keys the tool honours. ONE place, and it is a claim about the code: the key a
#: reader consults and the key named here must be the same, or `read_settings` warns about a
#: key the tool actually uses (or stays silent about one it does not). `tests/test_deps.py`
#: derives the keys the source reads and fails if the two sets drift (#156).
#: `vault` is the only key today; #52 grows this set rather than replacing it.
HONOURED_SETTINGS: frozenset[str] = frozenset({SETTINGS_KEY})


def settings_path() -> Path:
    """The settings file: `$DYAK_SETTINGS` if set, else `$XDG_CONFIG_HOME/dyak/settings.toml`.

    The env var names the *file*, not a directory, which is what the name says and what
    testing needs. Issue #52 wants a real config file with a real precedence resolver; this
    is the file that will hold it, carrying exactly one key until then, so #52 extends
    something rather than replacing a default someone is already relying on.
    """
    configured = os.environ.get(SETTINGS_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    base = Path(
        os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config"
    ).expanduser()
    return base / "dyak" / "settings.toml"


def _parse_settings(path: Path) -> dict[str, object] | None:
    """The settings file parsed as TOML; `None` when it is unreadable or malformed.

    Never raises, and reports both unreadable states to stderr itself with the path: a typo
    in a config file must not take the pipeline down at stage 0, and must not be read as a
    garbage value either. `None` is deliberately distinct from `{}` so `check_settings` can
    tell "the file is empty" from "the file could not be parsed" -- the message has already
    gone to stderr, but `doctor` should not report a file it could not read as empty.

    The parse error is reported once per file per process. `read_settings` runs several times
    in one invocation -- stage 0's `check_vault`, `process`'s export and stage 11's terms
    lookup all resolve the vault -- and the same error repeated three times reads as three
    problems. A CLI invocation cannot edit the file mid-run, so a process-lifetime memo is
    the same lifetime as the fact.
    """
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _report_once(path, f"cannot read settings {path}: {exc}")
        return None
    except tomllib.TOMLDecodeError as exc:
        _report_once(path, f"ignoring malformed settings {path}: {exc}")
        return None


#: Settings files whose parse failure has already been reported in this process (#156).
_REPORTED_SETTINGS: set[str] = set()


def _report_once(path: Path, message: str) -> None:
    """Print `dyak: {message}` on stderr the first time this file's failure is seen."""
    key = str(path)
    if key in _REPORTED_SETTINGS:
        return
    _REPORTED_SETTINGS.add(key)
    print(f"dyak: {message}", file=sys.stderr)


def _toml_type(value: object) -> str:
    """The TOML type of a parsed value, named for a diagnostic (#156)."""
    # bool before int: `isinstance(True, int)` is True, and "a boolean" is the useful word.
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "a number"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return f"a {type(value).__name__}"


#: How close a key must be to an honoured one to be reported as a likely typo rather than as
#: plain inert. 0.6 is what catches `vault_path` (0.67) without matching unrelated keys.
_TYPO_CUTOFF = 0.6


def settings_warnings(path: Path, data: dict[str, object]) -> list[str]:
    """One line per key in `data` that `read_settings` will not honour (#156).

    Three silent behaviours, all of them the same defect -- a hand-edited file with no
    feedback loop:

      * a key no setting reads (`stt = "whispercpp"` is inert and said nothing);
      * a value dropped for not being a string (`stt = 1` vanished, not even ignored aloud);
      * a typo'd key, which is indistinguishable from "not configured" (`vault_path` fell
        back to the default vault, and the only evidence was a path in the output).

    Pure: `report_settings` prints these, so the acceptance criterion "a correct file
    produces no warning" is a property of this function rather than of whichever caller
    happens to run it. A key whose value is not a string is reported for its type first:
    that is the more specific defect, and one line per key is enough.
    """
    lines: list[str] = []
    known = ", ".join(sorted(HONOURED_SETTINGS))
    for key, value in data.items():
        if not isinstance(value, str):
            lines.append(f"settings: {path}: '{key}' is {_toml_type(value)}, not a string; ignored")
            continue
        if key in HONOURED_SETTINGS:
            continue
        near = difflib.get_close_matches(key, HONOURED_SETTINGS, n=1, cutoff=_TYPO_CUTOFF)
        if near:
            lines.append(f"settings: {path}: '{key}' is not a setting; did you mean '{near[0]}'?")
        else:
            lines.append(
                f"settings: {path}: '{key}' is not a setting; nothing reads it "
                f"(known keys: {known})"
            )
    return lines


def report_settings() -> None:
    """Print what the settings file holds that will not be honoured. Silent when it is clean.

    Called once per invocation from `cli.main`, not from `read_settings`: `resolve_vault` is
    consulted by stage 0, by `process` and by stage 11's terms lookup, so a warning emitted
    by the reader would repeat three times in one run. One warning per key per run is
    feedback; three is noise. Unreadable and malformed files are reported by
    `_parse_settings`, which `read_settings` also calls, so this adds nothing for them.
    """
    path = settings_path()
    data = _parse_settings(path)
    if data is None:
        return
    for line in settings_warnings(path, data):
        print(f"dyak: {line}", file=sys.stderr)


def read_settings() -> dict[str, str]:
    """Top-level `key = "value"` string pairs from the settings file. Never raises.

    The file is TOML, parsed with stdlib `tomllib` (the >=3.11 floor has it), so trailing
    comments, quoting and escapes mean what any TOML parser says. A file that does not parse
    is reported once to stderr with its path and yields nothing: a typo in a config file must
    not take the pipeline down at stage 0, and must not be read as a garbage value either.
    Non-string values are dropped rather than stringified.

    Keys no setting reads, values dropped for not being a string and likely typos are named
    on stderr by `report_settings` (#156). They are not reported here: this function is the
    reader, it runs several times per invocation, and the report belongs to the command, not
    to each read.
    """
    return {
        key: value
        for key, value in (_parse_settings(settings_path()) or {}).items()
        if isinstance(value, str)
    }


def _toml_string(value: str) -> str:
    """A TOML basic string for `value`, with quotes, backslashes and control characters escaped."""
    out: list[str] = []
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def write_settings(vault: Path) -> Path:
    """Persist the vault, creating the file on first use. Returns the path written.

    Writing is the whole point of the first run: without it, "settable at setup" means the
    user has to guess that `DYAK_VAULT` exists, and a default nobody can discover is a
    default nobody will change. The value is escaped so any path round-trips through
    `read_settings` unchanged.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{SETTINGS_TEMPLATE}{SETTINGS_KEY} = {_toml_string(str(vault))}\n", encoding="utf-8"
    )
    return path


@dataclass
class Check:
    """One dependency's verdict. `code` is the exit code if this check fails."""

    name: str
    ok: bool
    detail: str
    path: str | None = None
    version: str | None = None
    remediation: str | None = None
    stderr: str | None = None
    code: int = ec.OK
    fatal: bool = True

    @property
    def failed(self) -> bool:
        return not self.ok and self.fatal


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)


def check_executable(
    name: str,
    probe: list[str],
    remediation: str,
    report: str = "version",
) -> Check:
    """Locate `name` on PATH, then prove it actually runs.

    `probe` is invoked only after PATH resolution succeeds, so a broken install
    is distinguishable from an absent one.

    `report="version"` shows the probe's first output line, which is only
    truthful when the probe IS a version command. For a tool with no --version
    flag, pass report="ok": printing argparse usage under a "version" heading
    tells the reader nothing and misleads them into thinking it is one.
    """
    exe = shutil.which(name)
    if exe is None:
        return Check(
            name=name,
            ok=False,
            detail="not found on PATH",
            remediation=remediation,
            code=ec.MISSING_DEPENDENCY,
        )

    try:
        proc = _run([exe, *probe])
    except subprocess.TimeoutExpired:
        return Check(
            name=name,
            ok=False,
            detail=f"timed out after {PROBE_TIMEOUT:.0f}s running `{' '.join(probe)}`",
            path=exe,
            remediation=f"the binary hangs on `{' '.join(probe)}`; check the install",
            code=ec.DEPENDENCY_FAILED,
        )
    except OSError as exc:
        return Check(
            name=name,
            ok=False,
            detail=f"could not execute: {exc}",
            path=exe,
            remediation=f"check permissions on {exe}",
            code=ec.DEPENDENCY_FAILED,
        )

    if proc.returncode != 0:
        # Verbatim: no paraphrase, no truncation beyond what the dependency said.
        return Check(
            name=name,
            ok=False,
            detail=f"`{' '.join(probe)}` exited {proc.returncode}",
            path=exe,
            stderr=proc.stderr.strip(),
            code=ec.DEPENDENCY_FAILED,
        )

    if report != "version":
        return Check(name=name, ok=True, detail="ok", path=exe, version="ok")

    raw = proc.stdout.strip()
    first = raw.splitlines()[0].strip() if raw else ""
    return Check(
        name=name,
        ok=True,
        detail="ok",
        path=exe,
        version=first or "ok",
    )


def check_ffmpeg() -> Check:
    return check_executable("ffmpeg", ["-version"], "install: pacman -S ffmpeg")


def check_ffprobe() -> Check:
    return check_executable("ffprobe", ["-version"], "install: pacman -S ffmpeg")


def check_stt() -> Check:
    """ADR-0001 D2 (amended): STT is delegated to an *endpoint*, not to a utility.

    Asking "is some binary on PATH" cannot tell "no STT configured" from "STT configured and
    its backend is down" -- and `process` used to discover the second only in stage 3, after
    6 s of audio extraction. So the configured backend is asked instead, and the backend's own
    health check is the probe. `doctor` never spends a lecture-length request on this:
    `check()` is a loopback GET, not a transcription.

    It also reports **where the audio is going**. The two remaining backends have opposite
    trust properties -- a local whisper.cpp keeps the lecture on this machine, an
    OpenAI-compatible endpoint does not -- and an operator should not have to read the README
    to learn that their recording left the host.
    """
    # `resolve` raises when nothing answers (#126). That is this check's verdict, not a crash.
    try:
        engine = stt.backend()
    except runner.DependencyError as exc:
        return Check(
            name="stt",
            ok=False,
            code=exc.code,
            detail=exc.message,
            remediation=exc.remediation,
        )
    info = engine.info()
    try:
        engine.check()
    except runner.DependencyError as exc:
        return Check(
            name="stt",
            ok=False,
            code=exc.code,
            detail=f"{info.name} at {info.target} is not usable: {exc.message}",
            remediation=exc.remediation,
        )
    detail = f"{info.name} at {info.target} reachable — {info.detail}"
    if info.name in ("openai", "openai-compatible"):
        detail += "; NOTE: audio and transcripts leave this machine"
    else:
        detail += "; audio stays on this machine"
    for note in info.notes:
        detail += f"; {note}"
    return Check(name="stt", ok=True, detail=detail)


def resolve_vault(explicit: str | Path | None = None) -> Path:
    """The vault ADR-0001 D9 names, from the most specific source available.

    Precedence: explicit argument, then `$DYAK_VAULT`, then the persisted `vault` key in
    the settings file, then the built-in default directory.

    The result is a path, not a judgement about it: a vault that does not exist is still what
    the user named, and whether that is fatal is `check_vault`'s question, not this one's.

    This exists because the default lived only inside `check_vault`, so `dyak doctor`
    reported a vault that `dyak process` never consulted. Measured on lecture 1,
    2026-10-04: doctor named `~/Documents/obs_notes`, the process ran with `vault_path=None`
    throughout, and stage 11 resolved no terms directory -- while `~/Documents/obs_notes/
    mcs/_terms` existed with 34 term notes. Stage 12 had the same gap.

    Two code paths, one documented default, and they disagreed.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(VAULT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    stored = read_settings().get(SETTINGS_KEY, "").strip()
    if stored:
        return Path(stored).expanduser()
    return DEFAULT_VAULT


def check_vault() -> Check:
    """ADR-0001 D9: artefacts live in the vault, never in this repository."""
    vault = resolve_vault()
    if not vault.exists():
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} does not exist",
            remediation=f"create it, or point {VAULT_ENV} at the right path",
            code=ec.MISSING_DEPENDENCY,
        )
    if not vault.is_dir():
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} is not a directory",
            remediation=f"point {VAULT_ENV} at the vault directory",
            code=ec.MISSING_DEPENDENCY,
        )
    probe = vault / ".dyak-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            name="vault",
            ok=False,
            detail=f"{vault} is not writable: {exc}",
            path=str(vault),
            remediation="fix ownership or permissions on the vault",
            code=ec.DEPENDENCY_FAILED,
        )
    return Check(
        name="vault",
        ok=True,
        detail="writable",
        path=str(vault),
        version="ok",
    )


def check_settings() -> Check:
    """The settings file as `doctor` reports it: where it is, and what it did with its keys.

    #156: `read_settings` had no feedback loop, so a key that no setting reads was
    indistinguishable from no config at all, and a typo in the one key that does work was
    indistinguishable from "not configured". This is the always-on half of the report --
    `doctor` prints it whether or not anything is wrong -- while `report_settings` warns only
    when there is something to warn about.

    Non-fatal and always `ok`. An unhonoured key is a fact to report, not a missing
    dependency, and a warning must not move an exit code (ADR-0001 D4): `worst_code` reads
    `Check.failed`, which needs `not ok and fatal`.
    """
    path = settings_path()
    if not path.is_file():
        return Check(name="settings", ok=True, fatal=False, detail="absent", path=str(path))
    data = _parse_settings(path)
    if data is None:
        return Check(
            name="settings",
            ok=True,
            fatal=False,
            detail="present, but unreadable or malformed (reported on stderr)",
            path=str(path),
        )
    honoured = sorted(k for k, v in data.items() if isinstance(v, str) and k in HONOURED_SETTINGS)
    ignored = sorted(k for k in data if k not in honoured)
    detail = f"present — honoured: {', '.join(honoured) or 'none'}"
    if ignored:
        detail += f"; ignored: {', '.join(ignored)}"
    return Check(name="settings", ok=True, fatal=False, detail=detail, path=str(path))


def check_numpy() -> Check:
    """Stage 5's array backend. A declared dependency, so absence is a packaging fault."""
    try:
        import numpy
    except ImportError as exc:
        return Check(
            name="numpy",
            ok=False,
            detail=f"not importable: {exc}",
            remediation="install: pip install 'numpy>=1.26'",
            code=ec.MISSING_DEPENDENCY,
        )
    return Check(name="numpy", ok=True, detail="importable", version=numpy.__version__)


def check_llm() -> Check:
    """The synthesis/audit model endpoint. Unconfigured is a state, not a fault.

    Stages 7, 9's critic tier and 10 need this, and stage 6 uses it as the vision fallback.
    `process` treats an unconfigured or unreachable endpoint as fatal unless `--no-llm` is
    given (#126), so a run never writes a template note by accident.
    """
    url = os.environ.get(LLM_ENV, "").strip()
    if not url:
        return Check(
            name="llm",
            ok=False,
            detail=f"not configured (set {LLM_ENV} to enable synthesis, audit and repair)",
            version="skipped",
            fatal=False,
        )
    model = os.environ.get(LLM_MODEL_ENV, "").strip()
    if not model:
        return Check(
            name="llm",
            ok=False,
            detail=f"{LLM_ENV} is set but {LLM_MODEL_ENV} is not",
            remediation=f"set {LLM_MODEL_ENV} to the model id the endpoint serves",
            code=ec.MISSING_DEPENDENCY,
        )
    try:
        request = urllib.request.Request(
            f"{url.rstrip('/')}/models", headers={"Accept": "application/json"}
        )
        if api_key := os.environ.get(LLM_KEY_ENV):
            request.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        return Check(
            name="llm",
            ok=False,
            detail=f"{url}/models returned HTTP {exc.code}",
            remediation="check the endpoint path and the key",
            code=ec.DEPENDENCY_FAILED,
        )
    except (urllib.error.URLError, OSError) as exc:
        return Check(
            name="llm",
            ok=False,
            detail=f"{url} unreachable: {exc}",
            remediation="check the endpoint is running and reachable from this host",
            code=ec.DEPENDENCY_FAILED,
        )
    return Check(name="llm", ok=True, detail="reachable", path=url, version=f"{model}, HTTP {code}")


def vlm_configured() -> bool:
    """True when the vision role has at least one of its own names set (ADR-0004 D3).

    `_preflight` uses this to decide whether a broken vision endpoint should refuse the run.
    "Unset" is a different question and deliberately not fatal: stage 6 falls back to the
    text pair, and the pipeline is required to run end to end with no model configured. A
    half-set pair (`DYAK_VLM_MODEL` without `DYAK_VLM_ENDPOINT`, or the reverse) counts as
    configured, because that is the state `Config.vision_from_env` refuses to resolve.
    """
    return bool(os.environ.get(VLM_ENV, "").strip() or os.environ.get(VLM_MODEL_ENV, "").strip())


def check_vlm() -> Check:
    """The vision endpoint stage 6 captions with. The same shape as `check_llm` (#167).

    Two states, and the difference between them is the whole check:

      * **Unset** is a state, not a fault. `Config.vision_from_env` falls back to the text
        pair when both vision names are unset, so the role is served -- reported, but not a
        failure. With no fallback either, stage 6 marks every frame `NO_ENDPOINT` and the
        pipeline still writes a note; the text endpoint's own check covers that case.
      * **Configured and unreachable** is a dependency failure. Stage 6 needs this role, and
        without this check the run learns the endpoint is dead there -- after the
        transcription and frame extraction, which #81 measured as ~396k input tokens and
        ~7.7 minutes on a 75-minute lecture. That is the most expensive possible moment to
        discover a stage-0 fact, so the check is fatal (`DEPENDENCY_FAILED`, like the text
        role) and `_preflight` refuses the run before it starts.
    """
    url = os.environ.get(VLM_ENV, "").strip()
    model = os.environ.get(VLM_MODEL_ENV, "").strip()
    if not url and not model:
        fallback = os.environ.get(LLM_ENV, "").strip()
        if fallback:
            return Check(
                name="vlm",
                ok=True,
                fatal=False,
                detail=f"not set — stage 6 falls back to {LLM_ENV} ({fallback})",
                path=fallback,
                version="fallback",
            )
        return Check(
            name="vlm",
            ok=False,
            fatal=False,
            detail=(f"not configured (set {VLM_ENV} to caption frames; {LLM_ENV} is the fallback)"),
            version="skipped",
            # Not fatal here, so this code is never read today -- but `run_all` promotes a
            # required check to fatal without giving it a code, and a fatal check whose code
            # is OK makes `_preflight` print a refusal and then return 0 (#172). Carrying the
            # code makes that impossible if `vlm` is ever promoted unconditionally.
            code=ec.MISSING_DEPENDENCY,
        )
    if not url:
        return Check(
            name="vlm",
            ok=False,
            detail=f"{VLM_MODEL_ENV} is set but {VLM_ENV} is not",
            remediation=f"set {VLM_ENV} to the vision endpoint, or unset {VLM_MODEL_ENV}",
            code=ec.MISSING_DEPENDENCY,
        )
    if not model:
        return Check(
            name="vlm",
            ok=False,
            detail=f"{VLM_ENV} is set but {VLM_MODEL_ENV} is not",
            remediation=f"set {VLM_MODEL_ENV} to the model id the endpoint serves",
            code=ec.MISSING_DEPENDENCY,
        )
    # The key falls back the same way `Config.vision_from_env` does: a vision-specific key
    # wins, and an unset one inherits the text role's.
    key = os.environ.get(VLM_KEY_ENV, "").strip() or os.environ.get(LLM_KEY_ENV, "").strip()
    try:
        request = urllib.request.Request(
            f"{url.rstrip('/')}/models", headers={"Accept": "application/json"}
        )
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        return Check(
            name="vlm",
            ok=False,
            detail=f"{url}/models returned HTTP {exc.code}",
            remediation="check the endpoint path and the key",
            code=ec.DEPENDENCY_FAILED,
        )
    except (urllib.error.URLError, OSError) as exc:
        return Check(
            name="vlm",
            ok=False,
            detail=f"{url} unreachable: {exc}",
            remediation="check the endpoint is running and reachable from this host",
            code=ec.DEPENDENCY_FAILED,
        )
    auth = "key set" if key else "no key"
    return Check(
        name="vlm",
        ok=True,
        detail="reachable",
        path=url,
        version=f"{model}, HTTP {code}, {auth}",
    )


def run_all(*, required: frozenset[str] | None = None) -> list[Check]:
    """Every check, with `required` names promoted to fatal.

    `required` exists because "the dependency is broken" and "this run needs
    that dependency" are different questions. `dyak process` stages 0-4 use
    ffmpeg, ffprobe and an STT endpoint, and write nothing outside the managed work
    dir, so refusing to transcribe because the *vault* is missing -- a directory
    stage 12 will need -- is the wrong answer. Without this the preflight gate is
    broader than the run it guards.
    """
    # There is no `check_gateway`. It read `DYAK_GATEWAY_URL`, a name nothing in
    # the codebase ever wrote and nothing in the docs ever told a user to set, and it
    # reported the *model* gateway under the name `gateway` while the endpoint the
    # pipeline actually uses is `DYAK_LLM_ENDPOINT` / `DYAK_VLM_ENDPOINT` --
    # checked by `check_llm` and `check_vlm` (#167). So a fully working run still printed
    #
    #     dyak: note: gateway is unavailable, but stages 0-12 do not use it
    #
    # which was false in both halves: the gateway was up, and stage 6 uses it (#68).
    checks = [
        check_ffmpeg(),
        check_ffprobe(),
        check_numpy(),
        check_stt(),
        check_llm(),
        check_vlm(),
        check_vault(),
        check_settings(),
    ]
    if required is None:
        return checks
    wanted = frozenset(required)
    for check in checks:
        if check.name in wanted:
            check.fatal = True
        elif not check.ok:
            # Reported, but not a reason to refuse: print it with the rest so a
            # degraded environment is visible without being fatal.
            check.fatal = False
    return checks


# What `dyak process` stages 0-3 actually invoke.
# No STT binary is required: stage 3 reaches whichever endpoint is configured, and the
# check above probes that endpoint rather than looking for a program on PATH.
PROCESS_REQUIRES = frozenset({"ffmpeg", "ffprobe", "numpy", "stt"})


# Precedence when several checks fail at once. MISSING wins over FAILED: a
# missing binary is the prerequisite, installing it may clear the others, and
# it is the one state with a definite remediation. Every failed check is still
# reported in full, so choosing an exit code loses no information either way.
_CODE_PRECEDENCE = (ec.MISSING_DEPENDENCY, ec.DEPENDENCY_FAILED)


def worst_code(checks: list[Check]) -> int:
    """The most actionable exit code across failed checks; OK if none failed."""
    codes = {c.code for c in checks if c.failed}
    for code in _CODE_PRECEDENCE:
        if code in codes:
            return code
    return ec.OK
