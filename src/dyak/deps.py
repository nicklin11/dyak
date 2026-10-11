"""Dependency detection for `dyak doctor` (ADR-0001 stage 0).

Two failure modes, and keeping them apart is load-bearing:

  * not on PATH              -> MISSING_DEPENDENCY (2), name + remediation line
  * on PATH but errors out   -> DEPENDENCY_FAILED (3), its stderr VERBATIM

Collapsing them into one "not working" state would lose exactly the
information a user needs: whether to install something or fix something.
"""

from __future__ import annotations

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

VAULT_ENV = "DYAK_VAULT"
LLM_ENV = "DYAK_LLM_ENDPOINT"
LLM_MODEL_ENV = "DYAK_LLM_MODEL"
LLM_KEY_ENV = "DYAK_LLM_KEY"
DEFAULT_VAULT = Path.home() / "Documents/obs_notes"
PROBE_TIMEOUT = 10.0

SETTINGS_PATH_ENV = "DYAK_SETTINGS"
SETTINGS_KEY = "vault"
#: Written by `write_settings`, read by `resolve_vault`. One key, one file.
SETTINGS_TEMPLATE = "# dyak settings. Written on first run; edit by hand.\n"


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


def read_settings() -> dict[str, str]:
    """Top-level `key = "value"` string pairs from the settings file. Never raises.

    The file is TOML, parsed with stdlib `tomllib` (the >=3.11 floor has it), so trailing
    comments, quoting and escapes mean what any TOML parser says. A file that does not parse
    is reported once to stderr with its path and yields nothing: a typo in a config file must
    not take the pipeline down at stage 0, and must not be read as a garbage value either.
    Non-string values are dropped rather than stringified.
    """
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"dyak: cannot read settings {path}: {exc}", file=sys.stderr)
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"dyak: ignoring malformed settings {path}: {exc}", file=sys.stderr)
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str)}


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
    # checked by `check_llm`. So a fully working run still printed
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
        check_vault(),
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
