#!/usr/bin/env python3
"""The settings file as a real third layer, and the one resolver (#52).

`settings.resolve` is the single place the order -- **flag > environment > settings file >
built-in default** -- is decided. This suite pins that order, the flat-key naming rule, the
derived honoured-key set, and the rejection of typos, then exercises the resolver through the
call sites that matter (vault, terms dir, output dir, work dir, both model roles, STT) rather
than only at the mechanism. A resolver that works and is not used is the defect #52 exists
for, so the last section asserts the file value actually reaches behaviour.

The surface is then enumerated (#243): the listing names every honoured key with its layer and
its default, the credentials are redacted, and the file a first run writes names every key too.
The defaults are re-read from the `resolve()` call sites, so the table cannot drift from them.

Isolated with `tests/_env.py`, so a developer's exported `DYAK_*` cannot answer these.
"""

import ast
import io
import os
import sys
import tempfile
import urllib.error
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dyak import bundle as dyb  # noqa: E402
from dyak import cli as dyc  # noqa: E402
from dyak import deps as dyd  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402
from dyak import link as dylnk  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import runner  # noqa: E402
from dyak import settings  # noqa: E402
from dyak import workspace as dyw  # noqa: E402
from dyak.llm import DEFAULT_CONCURRENCY, Config  # noqa: E402
from dyak.stt import backends as sttb  # noqa: E402
from dyak.stt import openai_compat, whispercpp  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def _raises(exc: type[BaseException], fn) -> bool:
    """True when `fn` raises `exc`; used for the resolver's refusal of unknown names."""
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


TMP = Path(tempfile.mkdtemp(prefix="dyak-settings-"))
SETTINGS = TMP / "settings.toml"
os.environ[settings.SETTINGS_PATH_ENV] = str(SETTINGS)


def use_file(body: str) -> None:
    """Write the settings file and clear every `DYAK_*` except the path to it."""
    for key in [k for k in os.environ if k.startswith("DYAK_")]:
        del os.environ[key]
    os.environ[settings.SETTINGS_PATH_ENV] = str(SETTINGS)
    SETTINGS.write_text(body, encoding="utf-8")


# --- 1. the four layers, in order ---------------------------------------------
# One representative key per layer transition. `llm_endpoint` is the one with the most
# call sites (`llm.Config.from_env` and `deps.check_llm`), so a wrong order here is the one
# most likely to reach a run.
use_file('llm_endpoint = "http://file/v1"\n')
check(
    "the file supplies a value when nothing else does",
    settings.resolve(settings.LLM_ENDPOINT_ENV).value == "http://file/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV).source == settings.SOURCE_FILE,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV)),
)

os.environ[settings.LLM_ENDPOINT_ENV] = "http://env/v1"
check(
    "the environment beats the file",
    settings.resolve(settings.LLM_ENDPOINT_ENV).value == "http://env/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV).source == settings.SOURCE_ENV,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV)),
)

check(
    "a flag beats both the environment and the file",
    settings.resolve(settings.LLM_ENDPOINT_ENV, flag="http://flag/v1").value == "http://flag/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV, flag="http://flag/v1").source
    == settings.SOURCE_FLAG,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV, flag="http://flag/v1")),
)

use_file("")
check(
    "with nothing set, the built-in default is the winner and is recorded",
    settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1").value
    == "http://default/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1").source
    == settings.SOURCE_DEFAULT,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1")),
)

# An empty value in any layer is "unset", not a value: `DYAK_LLM_ENDPOINT=""` must not shadow
# the file, which is what the previous per-call-site `.strip() or` chains did.
use_file('llm_endpoint = "http://file/v1"\n')
os.environ[settings.LLM_ENDPOINT_ENV] = "   "
check(
    "a blank environment value does not shadow the file",
    settings.resolve(settings.LLM_ENDPOINT_ENV).value == "http://file/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV).source == settings.SOURCE_FILE,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV)),
)

# --- 2. the key name is the variable, lowercased ------------------------------
check(
    "a key's name is its environment variable lowercased",
    settings.settings_key(settings.LLM_ENDPOINT_ENV) == "llm_endpoint"
    and settings.settings_key(settings.WHISPERCPP_URL_ENV) == "whispercpp_url"
    and settings.settings_key(settings.VLM_MODEL_ENV) == "vlm_model",
    f"{settings.settings_key(settings.VLM_MODEL_ENV)!r}",
)

# --- 3. the honoured set is derived, complete, and closed ---------------------
# Derived: every honoured key's variable is a constant `settings.py` defines. Complete: the
# reverse. The AST check in `tests/test_deps.py` holds this to the whole source tree; this is
# the runtime half, so the set a *file* is measured against is the set the code defines.
honoured_vars = {f"DYAK_{key.upper()}" for key in settings.HONOURED_SETTINGS}
defined = {
    value
    for value in vars(settings).values()
    if isinstance(value, str) and value.startswith("DYAK_")
}
check(
    "every honoured key is derived from a constant the module defines",
    honoured_vars == defined - {settings.SETTINGS_PATH_ENV},
    f"honoured: {sorted(honoured_vars)}; defined: {sorted(defined)}",
)
check(
    "the set is populated and excludes DYAK_SETTINGS",
    len(settings.HONOURED_SETTINGS) >= 15
    and settings.SETTINGS_PATH_ENV not in honoured_vars
    and settings.settings_key(settings.SETTINGS_PATH_ENV) not in settings.HONOURED_SETTINGS,
    f"{len(settings.HONOURED_SETTINGS)} keys",
)
check(
    "the resolver refuses a name the code does not define, rather than reading a default",
    _raises(ValueError, lambda: settings.resolve("DYAK_LLM_ENDPONT")),
    "DYAK_LLM_ENDPONT",
)

# Every honoured key is settable from the file: one line per key, source `file`. This is the
# "asserted for each setting, not once for the mechanism" acceptance criterion.
all_keys = "".join(f'{key} = "from-file"\n' for key in sorted(settings.HONOURED_SETTINGS))
use_file(all_keys)
wrong = {
    key: settings.resolve(f"DYAK_{key.upper()}").source
    for key in sorted(settings.HONOURED_SETTINGS)
    if settings.resolve(f"DYAK_{key.upper()}").value != "from-file"
    or settings.resolve(f"DYAK_{key.upper()}").source != settings.SOURCE_FILE
}
check(
    "every honoured key resolves from the file, one at a time",
    not wrong,
    f"not from the file: {wrong}",
)


# --- 4. a typo is warned and not honoured -------------------------------------
# The constraint that makes this non-trivial (#52 owner decision 3): a bare `DYAK_` prefix
# test would honour `llm_endpont`, inverting #156. The honoured set is the constants, so a
# near-miss is a typo and an invented name is inert -- both warned, neither read.
def warnings_for(body: str) -> tuple[dict[str, str], str]:
    use_file(body)
    buf = io.StringIO()
    with redirect_stderr(buf):
        settings.report_settings()
        read = settings.read_settings()
    return read, buf.getvalue()


near_read, near_err = warnings_for('llm_endpont = "http://typo/v1"\n')
check(
    "a near-miss key is reported as a likely typo, naming the honoured key",
    "llm_endpont" in near_err and "llm_endpoint" in near_err and "did you mean" in near_err,
    repr(near_err),
)
check(
    "a near-miss key is not honoured: the resolver returns the default",
    settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1").value
    == "http://default/v1"
    and settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1").source
    == settings.SOURCE_DEFAULT,
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1")),
)

invented_read, invented_err = warnings_for('totally_invented = "x"\n')
check(
    "an invented key is reported as one nothing reads",
    "totally_invented" in invented_err and "not a setting" in invented_err,
    repr(invented_err),
)
check(
    "the file is still read back whole, so doctor can report what it ignored",
    near_read == {"llm_endpont": "http://typo/v1"} and invented_read == {"totally_invented": "x"},
    f"{near_read!r} {invented_read!r}",
)

# A TOML table is out of scope by decision, and its permanent answer is "dropped, warned".
table_read, table_err = warnings_for('[llm]\nendpoint = "http://table/v1"\n')
check(
    "a TOML table is dropped as a non-string value and warned as a table (#52 decision 1)",
    table_read == {} and "table" in table_err and "llm" in table_err,
    repr(table_err),
)
check(
    "a table does not configure the endpoint it looks like it configures",
    settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1").value
    == "http://default/v1",
    repr(settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1")),
)

# A malformed file is a named error, not a traceback, and every setting falls through.
bad_path = TMP / "malformed.toml"
bad_path.write_text('llm_endpoint = "unterminated\n', encoding="utf-8")
os.environ[settings.SETTINGS_PATH_ENV] = str(bad_path)
buf = io.StringIO()
with redirect_stderr(buf):
    malformed = settings.resolve(settings.LLM_ENDPOINT_ENV, default="http://default/v1")
os.environ[settings.SETTINGS_PATH_ENV] = str(SETTINGS)
check(
    "malformed TOML is reported by path and falls through to the default",
    "malformed" in buf.getvalue()
    and str(bad_path) in buf.getvalue()
    and malformed.value == "http://default/v1"
    and malformed.source == settings.SOURCE_DEFAULT,
    f"{buf.getvalue()!r} {malformed!r}",
)

# --- 5. the resolver is what the call sites use --------------------------------
# A correct resolver that the code does not consult is the whole bug. Each of these drives a
# real call site with a file value and asserts the value arrived.
use_file('vault = "/from-file/vault"\n')
check(
    "resolve_vault takes the file value when neither flag nor environment is set",
    dyd.resolve_vault() == Path("/from-file/vault"),
    repr(dyd.resolve_vault()),
)
os.environ[settings.VAULT_ENV] = "/from-env/vault"
check(
    "resolve_vault still prefers the environment over the file",
    dyd.resolve_vault() == Path("/from-env/vault"),
    repr(dyd.resolve_vault()),
)
check(
    "resolve_vault still prefers the explicit flag over both",
    dyd.resolve_vault("/from-flag/vault") == Path("/from-flag/vault"),
    repr(dyd.resolve_vault("/from-flag/vault")),
)
use_file("")
check(
    "vault keeps its built-in default when nothing names it",
    dyd.resolve_vault() == dyd.DEFAULT_VAULT,
    repr(dyd.resolve_vault()),
)

# llm.py is a second reader of the same names: if it did not consult the file, the endpoint
# the pipeline uses would disagree with the one doctor reports.
use_file('llm_endpoint = "http://file/v1"\nllm_model = "file-model"\nllm_key = "file-key"\n')
cfg = Config.from_env()
check(
    "Config.from_env reads the endpoint, model and key from the file",
    (cfg.endpoint, cfg.model, cfg.api_key) == ("http://file/v1", "file-model", "file-key"),
    f"{cfg.endpoint} {cfg.model} {cfg.api_key}",
)
os.environ[settings.LLM_ENDPOINT_ENV] = "http://env/v1"
check(
    "an environment endpoint overrides the file in Config.from_env",
    Config.from_env().endpoint == "http://env/v1",
    Config.from_env().endpoint,
)

use_file('vlm_endpoint = "http://file-vlm/v1"\nvlm_model = "file-vlm-model"\n')
vision = Config.vision_from_env()
check(
    "Config.vision_from_env reads the vision role from the file",
    (vision.endpoint, vision.model) == ("http://file-vlm/v1", "file-vlm-model")
    and not vision.is_vision_default,
    f"{vision.endpoint} {vision.model} default={vision.is_vision_default}",
)

terms = TMP / "terms"
terms.mkdir(exist_ok=True)
use_file(f'terms_dir = "{terms}"\n')
check(
    "link.resolve_terms_dir finds the terms directory named in the file",
    dylnk.resolve_terms_dir(None, None) == terms,
    repr(dylnk.resolve_terms_dir(None, None)),
)

out = TMP / "from-file-out"
use_file(f'output_dir = "{out}"\n')
check(
    "Bundle.open uses the output directory named in the file",
    dyb.Bundle.open(None).root == out.resolve(),
    repr(dyb.Bundle.open(None).root),
)

work = TMP / "from-file-work"
work.mkdir(exist_ok=True)
use_file(f'workdir = "{work}"\n')
made = dyw.WorkDir()
check(
    "WorkDir uses the parent named in the file",
    made.path.parent == work,
    repr(made.path),
)
made.release()

use_file('stt = "openai"\nopenai_url = "http://file-openai/v1"\nopenai_model = "file-stt"\n')
check(
    "stt backend selection reads the file",
    sttb.resolve().name == "openai",
    sttb.resolve().name,
)
check(
    "the OpenAI-compatible STT endpoint and model come from the file",
    openai_compat.OpenAICompat().base_url == "http://file-openai/v1"
    and openai_compat.OpenAICompat().model == "file-stt",
    f"{openai_compat.OpenAICompat().base_url} {openai_compat.OpenAICompat().model}",
)

use_file('whispercpp_url = "http://file-whisper/v1"\nwhispercpp_language = "ru"\n')
check(
    "the whisper.cpp endpoint comes from the file",
    whispercpp.HttpWhisperCpp().base_url == "http://file-whisper/v1",
    whispercpp.HttpWhisperCpp().base_url,
)
# #95 removed the built-in default only; a configured URL must still resolve through all
# three real layers. The file value above is the baseline both of these must beat.
check(
    "the flag layer still beats the file for the STT URL (#95)",
    whispercpp.HttpWhisperCpp("http://flag-whisper/v1").base_url == "http://flag-whisper/v1"
    and openai_compat.OpenAICompat("http://flag-openai/v1").base_url == "http://flag-openai/v1",
    f"{whispercpp.HttpWhisperCpp('http://flag-whisper/v1').base_url}",
)
os.environ[settings.WHISPERCPP_URL_ENV] = "http://env-whisper/v1"
check(
    "the environment still beats the file for the STT URL (#95)",
    whispercpp.HttpWhisperCpp().base_url == "http://env-whisper/v1",
    whispercpp.HttpWhisperCpp().base_url,
)

# --- 5b. no URL in any layer is "not configured", not a probe of 10302 (#95) ---
# Both backends used to pass `default="http://127.0.0.1:10302"` to `settings.resolve`: an
# unset URL silently probed a raw container port with no auto-start, which is the target
# #62 measured being reclaimed after 300 s. There is no built-in default now, and the
# failure must name the missing configuration rather than a refused connection.
use_file("")
probes: list[str] = []
_real_whisper_urlopen = whispercpp.urllib.request.urlopen
_real_openai_urlopen = openai_compat.urllib.request.urlopen


def _forbidden_urlopen(request, timeout=None):
    """Record the probe and refuse it, so an unexpected one is a clean FAIL, not a crash."""
    probes.append(getattr(request, "full_url", str(request)))
    raise urllib.error.URLError(OSError(111, "Connection refused"))


whispercpp.urllib.request.urlopen = _forbidden_urlopen
openai_compat.urllib.request.urlopen = _forbidden_urlopen
try:
    unset_exc = None
    try:
        sttb.resolve()
    except runner.DependencyError as exc:
        unset_exc = exc
    explicit_excs: dict[str, runner.DependencyError | None] = {}
    for _cls in (whispercpp.HttpWhisperCpp, openai_compat.OpenAICompat):
        try:
            _cls().check()
        except runner.DependencyError as exc:
            explicit_excs[_cls.name] = exc
        else:
            explicit_excs[_cls.name] = None
finally:
    whispercpp.urllib.request.urlopen = _real_whisper_urlopen
    openai_compat.urllib.request.urlopen = _real_openai_urlopen
check(
    "an unset STT URL opens no socket, so 127.0.0.1:10302 is never probed (#95)",
    not probes,
    f"probes={probes}",
)
check(
    "an unset STT URL is exit 2 and says it is not configured (#95)",
    unset_exc is not None
    and unset_exc.code == ec.MISSING_DEPENDENCY
    and "not configured" in unset_exc.message.lower(),
    f"{getattr(unset_exc, 'code', None)} {getattr(unset_exc, 'message', None)}",
)
check(
    "the not-configured remediation names the env var, the proxy and the 300 s reclaim (#95)",
    unset_exc is not None
    and "DYAK_WHISPERCPP_URL" in unset_exc.remediation
    and "100.64.0.1:10301" in unset_exc.remediation
    and "300 s" in unset_exc.remediation,
    getattr(unset_exc, "remediation", ""),
)
for _name, _exc in explicit_excs.items():
    check(
        f"an explicitly selected unconfigured {_name} backend is exit 2 too (#95)",
        _exc is not None
        and _exc.code == ec.MISSING_DEPENDENCY
        and "not configured" in _exc.message.lower(),
        f"{getattr(_exc, 'code', None)} {getattr(_exc, 'message', None)}",
    )

# A *configured* URL that refuses is a different state: exit 3, "not reachable". The two
# must not collapse into one message, because they send the operator to different places.
use_file('whispercpp_url = "http://127.0.0.1:9"\n')
check(
    "a URL in the file is configured, so the backend does not claim otherwise (#95)",
    getattr(whispercpp.HttpWhisperCpp(), "configured", False),
    repr(whispercpp.HttpWhisperCpp().base_url),
)


def _refusing_urlopen(request, timeout=None):
    raise urllib.error.URLError(OSError(111, "Connection refused"))


whispercpp.urllib.request.urlopen = _refusing_urlopen
try:
    configured_exc = None
    try:
        whispercpp.HttpWhisperCpp().check()
    except runner.DependencyError as exc:
        configured_exc = exc
finally:
    whispercpp.urllib.request.urlopen = _real_whisper_urlopen
check(
    "a configured-but-refusing endpoint is exit 3, distinct from not configured (#95)",
    configured_exc is not None
    and configured_exc.code == ec.DEPENDENCY_FAILED
    and "not configured" not in configured_exc.message.lower(),
    f"{getattr(configured_exc, 'code', None)} {getattr(configured_exc, 'message', None)}",
)
check(
    "the unreachable remediation also names the proxy and the 300 s reclaim (#95)",
    configured_exc is not None
    and "100.64.0.1:10301" in configured_exc.remediation
    and "300 s" in configured_exc.remediation,
    getattr(configured_exc, "remediation", ""),
)

use_file('bbox_source = "null"\n')
check(
    "stage 5's bbox source comes from the file",
    dyp.resolve(settings.BBOX_SOURCE_ENV, default="geometric").value == "null",
    repr(dyp.resolve(settings.BBOX_SOURCE_ENV, default="geometric")),
)

# --- 5c. the numeric setting has a working form in the file (#192) -------------
# `openai_max_upload_bytes` is a number in the environment and in the built-in default, and
# `docs/running.md:56` documents a bare byte count. A TOML integer in the file must therefore
# be the value, not a dropped non-string -- and it must reach the consumer's own parser, which
# stays the authority on whether it is a valid count.
check(
    "every numeric setting is an honoured key (#192)",
    settings.NUMERIC_SETTINGS <= settings.HONOURED_SETTINGS,
    repr(sorted(settings.NUMERIC_SETTINGS - settings.HONOURED_SETTINGS)),
)

_bare_ok: dict[str, tuple[str | None, str]] = {}
for _key in sorted(settings.NUMERIC_SETTINGS):
    use_file(f"{_key} = 20000\n")
    _resolved = settings.resolve(f"DYAK_{_key.upper()}", default="0")
    _bare_ok[_key] = (_resolved.value, _resolved.source)
check(
    "every numeric setting resolves from a bare TOML integer (#192)",
    all(value == "20000" and source == settings.SOURCE_FILE for value, source in _bare_ok.values()),
    repr(_bare_ok),
)

use_file("openai_max_upload_bytes = 20000\n")
_numeric = settings.resolve(settings.OPENAI_MAX_UPLOAD_BYTES_ENV, default="26214400")
check(
    "a bare byte count in the file is honoured, not reported as ignored (#192)",
    _numeric.value == "20000" and _numeric.source == settings.SOURCE_FILE,
    repr(_numeric),
)
check(
    "the file value reaches the consumer's own parser as bytes (#192)",
    openai_compat._threshold_bytes(_numeric.value) == 20000,
    str(openai_compat._threshold_bytes(_numeric.value)),
)
_bare_err = io.StringIO()
with redirect_stderr(_bare_err):
    settings.report_settings()
check(
    "a bare number for a numeric key produces no warning (#192)",
    _bare_err.getvalue() == "",
    repr(_bare_err.getvalue()),
)

# The other half of the symptom in the issue: `doctor` printed the key as honoured *and* as
# ignored on one line, because `check_settings` has its own `isinstance(v, str)` filter and the
# file held a number. It reads `_parse_settings` directly, so the coercion must be visible to it
# too -- a fix in `read_settings` alone would leave this line contradicting itself.
_report = dyd.check_settings()
check(
    "doctor reports the bare numeric key as honoured, not as honoured and ignored (#192)",
    "honoured: openai_max_upload_bytes" in _report.detail and "ignored" not in _report.detail,
    _report.detail,
)

use_file('openai_max_upload_bytes = "20000"\n')
_quoted = settings.resolve(settings.OPENAI_MAX_UPLOAD_BYTES_ENV)
check(
    "the quoted form still works, so an existing file does not break (#192)",
    _quoted.value == "20000" and _quoted.source == settings.SOURCE_FILE,
    repr(_quoted),
)

# The #156 half must not weaken: a number for a *string* key is still a dropped value with a
# warning. `tests/test_deps.py` pins this too; the two must agree.
use_file("vault = 42\n")
_string_err = io.StringIO()
with redirect_stderr(_string_err):
    settings.report_settings()
    _string_read = settings.read_settings()
check(
    "a number for a string key is still dropped and warned, so #156 is not weakened (#192)",
    _string_read == {}
    and "number" in _string_err.getvalue()
    and "ignored" in _string_err.getvalue(),
    f"read={_string_read!r} err={_string_err.getvalue()!r}",
)

# --- 6b. the declared table covers the honoured set (#250) ---------------------
# `HONOURED_SETTINGS` is derived from this module's own constants, so it grows by itself, while
# `SETTING_DEFAULTS` is hand-written. `settings_template` walks the honoured keys and looks each
# one up, so a key in the first set and not the second is a `KeyError` on a *first run* -- which
# is what a clean merge of #247 with #246 (DYAK_LLM_CONCURRENCY) produced, and what no check here
# saw. The two sets have to be equal: a constant cannot arrive without a declared default, and a
# default cannot outlive its key.
#
# Checked here, before the first-run write below, so a drift is reported by name instead of
# aborting the suite with the very `KeyError` this check exists to explain.
check(
    "every honoured key is declared in SETTING_DEFAULTS, and nothing else is (#250)",
    set(settings.SETTING_DEFAULTS) == settings.HONOURED_SETTINGS,
    f"honoured but undeclared: "
    f"{sorted(settings.HONOURED_SETTINGS - set(settings.SETTING_DEFAULTS))}; "
    f"declared but not honoured: "
    f"{sorted(set(settings.SETTING_DEFAULTS) - settings.HONOURED_SETTINGS)}",
)

# --- 6. a first run still writes the one key it has always written --------------
use_file("")
written = settings.write_settings(Path("/first/run/vault"))
check(
    "the first run writes the vault key and the file reads back",
    written == SETTINGS
    and settings.read_settings() == {settings.SETTINGS_KEY: "/first/run/vault"}
    and dyd.resolve_vault() == Path("/first/run/vault"),
    f"{written} {settings.read_settings()!r}",
)

# --- 7. the surface is enumerable (#243) --------------------------------------
# Nineteen keys are honoured, and before this the only complete list of them was the `known
# keys:` line of a typo error: a setting nobody can enumerate is a setting nobody sets. The
# listing is that surface and the first-run file names it. Both are held to the call sites
# below rather than to their own copy -- a table that agrees only with itself is the drift this
# suite exists to catch.
PKG = REPO / "src" / "dyak"
SETTINGS_TREE = ast.parse((PKG / "settings.py").read_text(encoding="utf-8"))

#: The four defaults that are computed rather than literal, keyed by the expression the call
#: site passes (`ast.unparse`). Anything else that is not a string literal is reported as
#: unpinned, so a fifth computed default cannot be added without being read back here.
COMPUTED_DEFAULTS: dict[str, Callable[[], str | None]] = {
    "str(DEFAULT_VAULT)": lambda: str(dyd.DEFAULT_VAULT),
    "tempfile.gettempdir()": tempfile.gettempdir,
    "str(DEFAULT_MAX_UPLOAD_BYTES)": lambda: str(openai_compat.DEFAULT_MAX_UPLOAD_BYTES),
    "str(DEFAULT_CONCURRENCY)": lambda: str(DEFAULT_CONCURRENCY),
}


def _literal_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so `resolve(ENDPOINT_ENV)` can be resolved."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = value.value
    return out


def _imported_constants(tree: ast.Module, known: dict[str, str]) -> dict[str, str]:
    """`from ..settings import NAME as ALIAS` bindings, resolved to the literal in `settings`.

    The constants live in one module, so a home module reaches them through an import, often
    under a local alias (`stt/backends.py` imports `STT_BACKEND_ENV` as `ENV_BACKEND`).
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.endswith("settings"):
            continue
        for alias in node.names:
            if alias.name in known:
                out[alias.asname or alias.name] = known[alias.name]
    return out


def _resolve_call_defaults() -> tuple[dict[str, set[str | None]], list[str]]:
    """Per honoured key, the `default=` values its `resolve()` call sites pass.

    Returns the values by key and the `file:line expression` of any `default=` this test cannot
    resolve. `settings.py` is skipped: `describe()` passes `default_for(KEY)`, which is the
    table reading itself, and the table is what the call sites are measured against.
    """
    known = _literal_constants(SETTINGS_TREE)
    defaults: dict[str, set[str | None]] = {}
    unpinned: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "settings.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = {**known, **_imported_constants(tree, known), **_literal_constants(tree)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            else:
                continue
            if called != "resolve":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id in consts:
                name = consts[arg.id]
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                name = arg.value
            elif isinstance(arg, ast.Attribute) and arg.attr in known:
                name = known[arg.attr]
            else:
                continue
            key = settings.settings_key(name)
            if key not in settings.HONOURED_SETTINGS:
                continue
            expr = next((kw.value for kw in node.keywords if kw.arg == "default"), None)
            if expr is None:
                value: str | None = None
            elif isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                value = expr.value
            else:
                text = ast.unparse(expr)
                if text not in COMPUTED_DEFAULTS:
                    unpinned.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} {text}")
                    continue
                value = COMPUTED_DEFAULTS[text]()
            defaults.setdefault(key, set()).add(value)
    return defaults, unpinned


def _key_lines(text: str) -> list[tuple[str, str]]:
    """The `key = value` lines of a settings file, commented or live, in file order."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        body = line[2:] if line.startswith("# ") else line
        key, sep, value = body.partition(" = ")
        if sep and key in settings.HONOURED_SETTINGS:
            out.append((key, value))
    return out


def _uncommented(text: str) -> str:
    """The file with every *key* line uncommented; the header stays a comment.

    Only a line whose body is `key = value` for an honoured key is uncommented, because the
    header is prose and uncommenting that would make the file unparseable -- which is the
    property under test, not a way to test it.
    """
    out: list[str] = []
    for line in text.splitlines():
        body = line[2:] if line.startswith("# ") else line
        key, sep, _ = body.partition(" = ")
        if line.startswith("# ") and sep and key in settings.HONOURED_SETTINGS:
            out.append(body)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


_listing_rows = settings.describe()
check(
    "the listing names every honoured key, once, in key order",
    [row.key for row in _listing_rows] == sorted(settings.HONOURED_SETTINGS),
    f"{len(_listing_rows)} rows",
)

use_file('vault = "/from/file"\nllm_endpoint = "http://file/v1"\n')
os.environ[settings.LLM_MODEL_ENV] = "from-env"
_layers = {row.key: row for row in settings.describe({settings.VAULT_ENV: "/from/flag"})}
check(
    "the listing records which layer supplied each value, one row at a time",
    _layers["vault"].value == "/from/flag"
    and _layers["vault"].source == settings.SOURCE_FLAG
    and _layers["llm_endpoint"].value == "http://file/v1"
    and _layers["llm_endpoint"].source == settings.SOURCE_FILE
    and _layers["llm_model"].value == "from-env"
    and _layers["llm_model"].source == settings.SOURCE_ENV
    and _layers["terms_dir"].value is None
    and _layers["terms_dir"].source == settings.SOURCE_DEFAULT,
    f"{_layers['vault']} {_layers['llm_endpoint']} {_layers['llm_model']} {_layers['terms_dir']}",
)

# The three credential keys are the whole secret set. The literal pin names them; the naming
# assertion is the part that keeps a *later* credential from becoming printable -- every honoured
# key ending in `_key` must be in SECRET_KEYS, so adding `new_api_key` to the surface fails here
# instead of printing its value. A credential named outside that convention (`*_token`) would
# still slip through: the convention is the property this can check, and the alternative was a
# comment claiming a guard the assertion did not provide.
check(
    "the listing redacts exactly the three credential keys, and every `*_key` key is one of them",
    settings.SECRET_KEYS == {"llm_key", "vlm_key", "openai_key"}
    and {k for k in settings.HONOURED_SETTINGS if k.endswith("_key")} == settings.SECRET_KEYS,
    f"secrets={sorted(settings.SECRET_KEYS)} "
    f"key_named={sorted(k for k in settings.HONOURED_SETTINGS if k.endswith('_key'))}",
)

use_file("")
for _name in (settings.LLM_KEY_ENV, settings.VLM_KEY_ENV, settings.OPENAI_KEY_ENV):
    os.environ[_name] = f"secret-{settings.settings_key(_name)}"
_secrets_text = settings.listing()
_secret_rows = {row.key: row for row in settings.describe()}
check(
    "a set credential is shown as redacted, and its value is nowhere in the output",
    "secret-" not in _secrets_text
    and _secret_rows["llm_key"].shown == "(redacted)"
    and _secret_rows["llm_key"].secret
    and _secret_rows["llm_key"].source == settings.SOURCE_ENV,
    repr(_secret_rows["llm_key"]),
)

use_file("")
_unset_rows = {row.key: row for row in settings.describe()}
check(
    "an unset credential reads as unset, not as redacted",
    _unset_rows["vlm_key"].shown == "(unset)"
    and _unset_rows["vlm_key"].value is None
    and _unset_rows["vlm_key"].secret,
    repr(_unset_rows["vlm_key"]),
)

# The defaults the listing prints are declared in `settings.py` and applied at the call sites,
# so they are re-read from the call sites here. `tests/test_deps.py` owns the other half of the
# same claim: that every honoured key has a `resolve()` reader at all.
_call_defaults, _unpinned = _resolve_call_defaults()
_table = {key: settings.default_for(f"DYAK_{key.upper()}") for key in settings.HONOURED_SETTINGS}
_disagreeing = {
    key: (sorted(str(value) for value in values), _table[key])
    for key, values in _call_defaults.items()
    if _table[key] not in values or values - {None, _table[key]}
}
check(
    "every default the listing prints is the default the call sites pass",
    not _disagreeing and not _unpinned,
    f"disagreeing: {_disagreeing}; unpinned: {_unpinned}",
)
check(
    "every honoured key has a call site, so the table covers the surface the code reads",
    set(_call_defaults) == settings.HONOURED_SETTINGS,
    f"with no call site: {sorted(settings.HONOURED_SETTINGS - set(_call_defaults))}",
)

# The file a first run writes. Naming a key is only useful if the name and the value are the
# ones the resolver reads, so the commented lines are uncommented and resolved back below.
use_file("")
_first_run = settings.write_settings(Path("/first/run/vault"))
_first_run_text = _first_run.read_text(encoding="utf-8")
_named = _key_lines(_first_run_text)
check(
    "the file a first run writes names every honoured key, exactly once",
    sorted(key for key, _ in _named) == sorted(settings.HONOURED_SETTINGS),
    f"{len(_named)} key lines for {len(settings.HONOURED_SETTINGS)} keys",
)

_live = [line for line in _first_run_text.splitlines() if line and not line.startswith("#")]
check(
    "exactly one line is live, and it is the vault the run wrote",
    _live == ['vault = "/first/run/vault"'],
    f"{_live}",
)

_uncommented_text = _uncommented(_first_run_text)
use_file(_uncommented_text)
_from_file = {
    key: settings.resolve(f"DYAK_{key.upper()}").value for key in sorted(settings.HONOURED_SETTINGS)
}
_expected = {**_table, settings.SETTINGS_KEY: "/first/run/vault"}
_wrong_file = {
    key: (value, _expected[key]) for key, value in _from_file.items() if value != _expected[key]
}
check(
    "uncommenting every key line yields a file that parses, and every key reads back as it names",
    not _wrong_file,
    f"{_wrong_file}",
)

# `dyak settings` is the command form of the listing: the acceptance for #243 is a way to *see*
# the surface, and a function no command calls is not one. It exits 0 -- a listing is not a
# finding, and whatever is wrong with the file was already reported on stderr by `main`.
use_file('vault = "/from/file"\n')
_cli_out = io.StringIO()
with redirect_stdout(_cli_out):
    _cli_code = dyc.main(["settings", "--vault-path", "/from/flag"])
_cli_text = _cli_out.getvalue()
check(
    "dyak settings prints the listing, honours its flags, and exits 0",
    _cli_code == ec.OK
    and "/from/flag" in _cli_text
    and f"{len(settings.HONOURED_SETTINGS)} keys" in _cli_text,
    f"exit={_cli_code}",
)

# --- report -------------------------------------------------------------------
_env.restore(SAVED_ENV)
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print(f"\nall checks passed ({len(settings.HONOURED_SETTINGS)} honoured keys)")
