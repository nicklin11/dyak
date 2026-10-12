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

# --- 5. the honoured-key set is derived from the constants the code defines ----
# #52 makes `settings.resolve` the one reader and `HONOURED_SETTINGS` the set of flat keys the
# settings file may carry. Both are claims about the code, and a second hand-kept list is the
# drift this repo keeps finding. Two directions, checked separately because they are different
# bugs:
#
#   * a `DYAK_*` constant the source defines but `HONOURED_SETTINGS` omits -> the file cannot
#     set a variable the code reads, and the resolver refuses a key the tool uses;
#   * an honoured key with no `resolve(...)` call site -> the file accepts a key that then
#     does nothing, which is the defect #156 and #52 both exist for.
#
# `DYAK_SETTINGS` is the one documented exception: it names the file being read, so the file
# cannot carry it and `settings_path` reads it directly.

RESOLVER = "resolve"


def string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so `resolve(ENDPOINT_ENV)` resolves."""
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


def imported_constants(tree: ast.Module, known: dict[str, str]) -> dict[str, str]:
    """`from .settings import NAME as ALIAS` bindings, resolved to the literal in `settings`.

    The constants live in one module, so every other module reaches them through an import.
    Without this the call-site harvest could not resolve `resolve(ENDPOINT_ENV)` and the
    guard would be vacuous.
    """
    consts: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.endswith("settings"):
            continue
        for alias in node.names:
            if alias.name in known:
                consts[alias.asname or alias.name] = known[alias.name]
    return consts


def defines_resolver(tree: ast.Module) -> bool:
    """True when this module defines its own `resolve` (stt/backends picks a backend)."""
    return any(isinstance(node, ast.FunctionDef) and node.name == RESOLVER for node in tree.body)


def shadows_resolver(tree: ast.Module) -> bool:
    """True when a bare `resolve` in this module is not `settings.resolve`.

    `stt/__init__.py` re-exports `backends.resolve` (the backend selector) and calls it; that
    is a different function with the same name, and counting its calls as settings reads would
    make this guard lie.
    """
    if defines_resolver(tree):
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.endswith("settings"):
            continue
        if any(alias.name == RESOLVER for alias in node.names):
            return True
    return False


def resolve_call_names(tree: ast.Module, known: dict[str, str]) -> tuple[list[str], list[int]]:
    """Every `resolve(...)` call's variable name, and the lines it could not resolve.

    A bare `resolve` is `settings.resolve` unless the module shadows it; `settings.resolve` /
    `dyd.resolve` are matched by attribute name.
    """
    consts = {**string_constants(tree), **imported_constants(tree, known)}
    bare_ok = not shadows_resolver(tree)
    names: list[str] = []
    unresolved: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            called = func.attr
        elif isinstance(func, ast.Name):
            called = func.id if bare_ok else ""
        else:
            continue
        if called != RESOLVER:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in consts:
            names.append(consts[arg.id])
        elif isinstance(arg, ast.Attribute) and arg.attr in known:
            names.append(known[arg.attr])
        else:
            unresolved.append(node.lineno)
    return names, unresolved


settings_tree = ast.parse(
    (PKG / "settings.py").read_text(encoding="utf-8"), filename=str(PKG / "settings.py")
)
settings_consts = string_constants(settings_tree)

live_names: set[str] = set()
resolved_names: dict[str, str] = {}
unresolved_calls: list[str] = []
for path in modules:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO).as_posix()
    # The constants, not every prose mention: `"DYAK_LLM_* pair"` in a message is not a
    # variable, and test_claims.py already holds the documentation to the full string set.
    for value in string_constants(tree).values():
        if re.fullmatch(r"DYAK_[A-Z_]+", value):
            live_names.add(value)
    names, unresolved = resolve_call_names(tree, settings_consts)
    for name in names:
        resolved_names.setdefault(name, rel)
    unresolved_calls += [f"{rel}:{line}" for line in unresolved]
# No dynamic URL names to add any more: #95 removed `DEFAULT_ENDPOINTS`, so
# `DYAK_WHISPERCPP_URL` and `DYAK_OPENAI_URL` are ordinary module-level constants in
# `settings.py` and the string-constant harvest above already sees both. The earlier
# regex derivation existed only because the names were assembled from the endpoint dict.

honoured_names = {f"DYAK_{key.upper()}" for key in dyd.HONOURED_SETTINGS}
check(
    "the constant set the honoured-key check runs against is populated",
    len(live_names) >= 15,
    f"only {len(live_names)} names known: {sorted(live_names)}",
)
check(
    "every DYAK_* constant the source defines is honoured, and only those",
    live_names == honoured_names | {dyd.SETTINGS_PATH_ENV},
    f"in code, not honoured: {sorted(live_names - honoured_names - {dyd.SETTINGS_PATH_ENV})}; "
    f"honoured, not in code: {sorted((honoured_names | {dyd.SETTINGS_PATH_ENV}) - live_names)}",
)
check(
    "every resolve() call site names a known variable",
    not unresolved_calls and set(resolved_names) <= honoured_names,
    f"unresolved: {unresolved_calls}; unknown: {sorted(set(resolved_names) - honoured_names)}",
)
check(
    "every honoured key has a resolve() call site, so no key is accepted and then ignored",
    set(resolved_names) == honoured_names,
    f"honoured with no reader: {sorted(honoured_names - set(resolved_names))}; "
    f"read but not honoured: {sorted(set(resolved_names) - honoured_names)}",
)

# --- 5b. no call site reads a DYAK_* variable behind the resolver's back ------
# `resolve` is the single precedence point only if every reader goes through it. A call site
# that reaches into `os.environ` for a `DYAK_*` variable has no file layer and offers no flag
# layer -- exactly the "no setting may bypass it" rule in #52. Non-setting variables
# (`XDG_STATE_HOME` in `bundle.py`) are still allowed, so this is about the variable's name,
# not about `os.environ` existing. `settings.py` is the resolver itself.


def raw_env_reads(tree: ast.Module, known: dict[str, str]) -> list[int]:
    """Lines that read a `DYAK_*` variable from `os.environ` / `os.getenv` directly."""
    consts = {**string_constants(tree), **imported_constants(tree, known)}
    lines: list[int] = []
    for node in ast.walk(tree):
        arg = None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"get", "pop"}:
                if isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
                    arg = node.args[0] if node.args else None
            elif isinstance(func, ast.Attribute) and func.attr == "getenv":
                if isinstance(func.value, ast.Name) and func.value.id == "os":
                    arg = node.args[0] if node.args else None
        elif isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "environ":
                arg = node.slice
        if arg is None:
            continue
        name = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            name = arg.value
        elif isinstance(arg, ast.Name) and arg.id in consts:
            name = consts[arg.id]
        if name and name.startswith("DYAK_"):
            lines.append(node.lineno)
    return lines


raw: list[str] = []
for path in modules:
    if path.name == "settings.py":
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO).as_posix()
    raw += [f"{rel}:{line}" for line in raw_env_reads(tree, settings_consts)]
check(
    "no module reads a DYAK_* variable except through settings.resolve (#52)",
    not raw,
    "; ".join(raw),
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

inert_result, inert_err = read_settings_with(f'{dyd.SETTINGS_KEY} = "/v"\nnot_a_setting = "x"\n')
check(
    "a key no setting reads warns, naming the key and the file (#156)",
    "not_a_setting" in inert_err
    and str(settings_file) in inert_err
    and "not a setting" in inert_err,
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
settings_file.write_text(f'{dyd.SETTINGS_KEY} = "/v"\nnot_a_setting = "x"\n', encoding="utf-8")
split_check = dyd.check_settings()
check(
    "doctor names the honoured and the ignored keys (#156)",
    split_check.ok
    and not split_check.fatal
    and dyd.SETTINGS_KEY in split_check.detail
    and "not_a_setting" in split_check.detail,
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
