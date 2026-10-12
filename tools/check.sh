#!/bin/sh
# The one command a contributor runs to get the same answer CI gets (#166).
#
# CI pins ruff; nothing pinned it locally, and the two builds disagree on the same tree.
# The host's 0.16.10 produced identical output on two trees the pin tells apart -- 123
# findings and zero E402 on both, one of which the pin passes and the other it flags -- so
# a local "ruff is clean" could not be told apart from a local "ruff is clean on the
# broken tree". PR #158 merged a red Lint step on exactly that reading.
#
# The pin is written down once, in `pyproject.toml`'s `dev` extra; this script reads it
# from there so the two cannot drift. It runs the pinned ruff, then every suite in
# `tests/` -- derived from the directory rather than hand-listed, because a hand-written
# list is how #173 happened: `test_llm.py` passed while no workflow ran it.
#
# Usage: tools/check.sh   (from anywhere -- paths resolve from the repository root)

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

# The pin, straight out of the file that owns it. `tomllib` is stdlib on 3.11+, which is
# the project's floor, so this needs nothing installed.
SPEC=$(
    python3 - <<'PY'
import pathlib
import tomllib

project = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
print(next(dep for dep in project["optional-dependencies"]["dev"] if dep.startswith("ruff")))
PY
)
PIN=${SPEC#ruff==}

# A ruff on PATH is used only when it is exactly the pin. Silently accepting a different
# build is the bug this script exists to prevent, so a mismatch falls through to pipx
# rather than producing a result that looks local-clean and is CI-red.
if command -v ruff >/dev/null 2>&1 &&
    [ "$(ruff --version 2>/dev/null | awk '{print $2}')" = "$PIN" ]; then
    echo "using ruff $PIN from PATH"
    set -- ruff
elif command -v pipx >/dev/null 2>&1; then
    echo "using ruff $PIN via pipx (a ruff on PATH is a different build, or absent)"
    set -- pipx run --spec "$SPEC" ruff
else
    echo "tools/check.sh: no ruff $PIN and no pipx; install the pin with:" >&2
    echo "    pip install -e \".[dev]\"" >&2
    exit 2
fi

echo "== lint =="
"$@" check src tests
"$@" format --check src tests

echo "== suites =="
# Globbing, not a hand-written list: #173 was a suite that passed while nothing ran it,
# and the explicit list is what let it go missing. A shared helper is named `_*.py`
# (`_env.py`), so `test_*.py` is exactly the set of suites.
for suite in tests/test_*.py; do
    python3 "$suite"
done

echo
echo "check passed: ruff $PIN and every suite in tests/"
