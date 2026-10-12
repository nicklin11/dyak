#!/usr/bin/env python3
"""The remaining invariants from #48: claims that must match the code.

Issue #48 listed five checks and shipped one. Four were left, and one of the
four -- the stage-numbering one -- was written against an API that does not
exist: it asserted "every stage module declares STAGE = n", and exactly one of
twelve does. A check written from an assumption rather than from the source
produces either a false alarm or a check that silently guards nothing.

So each invariant below is derived from the thing it is supposed to describe,
and each states where its truth lives:

    stage numbering   pipeline.py's StageReport calls, and stages.IMPLEMENTED
    exit codes        exitcodes.DESCRIPTIONS
    repo tree         a literal allowlist, because the point is to catch surprises
    shipboard         the import graph, not the word
    suite wiring      tests/test_*.py, against ci.yml's run lines

The rule these share: a hand-maintained table in prose is a claim, and the only
thing that makes it true is a check that fails when it stops being true.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()


from dyak import audit  # noqa: E402
from dyak import bundle as dyb  # noqa: E402
from dyak import caption  # noqa: E402
from dyak import deps  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402
from dyak import frames  # noqa: E402
from dyak import lint  # noqa: E402
from dyak import link  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import probe  # noqa: E402
from dyak import quality  # noqa: E402
from dyak import repair  # noqa: E402
from dyak import report as dyro  # noqa: E402
from dyak import stages as gst  # noqa: E402
from dyak import synth  # noqa: E402
from dyak.stt import core as sttcore  # noqa: E402

README = (REPO / "README.md").read_text(encoding="utf-8")
PIPELINE = (REPO / "src" / "dyak" / "pipeline.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


# --- 2. stage numbering -------------------------------------------------------
# #48's version asserted `IMPLEMENTED` was a collection. It is an int. The real
# invariant is that the progress numbers the pipeline emits are 1..IMPLEMENTED,
# each exactly once -- a duplicate or a gap is the `[6/12] caption` bug where
# stage 11 printed the total for its own number.

reported = [int(n) for n in re.findall(r"StageReport\(\s*(\d+)", PIPELINE)]
check(
    "the pipeline reports every stage exactly once, with no gap",
    reported == list(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1)),
    f"reported {reported}, expected {list(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1))}",
)
check(
    "stages.IMPLEMENTED agrees with what the pipeline reports",
    len(reported) == gst.IMPLEMENTED,
    f"IMPLEMENTED={gst.IMPLEMENTED}, pipeline reports {len(reported)}",
)

# The README status table, *if it exists*, must have one row per stage plus doctor, and no
# stage number may appear twice. A duplicate is how "6" ended up on two rows.
#
# The check is conditional because the README no longer carries per-stage status. `c4aab27`
# cut it on the grounds that development state belongs in the issues, and this assertion was
# never updated to match -- it kept failing against a table that had been deleted on purpose,
# and a permanently red suite is a guard nobody reads. The invariant it protects still holds
# wherever such a table appears; what it no longer asserts is that one must appear.
#
# Scoped to the section: `^\| (\d+) \| ` matches the exit-code table too, and an
# unscoped scan of a document with three numeric tables reports all three.
if "| stage |" in README:
    status_start = README.index("| stage |")
    status_end = README.index("\n## ", status_start)
    status_rows = re.findall(r"^\| (\d+) \| ", README[status_start:status_end], flags=re.MULTILINE)
    check(
        "the README status table has one row per stage, plus doctor",
        sorted(int(n) for n in status_rows) == list(range(0, gst.IMPLEMENTED + 1)),
        f"stage column reads {status_rows}",
    )
else:
    check(
        "the README carries no per-stage status table, which the issues own",
        True,
        "the table exists and the conditional branch above will judge it",
    )

# --- 3. exit-code table -------------------------------------------------------
# The table is prose next to code. `DESCRIPTIONS` exists so the comparison does
# not depend on mapping constant names to codes by hand.
table_codes = {
    int(m) for m in re.findall(r"^\| (\d+) \| .+ \|$", README.split("## Exit codes")[1], flags=re.M)
}
check(
    "the README exit-code table lists exactly the codes exitcodes defines",
    table_codes == set(ec.DESCRIPTIONS),
    f"README {sorted(table_codes)}, code {sorted(ec.DESCRIPTIONS)}",
)

# --- 4. repo tree allowlist ---------------------------------------------------
# The file named `[` was a zero-byte artefact that rendered as blob/main/%5B.
ALLOWED_ROOT = {
    ".git",
    ".gitignore",
    ".github",
    ".pytest_cache",
    ".ruff_cache",
    "LICENSE",
    "README.md",
    "docs",
    "pyproject.toml",
    "src",
    "tests",
    "tools",
    # the committed graphify knowledge map (#116), regenerated on request
    "graphify-out",
    # caches are ignored by git but present in a working tree; they are expected
    # on disk and must never be committed, which the .gitignore check below covers
}
found_root = {p.name for p in REPO.iterdir()}
check(
    "the repository root contains only expected entries",
    found_root <= ALLOWED_ROOT,
    f"unexpected: {sorted(found_root - ALLOWED_ROOT)}",
)

tracked = {
    p.name
    for p in REPO.iterdir()
    if p.name in (".pytest_cache", ".ruff_cache") and (REPO / ".gitignore").exists()
}
gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
check(
    "caches on disk are gitignored, not committed",
    all(f"{name}/" in gitignore for name in tracked),
    f"present but possibly unignored: {sorted(tracked)}",
)

# The rule above reads the file and compares strings, which cannot see a pattern that
# covers nothing. `git check-ignore` asks git directly, on a path that need not exist, so
# this creates no files in the working tree.
#
# These three were all missing and all reachable by an ordinary command: `images/` is
# where stage 5 puts the frames, not the `frames/` the rule above names; `*.log` is where
# a redirected run writes; and the single-file zipapp build lands a binary named after the
# package at the repository root, which `ALLOWED_ROOT` would also reject. The bundle root
# is deliberately absent: `Bundle.open` (bundle.py:109-113,156) defaults to
# `$XDG_STATE_HOME/dyak/<lecture>`, never a CWD-relative `./output/`, so a run started in
# this tree leaves nothing behind.
MUST_BE_IGNORED = {
    "1_lecture_OCS/images/f_1.png": "the frames stage 5 publishes",
    "dyak-run.log": "a redirected run",
    "dyak": "the zipapp binary",
}
unignored = []
for candidate, _why in MUST_BE_IGNORED.items():
    rc = subprocess.run(
        ["git", "check-ignore", "-q", candidate],
        cwd=REPO,
        capture_output=True,
        check=False,
    ).returncode
    if rc != 0:
        unignored.append(candidate)
check(
    "a run's own outputs are gitignored",
    not unignored,
    f"git check-ignore accepted nothing for: {unignored}",
)

# --- 5. no shipboard ----------------------------------------------------------
# #48 wrote `grep -rn shipboard src/` must be empty. It is not, and should not
# be: backends.py records *why* the backend was removed. The invariant that
# matters is that nothing imports it.

offenders: list[tuple[str, int]] = []
for path in sorted((REPO / "src" / "dyak").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        name = ""
        if isinstance(node, ast.Import):
            name = ",".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            name = node.module
        if "shipboard" in name:
            offenders.append((path.relative_to(REPO).as_posix(), node.lineno))

check(
    "nothing imports the removed backend",
    not offenders,
    "; ".join(f"{p}:{ln}" for p, ln in offenders),
)

# --- 6. paths named in messages exist ------------------------------------------
# Three strings in `cli.py` named `audit/audit.json` and `repair/repair.json` for as long as
# they existed. Those are registry *keys*; `Bundle.publish` builds the destination from the
# source basename (`bundle.py:144`), so both files sit in the bundle root. The bundle has no
# `audit/` or `repair/` directory -- `ls` on the run-12 bundle confirms it. Both branches are
# exit 5 and exit 1, and thirteen runs exited 0, so the message was never shown. #91.
#
# The invariant, stated from the source rather than from a list of files: **the bundle root is
# flat, and `images/` is the only subdirectory.** Everything is published through
# `Bundle.publish`, which lands each file in the root under its own basename; only the frames
# are moved into `bundle.images` explicitly. So a message naming `<dir>/<artefact>` is wrong
# unless `<dir>` is `images/`.
CLI_SRC = REPO / "src" / "dyak" / "cli.py"
CLI_TREE = ast.parse(CLI_SRC.read_text(encoding="utf-8"), filename=str(CLI_SRC))

ARTEFACTS: set[str] = set()
for _mod in (audit, caption, frames, lint, link, quality, repair, dyro, synth):
    for _value in vars(_mod).values():
        if isinstance(_value, str) and _value.endswith((".json", ".md", ".tsv", ".txt")):
            ARTEFACTS.add(_value)
        elif isinstance(_value, (tuple, list, set, frozenset)):
            ARTEFACTS.update(
                v
                for v in _value
                if isinstance(v, str) and v.endswith((".json", ".md", ".tsv", ".txt"))
            )
ARTEFACTS |= set(dyro.REQUIRED) | set(dyro.OPTIONAL) | {sttcore.PROVENANCE_NAME, dyp.TIMINGS_NAME}

nested: list[str] = []
for _node in ast.walk(CLI_TREE):
    if isinstance(_node, ast.Constant) and isinstance(_node.value, str):
        for _prefix, _base in re.findall(r"([\w.]+)/([\w.-]+\.(?:json|md|tsv|txt))", _node.value):
            if _base in ARTEFACTS and _prefix != dyb.IMAGES:
                nested.append(f"cli.py:{_node.lineno} names {_prefix}/{_base}")

check(
    "no message names an artefact under a directory the bundle does not have",
    not nested,
    "; ".join(nested),
)

# The names themselves must exist, not just be flat: a message may not name an artefact no
# stage produces. Without this, an empty `ARTEFACTS` would make the check above vacuously true
# -- the same failure as the `total == sum` assertion that passed against a hardcoded zero.
check(
    "the artefact-name set the flatness check runs against is populated",
    len(ARTEFACTS) >= 20,
    f"only {len(ARTEFACTS)} names known, so the check above guards nothing: {sorted(ARTEFACTS)}",
)

# The flatness check above reads literals, and the fix for #91 removed the literals: the three
# strings now interpolate `{result.bundle.root / dya.REPORT_NAME}`, so `re.findall` over
# `cli.py`'s string constants matches **zero** of them. Measured, before writing this: 0 hits.
# A guard that matches nothing is not a guard, so the same invariant is now also asserted over
# attribute references, with the alias table `cli.py` actually imports.
CLI_ALIASES = {
    "dya": audit,
    "dyd": deps,
    "ec": ec,
    "dyb": dyb,
    "dylnk": link,
    "dyp": dyp,
    "dyr": repair,
}
#: The constant names a stage publishes under. Matching on the name rather than on the value is
#: deliberate: the value is what is being checked, and it is not visible in the AST.
ARTEFACT_CONSTANTS = (
    "REPORT_NAME",
    "PROVENANCE_NAME",
    "NOTE_NAME",
    "NOTE",
    "REPAIRED_NOTE",
    "LINKED_NOTE",
    "SYNTH_NOTE_NAME",
    "MANIFEST",
    "TIMINGS_NAME",
    "LLM_TRANSCRIPT_NAME",
)

referenced: list[tuple[int, str, str]] = []
for _node in ast.walk(CLI_TREE):
    if not isinstance(_node, ast.Attribute) or _node.attr not in ARTEFACT_CONSTANTS:
        continue
    if not (isinstance(_node.value, ast.Name) and _node.value.id in CLI_ALIASES):
        continue
    _module = CLI_ALIASES[_node.value.id]
    _value = getattr(_module, _node.attr, None)
    if isinstance(_value, str):
        referenced.append((_node.lineno, f"{_node.value.id}.{_node.attr}", _value))

unknown = [
    f"cli.py:{ln} names {expr} -> {value}, which no stage produces"
    for ln, expr, value in referenced
    if value not in ARTEFACTS
]
check(
    "no message names a constant whose artefact no stage produces",
    not unknown,
    "; ".join(unknown) or f"resolved {sorted({v for _, _, v in referenced})}",
)
#: Without a lower bound this section is the third check that would pass against an empty
#: input. `cli.py` interpolates at least the audit report and the repair report on the exit-5
#: and exit-1 branches.
check(
    "cli.py's messages do interpolate artefact constants, so the check above is not vacuous",
    len(referenced) >= 2,
    f"only {len(referenced)} artefact-constant references found in cli.py: {referenced}",
)
check(
    "and the messages name more than one artefact, each of them known",
    len({v for _, _, v in referenced}) >= 2 and {v for _, _, v in referenced} <= ARTEFACTS,
    f"referenced values: {sorted({v for _, _, v in referenced})}",
)

# --- 7. every stage announces itself -------------------------------------------
# ADR-0001 D7 rejected a progress bar that sits still through the dominant stage as "a lie
# about where the time went". **Seven** stages had no `[n/12]` line at all: 5, 6, 7, 8, 9, 10
# and 11 held their seconds in memory and printed nothing, so 700+ s of run 12's 1028 s produced
# no line. #97.
#
# Stage 12 is the one deliberate exception: `report.run` prints its own line immediately before
# the MISSING details it exists to justify, so a second line would repeat it. Both halves of
# that are asserted here, because "the exception" and "the bug" look identical from outside.
#
# Parsed, not grepped. A regex over the source matched the sentence inside the comment at
# stage 12 that explains the exception -- so the first version of this check believed stage 12
# printed a line and stage 8 did not. Stage 8 had not printed since it was written; the check
# found it, and a comment would not have. Comments are not in the AST.
PIPELINE_TREE = ast.parse(PIPELINE, filename="pipeline.py")

_stages: list[tuple[int, int]] = []
for _node in ast.walk(PIPELINE_TREE):
    if (
        isinstance(_node, ast.Call)
        and isinstance(_node.func, ast.Name)
        and _node.func.id == "StageReport"
        and _node.args
        and isinstance(_node.args[0], ast.Constant)
        and isinstance(_node.args[0].value, int)
    ):
        _stages.append((_node.lineno, int(_node.args[0].value)))
_stages.sort()


def _is_line(node: ast.AST) -> bool:
    """`reports[-1].line()` -- an Attribute call on a Subscript of `reports`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "line":
        return False
    inner = node.func.value
    return (
        isinstance(inner, ast.Subscript)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "reports"
    )


announced: set[int] = set()
announced_counts: dict[int, int] = {}
for _node in ast.walk(PIPELINE_TREE):
    if not _is_line(_node):
        continue
    _prior = [n for ln, n in _stages if ln <= _node.lineno]
    if _prior:
        announced.add(_prior[-1])
        announced_counts[_prior[-1]] = announced_counts.get(_prior[-1], 0) + 1

expected = set(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1))
#: The one documented exception, asserted as a literal so that removing the comment in
#: `pipeline.run` that explains it fails here rather than silently reintroducing a duplicate.
SILENT_BY_DESIGN = {gst.IMPLEMENTED}
check(
    "the pipeline prints a stage line for every stage but the documented exception",
    announced == expected - SILENT_BY_DESIGN,
    f"pipeline announces {sorted(announced)}, stages are {sorted(expected)}, "
    f"expected {sorted(expected - SILENT_BY_DESIGN)}",
)

REPORT_SRC = (REPO / "src" / "dyak" / "report.py").read_text(encoding="utf-8")
STAGE12_LINE = "[{stages.IMPLEMENTED}/{stages.IMPLEMENTED}] report"
check(
    "stage 12 announces itself from report.run, so no stage is silent",
    STAGE12_LINE in REPORT_SRC and announced | SILENT_BY_DESIGN == expected,
    f"report.py announces stage 12: {STAGE12_LINE in REPORT_SRC}; "
    f"covered {sorted(announced | SILENT_BY_DESIGN)} of {sorted(expected)}",
)

check(
    "every stage reaches the bundle's timings, silent or not",
    "write_timings(bundle, reports)" in PIPELINE,
    f"write_timings called after stage 12: {'write_timings(bundle, reports)' in PIPELINE}",
)

# --- 7b. one stage line per stage, spelled in exactly one place --------------------
# #101: a run printed 20 stage lines for 12 stages. Commit 5ef9dfa added
# `out.write(reports[-1].line())` for stages 5-11 to a pipeline where those seven stages
# already announced themselves from inside their own `run()`. The check above could not see
# it, and the reason is worth keeping in the source: `announced` is a set, and a set has no
# duplicates. Each stage *was* announced once by pipeline.py and once more by its own module,
# and both announcements collapsed into the same member.
#
# So what is counted here is the literal, not the call: a stage-prefixed string anywhere in
# the package. `ast.unparse` reassembles the placeholder text, so `[3/{IMPLEMENTED}]` is
# matched even though `{IMPLEMENTED}` is a FormattedValue and not part of any constant.
# Without it the regex would see only `"  [3/"` and stop looking.
STAGE_PREFIX = re.compile(r"\[[^\]]*IMPLEMENTED[^\]]*\]")


def stage_prefixed_literals(tree: ast.AST) -> list[str]:
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if (isinstance(node, ast.JoinedStr))
        or (isinstance(node, ast.Constant) and isinstance(node.value, str))
        if STAGE_PREFIX.search(ast.unparse(node))
    ]


_spelling: dict[str, list[str]] = {}
for _src in sorted((REPO / "src" / "dyak").glob("*.py")):
    _hits = stage_prefixed_literals(ast.parse(_src.read_text(encoding="utf-8"), filename=str(_src)))
    if _hits:
        _spelling[_src.name] = _hits

#: `StageReport.line()` is the formatter; `report.run` announces stage 12 because the
#: pipeline deliberately prints no line for it (see the comment in `pipeline.run`). Nothing
#: else may spell a stage prefix -- that is the whole of #101.
SPELLING_ALLOWED = {"pipeline.py", "report.py"}
check(
    "no stage announces itself: the prefix is spelled only where a line is formatted",
    not (set(_spelling) - SPELLING_ALLOWED),
    f"stage-prefixed literals outside {sorted(SPELLING_ALLOWED)}: "
    f"{ {k: len(v) for k, v in _spelling.items() if k not in SPELLING_ALLOWED} }",
)

#: Two in `pipeline.py` (the formatter and stage 3's progress line) and one in
#: `report.py`. A second formatter, or a stage announcing itself again, breaks this.
check(
    "the stage prefix is spelled three times in total: the formatter, stage 3's progress "
    "line, and stage 12",
    sum(len(v) for v in _spelling.values()) == 3,
    f"{ {k: len(v) for k, v in _spelling.items()} } -- expected 3 across the package",
)

#: The acceptance criterion "exactly 12 stage lines" would also be satisfied by deleting
#: progress output, which is the other thing #101 asked to preserve. Asserted so that a
#: future fix cannot buy the count by silencing the only line that says a 292 s stage began.
STT_PROGRESS = "starting"
check(
    "stage 3 still announces itself before the transcription, not only after",
    any(STT_PROGRESS in text for text in _spelling.get("pipeline.py", [])),
    f"no stage-prefixed pipeline literal mentions {STT_PROGRESS!r}: {_spelling.get('pipeline.py')}",
)

check(
    "the pipeline formats exactly one completion line per stage, with no stage twice",
    all(announced_counts.get(s) == 1 for s in range(gst.FIRST_STAGE, gst.IMPLEMENTED)),
    f"completion lines per stage: { {s: announced_counts.get(s, 0) for s in range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1)} }",
)

# --- 8. the configuration reference matches the code ---------------------------
# #98: `README.md` named env vars that no longer exist and omitted ones the code
# reads; `docs/running.md` is where the full table now lives, and it is held to
# the code the same way the exit-code table is held to `DESCRIPTIONS` -- parsed
# out of the source, not out of a hand-maintained list.
#
# Direction 1: every `DYAK_*` name that appears as a *string constant* in the
# source must appear in `docs/running.md`. Constants, not textual grep: a
# comment (the dead `DYAK_GATEWAY_URL` in `deps.py:377`) is not a live
# variable, and the docstrings that name one describe a real read. The one
# dynamic name, `f"DYAK_{name.upper()}_URL"`, resolves to `DYAK_OPENAI_URL`
# and `DYAK_WHISPERCPP_URL` against `DEFAULT_ENDPOINTS`; both also surface as
# literals in error messages the code can emit, but they are added explicitly so
# the set does not depend on the phrasing of a message.
RUNNING = (REPO / "docs" / "running.md").read_text(encoding="utf-8")

live_vars: set[str] = set()
for src_path in sorted((REPO / "src" / "dyak").rglob("*.py")):
    src_tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    for node in ast.walk(src_tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            live_vars.update(re.findall(r"DYAK_[A-Z_]+", node.value))
# the dynamically built names, matched against `DEFAULT_ENDPOINTS` keys
BACKENDS_SRC = (REPO / "src" / "dyak" / "stt" / "backends.py").read_text(encoding="utf-8")
backend_names = re.findall(r'^\s{4}"(\w+)":\s*"http', BACKENDS_SRC, flags=re.M)
for backend in backend_names:
    live_vars.add(f"DYAK_{backend.upper()}_URL")

missing_from_docs = sorted(v for v in live_vars if v not in RUNNING)
check(
    "every live DYAK_ variable is documented in docs/running.md",
    not missing_from_docs,
    f"in code, absent from running.md: {missing_from_docs}",
)
check(
    "the env-var set the check runs against is populated",
    len(live_vars) >= 15,
    f"only {len(live_vars)} names known: {sorted(live_vars)}",
)

# Direction 2: every `DYAK_*` the table names must be a live variable. The
# reference cannot rot into documenting aspirational configuration (#65, #68).
# The one exception is stated, not inferred from the prose around the name:
# `DYAK_GATEWAY_URL` appears only in the paragraph that records its removal,
# the same statement `deps.py:377` records in a comment.
REMOVED_VARS = {"DYAK_GATEWAY_URL"}
ghosts = sorted(
    m
    for m in set(re.findall(r"DYAK_[A-Z_]+", RUNNING))
    if m not in live_vars and m not in REMOVED_VARS
)
check(
    "docs/running.md names no variable the code does not read",
    not ghosts,
    f"in running.md, absent from code: {ghosts}",
)

# Every subcommand and flag registered in `cli.py` must be named in `running.md`,
# and the other way around an unregistered name must not sneak into the table.
registered: set[str] = set()
for node in ast.walk(CLI_TREE):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "add_parser" and node.args and isinstance(node.args[0], ast.Constant):
            registered.add(node.args[0].value)
        if (
            node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            if str(node.args[0].value).startswith("--"):
                registered.add(node.args[0].value)

absent_from_docs = sorted(f for f in registered if f not in RUNNING)
check(
    "every cli.py subcommand and flag is documented in docs/running.md",
    not absent_from_docs,
    f"registered, not in running.md: {absent_from_docs}",
)
check(
    "the registry the cli check runs against is populated",
    len(registered) >= 15,
    f"only {len(registered)} names known: {sorted(registered)}",
)

# --- 9. bundle.RUN_ARTEFACTS is the set `clear_root` deletes ------------------------
# `Bundle.clear_root` removes exactly the names in `RUN_ARTEFACTS` before each run, and that
# set is written as literals in `bundle.py` because importing the stage modules there would
# close an import cycle (`report.py` and `pipeline.py` import each other). Literals drift.
# Both directions are checked here, because the two failures are different bugs:
#
#   a name produced by a stage but absent from the set -> clear_root leaves the previous
#   run's file behind, which is exactly the defect #99 closed;
#   a name in the set that nothing produces -> `clear_root` deletes a file a user put in the
#   bundle root with `--output-dir`, which is the one thing it must never do.
BARE_LITERALS = {
    "transcript.json",
    "transcript.raw.json",
    "transcript.txt",
    "detect.json",
}
PRODUCED: set[str] = (
    # `REQUIRED["note"]` is a registry key, not a filename; the file it resolves to is
    # `synth.NOTE_NAME` below, and it is in `RUN_ARTEFACTS` as "note.md".
    (set(dyro.REQUIRED) | set(dyro.OPTIONAL)) - {"note"}
    | set(audit.ALL_ARTEFACTS)
    | set(synth.STAGE7_ARTEFACTS)
    | set(link.ALL_ARTEFACTS)
    | set(dyro.ALL_ARTEFACTS)
    | BARE_LITERALS
    | {
        probe.PROVENANCE_NAME,
        sttcore.PROVENANCE_NAME,
        frames.MANIFEST,
        synth.NOTE_NAME,
        synth.LLM_TRANSCRIPT_NAME,
        repair.REPAIRED_NOTE,
        link.LINKED_NOTE,
        dyro.SYNTH_NOTE_NAME,
        dyp.TIMINGS_NAME,
        dyp.RUN_STATE_NAME,
        # these five stages have no ALL_ARTEFACTS tuple, so their pair is named here
        caption.REPORT_NAME,
        caption.PROVENANCE_NAME,
        lint.PROVENANCE_NAME,
        quality.PROVENANCE_NAME,
        repair.PROVENANCE_NAME,
    }
)
unlisted = sorted(PRODUCED - dyb.RUN_ARTEFACTS)
check(
    "every artefact a run produces is in RUN_ARTEFACTS, or clear_root leaves it behind",
    not unlisted,
    f"not cleared between runs: {unlisted}",
)
unknown = sorted(dyb.RUN_ARTEFACTS - PRODUCED)
check(
    "RUN_ARTEFACTS names nothing a run does not produce, or clear_root eats a user's file",
    not unknown,
    f"would delete a file nothing wrote: {unknown}",
)
check(
    "the set is big enough to be the whole bundle",
    len(dyb.RUN_ARTEFACTS) >= 29,
    f"only {len(dyb.RUN_ARTEFACTS)} names",
)

# --- 9b. every lint rule has a fixture that trips it ---------------------------------
# #93's first acceptance item, stage-8 half: "a rule with no firing fixture is an unevidenced
# rule". The rationale is not that the rules are broken -- it is that 13 checks and 4 tier-1
# audit rules have never fired on a real note, so a clean run and a broken checker produce the
# same artefact. That is the shape #82 shipped in: a stage-5 gate that was a no-op for eight
# runs and whose report said nothing about it.
#
# "Named in a test" is a proxy for "fired in a test" -- a rule could be quoted by an assertion
# that expects it *not* to fire. It is the cheap half of the check and it is what keeps a new
# rule from arriving with no evidence at all; the firing fixture is the part a human reads.
#
# The rules are harvested by walking the AST, not by reading module attributes. They are not
# module-level constants: every one is a literal at the `report.add(...)` call site that emits
# it, so `vars(lint)` yields zero and the first version of this check passed vacuously.
_RULE_ID = re.compile(r"\A[a-z][a-z-]*/[a-z0-9-]+\Z")
STAGES_TESTS = (REPO / "tests" / "test_stages.py").read_text(encoding="utf-8")


def _rule_ids(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _RULE_ID.match(node.value)
    }


unevidenced: list[str] = []
all_rules: set[str] = set()
for _mod in (lint, audit):
    _rules = _rule_ids(Path(_mod.__file__))
    all_rules |= _rules
    for _rule in _rules:
        if f'"{_rule}"' not in STAGES_TESTS:
            unevidenced.append(f"{_mod.__name__}:{_rule}")

check(
    "every rule stage 8 and stage 9 can emit has a fixture in the stage tests",
    not unevidenced,
    f"no fixture trips these: {sorted(unevidenced)}",
)
check(
    "the rule set this check reads is populated",
    len(all_rules) >= 23,
    f"{len(all_rules)} rule ids harvested from lint.py and audit.py",
)

# --- 10. a dollar figure in the docs names its source, or says it has none -----------
# #96: ADR-0006 carried "~$0.0006 per frame on the measured gateway". No measurement of a
# gateway price exists in this repository -- not in any document, not in any artefact -- so
# the figure could not be derived from anything, and an ADR propagates whatever it is quoted
# as. It was flagged as unsourced in #86's review and merged anyway.
#
# The general rule, which is what stops the next one: a `$` amount in `docs/` must sit next to
# either a measurement it came from or an explicit statement that it was never measured.
# Everything else in these documents is quoted in tokens and seconds, because those are what
# the pipeline records in `caption_trace` and `timings.json`.
DOLLAR = re.compile(r"\$[0-9]+(?:\.[0-9]+)?")
#: Words that make an amount honest by declaring it an estimate. Deliberately exact phrases
#: rather than a regex over prose: a looser rule would start passing on wording nobody checked,
#: which is the failure this file exists to prevent.
COST_MARKERS = (
    "never measured",
    "no such measurement",
    "not measured",
    "unsourced",
    "assumption",
)

unsourced: list[str] = []
figures: list[str] = []
for _doc in sorted((REPO / "docs").rglob("*.md")):
    _lines = _doc.read_text(encoding="utf-8").splitlines()
    for _i, _line in enumerate(_lines):
        for _fig in DOLLAR.findall(_line):
            figures.append(f"{_doc.relative_to(REPO)}:{_i + 1} {_fig}")
            _window = "\n".join(_lines[max(0, _i - 3) : _i + 4]).lower()
            if not any(_marker in _window for _marker in COST_MARKERS):
                unsourced.append(f"{_doc.relative_to(REPO)}:{_i + 1} {_fig}")

check(
    "no dollar figure in docs/ is presented without a source or a disclaimer",
    not unsourced,
    "; ".join(unsourced) or f"checked {len(figures)} figures",
)
#: Four figures, each disclaimed. Zero would mean the regex stopped matching -- and the first
#: version of it did match `$.` inside the `$...$` LaTeX in `docs/stages.md` and
#: `docs/quality.md`, which would have produced a check that failed on nothing and then been
#: loosened into one that passes on nothing.
check(
    "the dollar-figure check actually finds figures to judge",
    len(figures) >= 4,
    f"only {len(figures)} found, so the check above guards nothing: {figures}",
)

# --- 11. every suite in tests/ is invoked by CI ---------------------------------------
# #173: `tests/test_llm.py` is a 330-line suite that passes and that no workflow ran. A
# suite nothing invokes cannot fail, so the invariant it holds (#61) was free to drift with
# `llm.py`. The workflow names suites by hand, which is how the omission happened, so this
# derives the truth from the directory instead and fails when the two disagree -- in either
# direction, so a removed suite cannot leave a stale `run:` line behind.
#
# Comment lines are dropped before the scan: a suite named in prose is not invoked, and
# counting it would let the next omission pass.
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ci_code = "\n".join(line for line in CI.splitlines() if not line.lstrip().startswith("#"))
expected_suites = sorted(f"tests/{p.name}" for p in (REPO / "tests").glob("test_*.py"))
invoked_suites = sorted(set(re.findall(r"tests/test_[A-Za-z0-9_]+\.py", ci_code)))
check(
    "ci.yml runs exactly the suites in tests/",
    invoked_suites == expected_suites,
    f"on disk {expected_suites}, ci.yml runs {invoked_suites}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all claim checks passed")
