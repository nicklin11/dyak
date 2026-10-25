#!/usr/bin/env python3
"""Make a test suite independent of the developer's shell.

The suites read `DYAK_*` through `deps.Config.from_env()` and `resolve_vault()`, which is
correct for the tool and wrong for a test: whatever the developer has exported changes what
the code under test sees. Two failures follow from that, both reproduced on pristine `main`
and filed as #63.

    AttributeError: '_HealthResponse' object has no attribute 'status'

`deps.check_llm` takes the real network path because `DYAK_LLM_ENDPOINT` happens to be
set, so the suite's own stub is never consulted -- and the crash happens before `check_llm`
returns, so the suite dies at module scope with zero checks printed rather than accumulating
failures. In `test_stages.py` the same cause shows up as

    AssertionError("expected NotConfiguredError")

because the ambient configuration is *working correctly* and the assertion cannot tell that
apart from a mistake. A suite whose result depends on the shell cannot gate anything; it is
green on CI only because CI exports nothing.

The fix is to clear the prefix rather than a hand-maintained list of names: #68 added
`DYAK_GATEWAY_URL`, and a scrub list would have missed it the same way it missed the last
four. Anything that is genuinely `DYAK_*` belongs in a test's own arrangement, set after
`isolate()` and therefore unaffected by it.
"""

from __future__ import annotations

import os
import tempfile

PREFIX = "DYAK_"

#: A settings path that does not exist, installed by `isolate()` for the same reason the
#: `DYAK_*` variables are scrubbed: `settings.resolve` reads `~/.config/dyak/settings.toml`
#: when no layer names one, so a developer's `llm_concurrency = 4` silently changed stage 7's
#: call order in the #117 test. A suite that reads the developer's config is not hermetic.
#: A test that wants the default path pops this variable; `test_settings` sets its own.
NO_SETTINGS = os.path.join(tempfile.gettempdir(), f"dyak-tests-no-settings-{os.getpid()}.toml")


def isolate() -> dict[str, str]:
    """Remove every `DYAK_*` variable and point the settings file at a non-existent path.

    The settings file is a second ambient layer, not a `DYAK_*` variable, so scrubbing the
    prefix alone left it readable.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith(PREFIX)}
    for key in saved:
        del os.environ[key]
    os.environ["DYAK_SETTINGS"] = NO_SETTINGS
    return saved


def restore(saved: dict[str, str]) -> None:
    """Undo `isolate()`, including anything a test added."""
    for key in [key for key in os.environ if key.startswith(PREFIX)]:
        del os.environ[key]
    os.environ.update(saved)


if __name__ == "__main__":
    # `python tests/_env.py` reports what a developer who runs the tool has configured.
    found = {k: v for k, v in sorted(os.environ.items()) if k.startswith(PREFIX)}
    print(f"{len(found)} DYAK_* variable(s) in the environment:")
    for key, value in found.items():
        print(f"  {key}={value}")
    if found:
        print("\nrun the suites through `env -i` or they are not trustworthy:")
        print('  env -i PATH="$PATH" HOME="$HOME" python tests/test_stages.py')
    raise SystemExit(0)
