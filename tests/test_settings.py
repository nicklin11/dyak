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
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dyak import bundle as dyb  # noqa: E402
from dyak import deps as dyd  # noqa: E402
from dyak import link as dylnk  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
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

use_file('bbox_source = "null"\n')
check(
    "stage 5's bbox source comes from the file",
    dyp.resolve(settings.BBOX_SOURCE_ENV, default="geometric").value == "null",
    repr(dyp.resolve(settings.BBOX_SOURCE_ENV, default="geometric")),
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
