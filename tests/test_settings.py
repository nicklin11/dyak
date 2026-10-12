#!/usr/bin/env python3
"""The settings file as a real third layer, and the one resolver (#52).

`settings.resolve` is the single place the order -- **flag > environment > settings file >
built-in default** -- is decided. This suite pins that order, the flat-key naming rule, the
derived honoured-key set, and the rejection of typos, then exercises the resolver through the
call sites that matter (vault, terms dir, output dir, work dir, both model roles, STT) rather
than only at the mechanism. A resolver that works and is not used is the defect #52 exists
for, so the last section asserts the file value actually reaches behaviour.

Isolated with `tests/_env.py`, so a developer's exported `DYAK_*` cannot answer these.
"""

import io
import os
import sys
import tempfile
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dyak import bundle as dyb  # noqa: E402
from dyak import deps as dyd  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402
from dyak import link as dylnk  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import runner  # noqa: E402
from dyak import settings  # noqa: E402
from dyak import workspace as dyw  # noqa: E402
from dyak.llm import Config  # noqa: E402
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

# --- report -------------------------------------------------------------------
_env.restore(SAVED_ENV)
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print(f"\nall checks passed ({len(settings.HONOURED_SETTINGS)} honoured keys)")
