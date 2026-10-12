#!/usr/bin/env python3
"""Enforce ADR-0002 D2: third-party code may import numpy and nothing else.

ADR-0001 D2 is a decision record, and a decision record does not enforce itself. The
zipapp ships one file with nothing to resolve at start, which is exactly what a
stray `import requests` gives away -- silently, at runtime, on someone else's
machine, after the fact. This script is the executable form of that sentence.

It walks the AST of every module under `src/` and reports any import that is
neither standard library nor numpy. Two deliberate properties:

  * AST, not text. A match on the string "requests" inside a docstring or a
    comment is not an import, and a check that cannot tell the difference will
    be disabled the first time it produces a false positive.
  * `sys.stdlib_module_names`, not a hand-kept list. A hand-kept list is a
    fourth table of facts in this repo, and every one of them has drifted.

It also cross-checks `pyproject.toml` against the source: a dependency that is
declared but never imported, or imported but never declared, is the same drift
in the other direction, and the zipapp build depends on both halves agreeing.
"""

import ast
import io
import os
import re
import sys
import tempfile
import tomllib
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
PKG = SRC / "dyak"

# The package under test, for the settings-feedback checks below (#156). Imported after the
# env isolation above so the module's own `DYAK_*` reads are not the developer's.
sys.path.insert(0, str(SRC))
from dyak import deps as dyd  # noqa: E402

# The one permitted third-party import, and the reason it is permitted.
ALLOWED = {"numpy"}
# The package under test. Everything below `src/dyak/` may import it freely;
# treating it as third-party would flag every module in the codebase.
SELF = {"dyak"}
# Sibling modules in `tests/` itself. `tests/_env.py` is not a dependency the zipapp
# would have to ship -- it is the file next door, which is a different property. Before
# #63's fix each suite duplicated the env handling inline; sharing it means the suites now
# import a local module, and this rule would otherwise report three of its own files.
SIBLINGS = {path.stem for path in (REPO / "tests").glob("*.py")} - {"test_deps"}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def top_level(name: str) -> str:
    """`a.b.c` -> `a`. Relative imports have no root and return ''."""
    return name.split(".", 1)[0]


def imports_of(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module named by an import statement, as (top-level name, line).

    Relative imports (`from .x import y`) are skipped outright. For those,
    `ImportFrom.module` is only the *tail* -- `from .cli import main` yields
    "cli", not "dyak.cli" -- so it names a module that does not exist at any
    absolute path. And `level > 0` cannot escape the package by definition,
    which is exactly what this check is about.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((top_level(alias.name), node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((top_level(node.module), node.lineno))
    return found


# --- 1. no import outside stdlib + numpy -------------------------------------

modules = sorted(PKG.rglob("*.py"))
check("there are modules to scan", bool(modules), f"{len(modules)} files")

imported: set[str] = set()
offenders: list[tuple[str, int, str]] = []

for path in modules:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for name, lineno in imports_of(tree):
        if not name or name in sys.stdlib_module_names or name in ALLOWED or name in SELF:
            if name:
                imported.add(name)
            continue
        offenders.append((path.relative_to(REPO).as_posix(), lineno, name))

check(
    "no third-party import outside numpy",
    not offenders,
    "; ".join(f"{p}:{ln} imports {n}" for p, ln, n in offenders),
)

# --- 2. numpy is actually used -----------------------------------------------
# The opposite drift: ADR-0001 D2 permits numpy, so a future edit that drops the import
# leaves a declared dependency nobody needs, and the zipapp pays for it.
check(
    "numpy is imported somewhere, so the declared dependency is real",
    "numpy" in imported,
    f"imported: {', '.join(sorted(imported & ALLOWED)) or 'nothing'}",
)

# --- 3. pyproject and the source agree ---------------------------------------

declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
declared_deps = {
    top_level(re.split(r"[<>=!~\[ ]", spec, maxsplit=1)[0])
    for spec in declared["project"]["dependencies"]
}
check(
    "pyproject declares exactly numpy",
    declared_deps == ALLOWED,
    f"declared: {sorted(declared_deps)}",
)
check(
    "every declared dependency is actually imported",
    declared_deps <= imported,
    f"declared but unused: {sorted(declared_deps - imported)}",
)

requires_python = declared["project"].get("requires-python", "")
check(
    "requires-python pins the floor the CI matrix tests", "3.11" in requires_python, requires_python
)

# --- 4. the scripts under tests/ hold themselves to the same rule ------------
# They import the package under test and the stdlib. If one ever grows a real
# dependency, it is still a dependency the zipapp does not ship.

for path in sorted((REPO / "tests").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [
        (path.relative_to(REPO).as_posix(), lineno, name)
        for name, lineno in imports_of(tree)
        if name and name not in sys.stdlib_module_names and name not in ALLOWED | SELF | SIBLINGS
    ]
    check(
        f"tests/{path.name} imports no dependency outside numpy",
        not bad,
        "; ".join(f"{p}:{ln} imports {n}" for p, ln, n in bad),
    )

# --- 5. the honoured-key set cannot drift from the code that reads it ---------
# #156 makes `read_settings` warn about every key outside `HONOURED_SETTINGS`, so that set is
# a claim about which keys the tool reads. A second hand-kept list is exactly the drift this
# repo keeps finding, so the claim is derived from the source: every `read_settings().get(...)`
# call site, with module-level string constants resolved, is compared against the set. Add a
# reader without listing its key and the warning starts lying about a key the tool uses; list
# a key nothing reads and it lies in the other direction.

SETTINGS_READER = "read_settings"


def string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so `get(SETTINGS_KEY)` resolves."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


def settings_read_keys(tree: ast.Module) -> tuple[list[str], list[int]]:
    """Every key a `read_settings().get(...)` call consults, and the lines it could not resolve."""
    consts = string_constants(tree)
    keys: list[str] = []
    unresolved: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get" or not node.args:
            continue
        inner = node.func.value
        if not isinstance(inner, ast.Call):
            continue
        callee = inner.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if name != SETTINGS_READER:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            keys.append(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in consts:
            keys.append(consts[arg.id])
        else:
            unresolved.append(node.lineno)
    return keys, unresolved


consulted: dict[str, str] = {}
unresolved_keys: list[str] = []
for path in modules:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    keys, unresolved = settings_read_keys(tree)
    rel = path.relative_to(REPO).as_posix()
    for key in keys:
        consulted.setdefault(key, rel)
    unresolved_keys += [f"{rel}:{line}" for line in unresolved]

check(
    "the reader set the drift check runs against is populated",
    bool(consulted),
    f"no read_settings().get() call site found, so the check guards nothing: {sorted(consulted)}",
)
check(
    "every read_settings() key is a literal or a resolvable constant",
    not unresolved_keys,
    "; ".join(unresolved_keys),
)
check(
    "the honoured-key set matches the keys the code actually reads",
    set(consulted) == set(dyd.HONOURED_SETTINGS),
    f"read: {sorted(consulted)}; honoured: {sorted(dyd.HONOURED_SETTINGS)}",
)

# --- 6. the settings file reports what it will not honour (#156) --------------
# The file is hand-edited -- `SETTINGS_TEMPLATE` says "edit by hand" -- while `read_settings`
# used to return every string pair and only `vault` was ever consulted. A key that did
# nothing, a value silently dropped for not being a string, and a typo'd key were all
# invisible, and the consequence was a run exporting into the wrong vault. These pin the
# feedback loop, the acceptance criterion that a correct file stays quiet, and that none of
# it moves behaviour.
settings_file = Path(tempfile.mkdtemp()) / "settings.toml"
os.environ[dyd.SETTINGS_PATH_ENV] = str(settings_file)


def read_settings_with(body: str) -> tuple[dict[str, str], str]:
    """Write `body` as the settings file, then return (read result, report stderr)."""
    settings_file.write_text(body, encoding="utf-8")
    buf = io.StringIO()
    with redirect_stderr(buf):
        dyd.report_settings()
        result = dyd.read_settings()
    return result, buf.getvalue()


clean_result, clean_err = read_settings_with(f'{dyd.SETTINGS_KEY} = "/v"\n')
check(
    "an honoured key is read back",
    clean_result == {dyd.SETTINGS_KEY: "/v"},
    repr(clean_result),
)
check(
    "a settings file holding only honoured keys produces no warning (#156)",
    clean_err == "",
    repr(clean_err),
)

inert_result, inert_err = read_settings_with(f'{dyd.SETTINGS_KEY} = "/v"\nstt = "whispercpp"\n')
check(
    "a key no setting reads warns, naming the key and the file (#156)",
    "stt" in inert_err and str(settings_file) in inert_err and "not a setting" in inert_err,
    repr(inert_err),
)
check(
    "an inert key changes no behaviour: the honoured key still resolves (#156)",
    inert_result.get(dyd.SETTINGS_KEY) == "/v" and dyd.resolve_vault() == Path("/v"),
    f"read={inert_result!r} vault={dyd.resolve_vault()}",
)

typed_result, typed_err = read_settings_with(f"{dyd.SETTINGS_KEY} = 42\n")
check(
    "a non-string value warns with the key, its TOML type and the file (#156)",
    dyd.SETTINGS_KEY in typed_err and "number" in typed_err and str(settings_file) in typed_err,
    repr(typed_err),
)
check(
    "a non-string value is still dropped, not stringified, and behaves as unset (#156)",
    typed_result == {} and dyd.resolve_vault() == dyd.DEFAULT_VAULT,
    f"read={typed_result!r} vault={dyd.resolve_vault()}",
)

typo_result, typo_err = read_settings_with('vault_path = "/x"\n')
check(
    "a likely typo of an honoured key is named as one (#156)",
    "vault_path" in typo_err and dyd.SETTINGS_KEY in typo_err and "did you mean" in typo_err,
    repr(typo_err),
)
check(
    "a typo'd key behaves as unset, which is the confusion the warning exists for (#156)",
    typo_result == {"vault_path": "/x"} and dyd.resolve_vault() == dyd.DEFAULT_VAULT,
    f"read={typo_result!r} vault={dyd.resolve_vault()}",
)

malformed_result, malformed_err = read_settings_with(f"{dyd.SETTINGS_KEY} = = broken\n")
check(
    "malformed TOML is still reported by name and yields nothing (#156)",
    malformed_result == {} and "malformed" in malformed_err and str(settings_file) in malformed_err,
    repr(malformed_err),
)

absent_file = Path(tempfile.mkdtemp()) / "no-settings.toml"
os.environ[dyd.SETTINGS_PATH_ENV] = str(absent_file)
absent_check = dyd.check_settings()
check(
    "doctor reports the settings path when the file is absent, and does not fail (#156)",
    absent_check.ok
    and not absent_check.fatal
    and absent_check.path == str(absent_file)
    and "absent" in absent_check.detail,
    repr(absent_check),
)

os.environ[dyd.SETTINGS_PATH_ENV] = str(settings_file)
settings_file.write_text(f'{dyd.SETTINGS_KEY} = "/v"\nstt = "x"\n', encoding="utf-8")
split_check = dyd.check_settings()
check(
    "doctor names the honoured and the ignored keys (#156)",
    split_check.ok
    and not split_check.fatal
    and dyd.SETTINGS_KEY in split_check.detail
    and "stt" in split_check.detail,
    split_check.detail,
)

settings_checks = [c for c in dyd.run_all() if c.name == "settings"]
check(
    "the settings report is part of run_all, so doctor prints it (#156)",
    len(settings_checks) == 1,
    f"{len(settings_checks)} settings checks in run_all",
)
check(
    "the settings check is never fatal, so a warning cannot move an exit code (#156)",
    all(c.ok and not c.fatal and not c.failed for c in settings_checks),
    "; ".join(repr(c) for c in settings_checks),
)

os.environ.pop(dyd.SETTINGS_PATH_ENV, None)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all dependency checks passed")
