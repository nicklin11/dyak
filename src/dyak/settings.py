"""The settings file, and the one resolver every setting goes through (#52).

Three layers of configuration, in the order they win:

    CLI flag  >  environment variable  >  settings file  >  built-in default

`resolve()` is the only place that order is decided. A setting read anywhere else -- an
`os.environ.get("DYAK_...")` at a call site, a second fallback chain -- is a second chance to
get the order wrong and a place the file layer cannot reach. `tests/test_deps.py` fails if any
module outside this one reads a `DYAK_*` variable directly.

The file is TOML, parsed with stdlib `tomllib` (the >=3.11 floor has it). Keys are **flat**,
and a key's name is the environment variable lowercased: `vault`, `stt`, `llm_endpoint`,
`whispercpp_url`. There is no `[llm] endpoint` -> `DYAK_LLM_ENDPOINT` mapping table to drift,
and a warning can name the exact variable to set. `vault` worked this way already, so flat
keys introduce no inconsistency; TOML tables are out of scope and a table is dropped as a
non-string value, which `settings_warnings` reports (#52 owner decision 1).

Which keys the file honours is **derived from the environment-variable constants this module
defines**, minus `DYAK_SETTINGS` itself (which names the file being read, so it is
self-referential). It is not a `DYAK_` prefix test: a bare prefix would make
`llm_endpont = "..."` a silently accepted key and invert the point of #156, that inert keys
and dropped values were invisible. A key is honoured only when a constant with that exact
name exists, so a typo is still a typo (#52 owner decision 3).
"""

from __future__ import annotations

import difflib
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# --- the names -----------------------------------------------------------------------
# Every run-affecting `DYAK_*` variable, defined once, here. Each home module imports the
# name it reads rather than retyping the literal, so the string exists in exactly one place
# and `HONOURED_SETTINGS` below is derived from these constants rather than from a second
# hand-kept list that can drift.
SETTINGS_PATH_ENV = "DYAK_SETTINGS"
VAULT_ENV = "DYAK_VAULT"
TERMS_DIR_ENV = "DYAK_TERMS_DIR"
WORKDIR_ENV = "DYAK_WORKDIR"
OUTPUT_DIR_ENV = "DYAK_OUTPUT_DIR"
BBOX_SOURCE_ENV = "DYAK_BBOX_SOURCE"
STT_BACKEND_ENV = "DYAK_STT"
WHISPERCPP_URL_ENV = "DYAK_WHISPERCPP_URL"
WHISPERCPP_LANGUAGE_ENV = "DYAK_WHISPERCPP_LANGUAGE"
OPENAI_URL_ENV = "DYAK_OPENAI_URL"
OPENAI_KEY_ENV = "DYAK_OPENAI_KEY"
OPENAI_MODEL_ENV = "DYAK_OPENAI_MODEL"
OPENAI_MAX_UPLOAD_BYTES_ENV = "DYAK_OPENAI_MAX_UPLOAD_BYTES"
LLM_ENDPOINT_ENV = "DYAK_LLM_ENDPOINT"
LLM_MODEL_ENV = "DYAK_LLM_MODEL"
LLM_KEY_ENV = "DYAK_LLM_KEY"
VLM_ENDPOINT_ENV = "DYAK_VLM_ENDPOINT"
VLM_MODEL_ENV = "DYAK_VLM_MODEL"
VLM_KEY_ENV = "DYAK_VLM_KEY"

SETTINGS_KEY = "vault"
#: Written by `write_settings`, read by the resolver. One key on the first run; the file
#: carries every honoured key once a user edits it.
SETTINGS_TEMPLATE = "# dyak settings. Written on first run; edit by hand.\n"


def settings_key(env_name: str) -> str:
    """The flat settings key for an environment variable: the variable name, lowercased.

    The single mapping from the one name system to the other. `DYAK_LLM_ENDPOINT` is
    `llm_endpoint`; there is no table, because the rule is the whole mapping.
    """
    return env_name.removeprefix("DYAK_").lower()


#: Every `DYAK_*` name this module defines, read back out of the module's own namespace.
#: Deriving rather than listing is what makes "honoured" a claim about the code: a variable
#: added as a constant is honoured automatically, and a constant that is never honoured is
#: visible as a key the resolver will accept with no reader.
_ENV_NAMES: frozenset[str] = frozenset(
    value for value in globals().values() if isinstance(value, str) and value.startswith("DYAK_")
)

#: The settings keys the tool honours: the environment variables lowercased, minus
#: `DYAK_SETTINGS`. `tests/test_deps.py` harvests the live `DYAK_*` constants from the source
#: and fails if this set drifts from them, and fails if any module reads a `DYAK_*` variable
#: without going through `resolve()` (#156's guard, kept for #52).
HONOURED_SETTINGS: frozenset[str] = frozenset(
    settings_key(name) for name in _ENV_NAMES if name != SETTINGS_PATH_ENV
)

# --- the resolver --------------------------------------------------------------------
#: `Resolved.source` values, named rather than inline so a caller compares against a symbol.
SOURCE_FLAG = "flag"
SOURCE_ENV = "env"
SOURCE_FILE = "file"
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class Resolved:
    """A setting's value and which layer supplied it.

    The source is the half that makes precedence checkable. "The value is X" is satisfied by
    any fallback chain; "the value is X and it came from the environment" is the claim the
    order makes, and a `doctor` line or a warning can name the layer a user must change
    without re-deriving where the value came from.
    """

    name: str
    key: str
    value: str | None
    source: str


def resolve(
    name: str,
    *,
    flag: str | Path | None = None,
    default: str | None = None,
) -> Resolved:
    """The one precedence decision: flag > environment > settings file > built-in default.

    `name` is the environment variable, e.g. `"DYAK_LLM_ENDPOINT"`; the settings key is its
    lowercased form. `flag` is the value an explicit CLI argument already carries -- every
    flag-bearing setting reaches its call site as an argument (`--vault-path` arrives as
    `resolve_vault(explicit)`), so the flag layer is passed in rather than re-parsed here.
    `default` is the built-in fallback, recorded in the result so `source == "default"` is a
    statement about the value and not merely "nothing was set".

    A name the code does not define is refused rather than resolved. A typo in a call site
    would otherwise read as "not configured", which is the confusion #156 exists to remove;
    here it is a loud `ValueError` at the first run instead of a silent default.
    """
    key = settings_key(name)
    if key not in HONOURED_SETTINGS:
        raise ValueError(
            f"{name!r} is not a resolvable setting; known keys: {sorted(HONOURED_SETTINGS)}"
        )

    if flag is not None:
        text = str(flag).strip()
        if text:
            return Resolved(name, key, text, SOURCE_FLAG)

    configured = os.environ.get(name, "").strip()
    if configured:
        return Resolved(name, key, configured, SOURCE_ENV)

    stored = read_settings().get(key, "").strip()
    if stored:
        return Resolved(name, key, stored, SOURCE_FILE)

    return Resolved(name, key, default, SOURCE_DEFAULT)


# --- the file ------------------------------------------------------------------------


def settings_path() -> Path:
    """The settings file: `$DYAK_SETTINGS` if set, else `$XDG_CONFIG_HOME/dyak/settings.toml`.

    The env var names the *file*, not a directory, which is what the name says and what
    testing needs. Read directly rather than through `resolve`: the file cannot name itself,
    so `DYAK_SETTINGS` is the one variable the settings file does not honour.
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

    The parse error is reported once per file per process. The resolver runs several times
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
    """One line per key in `data` that the resolver will not honour (#156).

    Three silent behaviours, all of them the same defect -- a hand-edited file with no
    feedback loop:

      * a key no setting reads (`stt = "whispercpp"` was inert and said nothing before
        #156; it is honoured now, and this line fires for a key that is still no setting);
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

    Called once per invocation from `cli.main`, not from the resolver: the resolver is
    consulted by stage 0, by `process` and by stage 11's terms lookup, so a warning emitted
    by it would repeat several times in one run. One warning per key per run is feedback;
    three is noise. Unreadable and malformed files are reported by `_parse_settings`, which
    `read_settings` also calls, so this adds nothing for them.
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
    Non-string values are dropped rather than stringified -- a TOML table is a non-string
    value, which is the permanent answer for tables (#52 owner decision 1).

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
