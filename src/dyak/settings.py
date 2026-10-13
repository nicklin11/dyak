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

The honoured keys are also **enumerable**: `listing()` prints every one of them with the value
in effect, the layer that supplied it and the built-in default it falls back to (#243), and the
file `write_settings` creates on a first run names every key the file will be read for. A
surface that exists only as the union of the call sites that read it is a surface a user cannot
discover, and the only complete list used to be the `known keys:` line of a typo error.
"""

from __future__ import annotations

import difflib
import os
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
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
#: #115's kill-switch: run the pre-caption branches in sequence instead of concurrently.
SERIAL_ENV = "DYAK_SERIAL"
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
LLM_CONCURRENCY_ENV = "DYAK_LLM_CONCURRENCY"
VLM_ENDPOINT_ENV = "DYAK_VLM_ENDPOINT"
VLM_MODEL_ENV = "DYAK_VLM_MODEL"
VLM_KEY_ENV = "DYAK_VLM_KEY"

SETTINGS_KEY = "vault"
#: The first paragraph of the file `write_settings` writes: what the file is, and how to read
#: the key list under it. The key lines themselves are generated, one per honoured key (#243),
#: so this is only the part that does not depend on which keys exist.
SETTINGS_HEADER = (
    "# dyak settings. Written on first run; edit by hand.\n"
    "#\n"
    "# Every key this file honours, with its built-in default. Uncomment a line to set it;\n"
    "# `dyak settings` prints what is in effect now and which layer supplied each value.\n"
    '# `""` is a key with no built-in default: it stays unset until a layer supplies one.\n'
    "#\n"
)


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

#: Honoured keys whose value is a number in the environment and in the built-in default, so a
#: bare TOML integer in the file is the value rather than a type error (#192). Derived from the
#: constant above for the same reason `HONOURED_SETTINGS` is: the name exists once.
NUMERIC_SETTINGS: frozenset[str] = frozenset(
    {settings_key(OPENAI_MAX_UPLOAD_BYTES_ENV), settings_key(LLM_CONCURRENCY_ENV)}
)

# --- the built-in default of every key (#243) -----------------------------------------
#: Keys whose value is a credential. The listing is the one place that prints values in bulk,
#: so redaction is a property of the key rather than of the printer: an API key that reaches a
#: terminal once has reached a log, a screenshot and a pasted bug report.
SECRET_KEYS: frozenset[str] = frozenset(
    settings_key(name) for name in (LLM_KEY_ENV, VLM_KEY_ENV, OPENAI_KEY_ENV)
)


def _no_default() -> None:
    """A key with no built-in default: unset until a layer supplies a value (#95)."""
    return None


def _vault_default() -> str:
    """`deps.DEFAULT_VAULT` (`~/Documents/obs_notes`), read inside the call.

    A module-level `from . import deps` would be a cycle: `deps.py:23` imports this module
    before it defines `DEFAULT_VAULT` (`deps.py:47`), so at import time the name does not
    exist yet. Deferring the import to the call is what lets this table name a default the
    other module owns instead of retyping the path.
    """
    from . import deps

    return str(deps.DEFAULT_VAULT)


def _upload_default() -> str:
    """`stt.openai_compat.DEFAULT_MAX_UPLOAD_BYTES`, for the reason `_vault_default` gives."""
    from .stt import openai_compat

    return str(openai_compat.DEFAULT_MAX_UPLOAD_BYTES)


#: The built-in default of every honoured key: the value the key has when no flag, no
#: environment variable and no file entry supplies one. The values are callables because two
#: of them are computed -- `vault` sits under the user's home and `workdir` is `$TMPDIR` -- and
#: a listing must print the value a run would use, not the expression that produces it.
#:
#: Declared rather than derived. The defaults live at the `resolve()` call sites (`deps.py:225`,
#: `workspace.py:48`, `pipeline.py:378`, `pipeline.py:718`, `stt/backends.py:129`,
#: `stt/openai_compat.py:124`) and enumerating the surface must not run a pipeline, so this is a
#: second statement of them; `tests/test_settings.py` re-reads those call sites and fails if an
#: entry here disagrees with the default the code passes. That is what keeps the table a checked
#: claim instead of a comfortable lie -- the drift `NUMERIC_SETTINGS` above exists to avoid.
SETTING_DEFAULTS: dict[str, Callable[[], str | None]] = {
    settings_key(BBOX_SOURCE_ENV): lambda: "geometric",
    settings_key(LLM_ENDPOINT_ENV): _no_default,
    settings_key(LLM_KEY_ENV): _no_default,
    settings_key(LLM_MODEL_ENV): _no_default,
    settings_key(OPENAI_KEY_ENV): _no_default,
    settings_key(OPENAI_MAX_UPLOAD_BYTES_ENV): _upload_default,
    settings_key(OPENAI_MODEL_ENV): lambda: "whisper-1",
    settings_key(OPENAI_URL_ENV): _no_default,
    settings_key(OUTPUT_DIR_ENV): _no_default,
    settings_key(SERIAL_ENV): lambda: "0",
    settings_key(STT_BACKEND_ENV): lambda: "auto",
    settings_key(TERMS_DIR_ENV): _no_default,
    settings_key(VAULT_ENV): _vault_default,
    settings_key(VLM_ENDPOINT_ENV): _no_default,
    settings_key(VLM_KEY_ENV): _no_default,
    settings_key(VLM_MODEL_ENV): _no_default,
    settings_key(WHISPERCPP_LANGUAGE_ENV): _no_default,
    settings_key(WHISPERCPP_URL_ENV): _no_default,
    settings_key(WORKDIR_ENV): tempfile.gettempdir,
}


def default_for(name: str) -> str | None:
    """The built-in default of the setting `name` is the variable for, or `None` if it has none."""
    return SETTING_DEFAULTS[settings_key(name)]()


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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        _report_once(path, f"cannot read settings {path}: {exc}")
        return None
    except tomllib.TOMLDecodeError as exc:
        _report_once(path, f"ignoring malformed settings {path}: {exc}")
        return None
    return _coerce_numeric(data)


def _coerce_numeric(data: dict[str, object]) -> dict[str, object]:
    """A TOML number for a numeric setting becomes its decimal text (#192).

    `openai_max_upload_bytes` is a number in the environment and in the built-in default, and
    `docs/running.md` documents a bare byte count, so `20000` in the file must be the value
    rather than a dropped non-string. The coercion sits here, not in `read_settings`: `doctor`
    reads the parse result directly (`deps.check_settings`), so a reader-side fix would leave
    the key listed as honoured *and* ignored on the same line.

    A `bool` is excluded because `isinstance(True, int)` is true and `true` is not a count;
    the consumer's own parser stays the authority on whether the text is a valid count, so
    `20000.5` is handed to it and fails loudly naming the setting rather than silently
    falling back to the default.
    """
    return {
        key: (
            str(value)
            if key in NUMERIC_SETTINGS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            else value
        )
        for key, value in data.items()
    }


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
    value, which is the permanent answer for tables (#52 owner decision 1). A TOML number under
    a numeric key is not a non-string by the time it reaches this filter: `_parse_settings`
    coerces it to its decimal text (#192).

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


def _toml_default(key: str) -> str:
    """A key's built-in default as a TOML value, for the commented lines of the first-run file.

    `""` for a key with no default: an empty string is not a value in any layer (#52), so
    uncommenting that line reproduces the behaviour it replaces. A numeric setting is written
    bare, as `docs/running.md` documents for `openai_max_upload_bytes` (#192); everything else
    is quoted and escaped, so a default that contains a quote still parses.
    """
    value = SETTING_DEFAULTS[key]()
    if value is None:
        return '""'
    if key in NUMERIC_SETTINGS and value.isdigit():
        return value
    return _toml_string(value)


def settings_template(vault: Path) -> str:
    """The whole first-run file: every honoured key, in key order, plus the one live line.

    #243: a first run wrote `vault` and a one-line comment, so the other eighteen keys were
    invisible to anyone who had not read this module -- the owner's ask was for a file with
    "all llm endpoints and all preferences", and there was no way to learn which keys those
    are. Every key is now named, with its built-in default as the value. Only `vault` is
    uncommented, because it is the one value a first run knows; the rest are comments, so the
    file still reads back as exactly `{vault: ...}` until a user uncomments something.
    """
    lines = [SETTINGS_HEADER.rstrip("\n")]
    for key in sorted(HONOURED_SETTINGS):
        if key == SETTINGS_KEY:
            lines.append(f"{key} = {_toml_string(str(vault))}")
        else:
            lines.append(f"# {key} = {_toml_default(key)}")
    return "\n".join(lines) + "\n"


def write_settings(vault: Path) -> Path:
    """Persist the vault, creating the file on first use. Returns the path written.

    Writing is the whole point of the first run: without it, "settable at setup" means the
    user has to guess that `DYAK_VAULT` exists, and a default nobody can discover is a
    default nobody will change. The value is escaped so any path round-trips through
    `read_settings` unchanged. The file names every honoured key, not only this one (#243),
    so the same first run also answers "what else can I set here".
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings_template(vault), encoding="utf-8")
    return path


# --- the surface, enumerated (#243) ---------------------------------------------------
@dataclass(frozen=True)
class Setting:
    """One honoured key as the listing shows it: value, supplying layer, built-in default."""

    key: str
    value: str | None
    source: str
    default: str | None

    @property
    def secret(self) -> bool:
        """True when the value is a credential and must not be printed."""
        return self.key in SECRET_KEYS

    @property
    def shown(self) -> str:
        """The value as the listing prints it: never a secret, and explicit about unset.

        `(unset)` and `(redacted)` are different facts -- no layer supplied a value, versus one
        did and it is not being printed -- and a reader asking whether an endpoint is
        configured must be able to tell them apart.
        """
        if self.value is None:
            return "(unset)"
        return "(redacted)" if self.secret else self.value


def describe(flags: Mapping[str, str | None] | None = None) -> list[Setting]:
    """Every honoured key, resolved now, with the layer that supplied it and its default.

    `flags` maps an environment-variable name (`VAULT_ENV`) to the value an explicit CLI
    argument carries, exactly as `resolve` takes one, so a caller can ask what a run with
    `--vault-path /tmp/v` would see. `dyak settings` passes the four flags it accepts, so its
    output answers for the command line it was given and nothing else.

    The `resolve` calls are written out one per key rather than looped over
    `HONOURED_SETTINGS`. `tests/test_deps.py` harvests `resolve(...)` call sites and fails on
    one whose variable it cannot name -- that guard is what keeps "every honoured key has a
    reader" a fact about the source -- and a loop would call `resolve(name)`, which is exactly
    the shape it refuses. The verbosity is the price of the key set staying checkable.
    """
    flags = flags or {}
    # Alphabetical by key, which is the order `settings_template` writes the file in: the
    # listing and the file a reader edits name the surface in the same order.
    resolved = [
        resolve(
            BBOX_SOURCE_ENV, flag=flags.get(BBOX_SOURCE_ENV), default=default_for(BBOX_SOURCE_ENV)
        ),
        resolve(
            LLM_ENDPOINT_ENV,
            flag=flags.get(LLM_ENDPOINT_ENV),
            default=default_for(LLM_ENDPOINT_ENV),
        ),
        resolve(LLM_KEY_ENV, flag=flags.get(LLM_KEY_ENV), default=default_for(LLM_KEY_ENV)),
        resolve(LLM_MODEL_ENV, flag=flags.get(LLM_MODEL_ENV), default=default_for(LLM_MODEL_ENV)),
        resolve(
            OPENAI_KEY_ENV, flag=flags.get(OPENAI_KEY_ENV), default=default_for(OPENAI_KEY_ENV)
        ),
        resolve(
            OPENAI_MAX_UPLOAD_BYTES_ENV,
            flag=flags.get(OPENAI_MAX_UPLOAD_BYTES_ENV),
            default=default_for(OPENAI_MAX_UPLOAD_BYTES_ENV),
        ),
        resolve(
            OPENAI_MODEL_ENV,
            flag=flags.get(OPENAI_MODEL_ENV),
            default=default_for(OPENAI_MODEL_ENV),
        ),
        resolve(
            OPENAI_URL_ENV, flag=flags.get(OPENAI_URL_ENV), default=default_for(OPENAI_URL_ENV)
        ),
        resolve(
            OUTPUT_DIR_ENV, flag=flags.get(OUTPUT_DIR_ENV), default=default_for(OUTPUT_DIR_ENV)
        ),
        resolve(SERIAL_ENV, flag=flags.get(SERIAL_ENV), default=default_for(SERIAL_ENV)),
        resolve(
            STT_BACKEND_ENV, flag=flags.get(STT_BACKEND_ENV), default=default_for(STT_BACKEND_ENV)
        ),
        resolve(TERMS_DIR_ENV, flag=flags.get(TERMS_DIR_ENV), default=default_for(TERMS_DIR_ENV)),
        resolve(VAULT_ENV, flag=flags.get(VAULT_ENV), default=default_for(VAULT_ENV)),
        resolve(
            VLM_ENDPOINT_ENV,
            flag=flags.get(VLM_ENDPOINT_ENV),
            default=default_for(VLM_ENDPOINT_ENV),
        ),
        resolve(VLM_KEY_ENV, flag=flags.get(VLM_KEY_ENV), default=default_for(VLM_KEY_ENV)),
        resolve(VLM_MODEL_ENV, flag=flags.get(VLM_MODEL_ENV), default=default_for(VLM_MODEL_ENV)),
        resolve(
            WHISPERCPP_LANGUAGE_ENV,
            flag=flags.get(WHISPERCPP_LANGUAGE_ENV),
            default=default_for(WHISPERCPP_LANGUAGE_ENV),
        ),
        resolve(
            WHISPERCPP_URL_ENV,
            flag=flags.get(WHISPERCPP_URL_ENV),
            default=default_for(WHISPERCPP_URL_ENV),
        ),
        resolve(WORKDIR_ENV, flag=flags.get(WORKDIR_ENV), default=default_for(WORKDIR_ENV)),
    ]
    return [Setting(row.key, row.value, row.source, default_for(row.name)) for row in resolved]


def listing(flags: Mapping[str, str | None] | None = None) -> str:
    """The whole surface as text: one row per honoured key, then the file they come from.

    `dyak settings` prints this and exits 0. A listing is not a finding: the exit-code table
    (ADR-0001 D4) is about what a run did, and a command whose whole job is to answer a
    question must not answer it with a failure code.
    """
    rows = describe(flags)
    cells = [(row.key, row.shown, row.source, row.default or "(none)") for row in rows]
    head = ("key", "value", "source", "default")
    widths = [max(len(cell) for cell in column) for column in zip(head, *cells)]

    def render(row: tuple[str, ...]) -> str:
        return "  ".join(text.ljust(width) for text, width in zip(row, widths)).rstrip()

    path = settings_path()
    by_source = {
        source: sum(1 for row in rows if row.source == source)
        for source in (SOURCE_FLAG, SOURCE_ENV, SOURCE_FILE, SOURCE_DEFAULT)
    }
    summary = ", ".join(f"{count} from {source}" for source, count in by_source.items() if count)
    return "\n".join(
        [
            "Every key dyak honours, and the layer that supplied its value.",
            "Precedence: flag > env > file > default. `(unset)` is a key no layer supplies;",
            "`(redacted)` is a credential, shown by name only.",
            "",
            render(head),
            render(tuple("-" * width for width in widths)),
            *(render(row) for row in cells),
            "",
            f"{len(rows)} keys: {summary}.",
            f"settings file: {path} ({'present' if path.is_file() else 'absent'})",
        ]
    )
