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

import argparse
import ast
import importlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()


from dyak import audit  # noqa: E402
from dyak import bundle as dyb  # noqa: E402
from dyak import caption  # noqa: E402
from dyak import cli as dyc  # noqa: E402
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
from dyak import settings as dys  # noqa: E402
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

# `REMAINING_NOTE` outlived its own obituary, and each step is a different commit. `2f69998`
# created it as a multi-line expression ("stages 4-12 are not built yet (tracked in #3): ...")
# together with the run's exit line that quoted it, as the constant `PROCESS_INCOMPLETE` (that
# f-string) and the print of it; `1123489` rewrote the name into a one-line literal ("stages 8-12
# are not built yet ...") and edited the f-string's tail; `7b39e58` moved the value to `stages`,
# which is why `pipeline` re-exported it at all; and `21af2fc` -- the commit that built stage 12
# -- deleted `PROCESS_INCOMPLETE` and its print, the only reader the name had, and rewrote the
# value once more, to "every stage D3 lists is built". That deletion is what left the name
# defined, exported from `pipeline` and read by nothing (#215). With `IMPLEMENTED = 12` and no
# gap above, the state it described cannot occur: a "not built yet" line today is `_pending` for
# a *subcommand* (`cli.py:136-142`), not a stage. The name is gone; this is what catches its
# return, because a re-added constant that nothing prints is exactly the orphan #215 was.
STAGES = (REPO / "src" / "dyak" / "stages.py").read_text(encoding="utf-8")
check(
    "no orphaned remaining-stages constant in stages.py or pipeline.py",
    "REMAINING_NOTE" not in STAGES and "REMAINING_NOTE" not in PIPELINE,
    "a name nothing prints is back; either print it at the run's end line or delete it (#215)",
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

# --- 3b. exit 6 is described in one voice, at every site that restates it --------------
# #206 widened four descriptions to `exitcodes.DESCRIPTIONS[6]`'s meaning: code 6 is also
# produced by a run that stopped at the stage which needed the artefact, not only by stage
# 12's end-of-run check. The set check above cannot see a re-narrowing -- a narrowed
# description is still code 6 -- so the sentence is pinned here, per site.
#
# The marker is the widened half, not the whole sentence: the ADR appends its own
# "(ADR-0001 D3)" and `cli.py` rewords the first half, and one literal everywhere would pin
# prose rather than the claim. The old narrow wording -- "required artefacts could not be
# verified on disk" -- does not contain it.
CODE_6_WIDENED = "stopped at the stage"
CODE_6_DOCS = {
    "README.md": README,
    "docs/quality.md": (REPO / "docs" / "quality.md").read_text(encoding="utf-8"),
    "docs/adr/0001-lecture-pipeline.md": (
        REPO / "docs" / "adr" / "0001-lecture-pipeline.md"
    ).read_text(encoding="utf-8"),
}
CLI_LINES = (REPO / "src" / "dyak" / "cli.py").read_text(encoding="utf-8").splitlines()
CODE_6_DESCRIPTIONS = {
    name: [ln for ln in text.splitlines() if ln.startswith("| 6 |")]
    for name, text in CODE_6_DOCS.items()
}
# `cli.py` states it as a precedence note: the `#   6 -- ` line plus its continuation.
CODE_6_DESCRIPTIONS["src/dyak/cli.py"] = [
    f"{CLI_LINES[i].strip()} {CLI_LINES[i + 1].strip()}"
    for i in range(len(CLI_LINES) - 1)
    if CLI_LINES[i].strip().startswith("#   6 -- ")
]
for _site, _descriptions in CODE_6_DESCRIPTIONS.items():
    check(
        f"{_site} has exactly one exit-code-6 description to judge",
        len(_descriptions) == 1,
        f"{len(_descriptions)}: {_descriptions}",
    )
    check(
        f"{_site} describes exit 6 with the widened meaning, not stage 12 alone",
        len(_descriptions) == 1 and CODE_6_WIDENED in _descriptions[0],
        f"{_descriptions}",
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


# #194: this enumerated the working tree, so any untracked directory a tool dropped in the
# root failed the gate locally while CI -- a fresh clone -- stayed green. The invariant is
# the tracked tree: the `[` artefact this block was written for was tracked. The on-disk
# extras are judged by git's own ignore rules, the `git check-ignore` the block below uses.
def _tracked_root() -> set[str]:
    """The top-level component of every tracked path, from `git ls-files`."""
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout
    return {line.split("/", 1)[0] for line in listed.splitlines() if line}


def _ignored(name: str) -> bool:
    """Whether git ignores `name`, asked of git rather than read out of .gitignore."""
    asked = subprocess.run(
        ["git", "check-ignore", "-q", name], cwd=REPO, capture_output=True, check=False
    )
    return asked.returncode == 0


tracked_root = _tracked_root()
check(
    "git ls-files names the tracked tree",
    bool(tracked_root),
    "git ls-files returned nothing, so the checks below would be vacuous",
)
check(
    "the repository root's tracked entries are all expected",
    tracked_root <= ALLOWED_ROOT,
    f"tracked but unexpected: {sorted(tracked_root - ALLOWED_ROOT)}",
)

# On-disk entries git does not track. `ALLOWED_ROOT` covers the expected ones (the caches,
# and `.git`, which is a file in a linked worktree); a hidden directory is tool state -- the
# `.agent-teams/` that broke this is untracked and, unlike the caches, absent from
# .gitignore -- and everything else untracked and unignored is content this check exists to
# catch: a visible file or directory, or a hidden file such as `.env`. Adding
# `.agent-teams/` to .gitignore instead (#194's candidate 1) would hide the next tool's
# droppings, so the tolerance is the shape, not the name.
untracked_root = sorted({p.name for p in REPO.iterdir()} - tracked_root)
unexpected_root = [
    name
    for name in untracked_root
    if name not in ALLOWED_ROOT
    and not _ignored(name)
    and not (name.startswith(".") and (REPO / name).is_dir())
]
check(
    "untracked entries at the repository root are ignored or hidden tool state",
    not unexpected_root,
    f"untracked, unignored and not a hidden directory: {unexpected_root}",
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
    if not _ignored(candidate):
        unignored.append(candidate)
check(
    "a run's own outputs are gitignored",
    not unignored,
    f"git check-ignore accepted nothing for: {unignored}",
)

# --- 5. no shipboard ----------------------------------------------------------
# #48 wrote `grep -rn shipboard src/` must be empty. It was not, and should not have been: the
# removed backend's *why* lived in `stt/backends.py`. #259 retired the word, not that reason --
# the reason is still there (`backends.py:14-16`, "See issue #49") -- and the description of what
# the old wording named is ADR-0001's (`docs/adr/0001-lecture-pipeline.md:1060-1156`, already
# there at this change's base; this PR appends an amendment, it does not move the record).
# Two invariants, and they are different: nothing may import the removed backend (the graph
# below, which a word scan cannot see), and nothing in shipped code may carry the host-specific
# wording either (the rule after it).

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

# The word-level half of the same section (#259). A word scan is right for *this* invariant
# and wrong for the one above: the record that justifies the word lives in `docs/adr/`, so
# what remains in `src/` must name the variables to set and nothing about this host.
LORE = re.compile(r"shipboard|100\.64\.0\.1|10301|whisper-idle-stop", re.I)
word_offenders = [
    f"{p.relative_to(REPO).as_posix()}:{i}"
    for p in sorted((REPO / "src" / "dyak").rglob("*.py"))
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
    if LORE.search(line)
]
check(
    "no host-specific STT lore in shipped code (#259)",
    not word_offenders,
    "; ".join(word_offenders),
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

# --- 6b. one guarded reader per artefact ---------------------------------------
# #188/#201/#202: a malformed artefact must reach the user as a named reason and exit 6, not a
# traceback with Python's exit 1. The shape that makes that true is one guarded reader in the
# module that owns the artefact, and every consumer calling it. A second parse of the same file
# is a second place for the failure to escape -- and, for the transcript, a second read of a
# file that can change between the two. The behavioural half of these claims is in
# `tests/test_stages.py`; this is the half a behavioural test cannot see: that the parse is not
# still there underneath the call.
CAPTION_TREE = ast.parse((REPO / "src" / "dyak" / "caption.py").read_text(encoding="utf-8"))
SYNTH_TREE = ast.parse((REPO / "src" / "dyak" / "synth.py").read_text(encoding="utf-8"))
REPORT_TREE = ast.parse((REPO / "src" / "dyak" / "report.py").read_text(encoding="utf-8"))


def _funcs(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Calls spelled `name(...)` or `<anything>.name(...)` under `node`."""
    out: list[ast.Call] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            out.append(call)
    return out


def _parses(node: ast.AST, receiver: str) -> list[ast.Call]:
    """`json.loads(<receiver>.read_text(...))` calls under `node`: a second parse of one file."""
    out: list[ast.Call] = []
    for call in _calls(node, "loads"):
        for arg in call.args:
            for sub in ast.walk(arg):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "read_text"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == receiver
                ):
                    out.append(call)
    return out


_caption_funcs = _funcs(CAPTION_TREE)
_synth_funcs = _funcs(SYNTH_TREE)
_report_funcs = _funcs(REPORT_TREE)
check(
    "the functions the reader claims describe are the ones this section reads",
    {"align", "read_transcript", "read_alignments"} <= set(_caption_funcs)
    and "run" in _synth_funcs
    and "gate_disagreements" in _report_funcs,
    f"caption={sorted(_caption_funcs)} synth={sorted(_synth_funcs)} report={sorted(_report_funcs)}",
)
check(
    "#201: caption.align reads transcript.json once, through the guarded reader",
    len(_calls(_caption_funcs["align"], "read_transcript")) == 1
    and not _parses(_caption_funcs["align"], "transcript"),
    f"read_transcript={len(_calls(_caption_funcs['align'], 'read_transcript'))} "
    f"parses={len(_parses(_caption_funcs['align'], 'transcript'))}",
)
check(
    "#201: synth.run reads captions.json through caption.read_alignments, not json.loads",
    len(_calls(_synth_funcs["run"], "read_alignments")) == 1
    and not _parses(_synth_funcs["run"], "captions"),
    f"read_alignments={len(_calls(_synth_funcs['run'], 'read_alignments'))} "
    f"parses={len(_parses(_synth_funcs['run'], 'captions'))}",
)
check(
    "#201: report.gate_disagreements calls the same captions.json reader, not json.loads",
    len(_calls(_report_funcs["gate_disagreements"], "read_alignments")) == 1
    and not _parses(_report_funcs["gate_disagreements"], "path"),
    f"read_alignments={len(_calls(_report_funcs['gate_disagreements'], 'read_alignments'))} "
    f"parses={len(_parses(_report_funcs['gate_disagreements'], 'path'))}",
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

#: #186: the line used to read "whisper.cpp on CPU" whatever `stt.resolve` picked, and "on CPU"
#: was a claim about a machine this code never inspects -- `info().target` may name another
#: host. Pinned here as well as at runtime because the static form catches a literal coming back
#: in a path the runtime stubs do not exercise.
check(
    "#186: the hardcoded hardware claim cannot come back",
    "on CPU" not in PIPELINE,
    "pipeline.py still says 'on CPU'",
)

#: #186: the resolution must happen *before* the line, or the line can name a backend that is
#: not the one that runs (`stt/backends.py:129-134` falls back to the OpenAI-compatible route).
#: The runtime half -- that the line is flushed before the blocking call -- is in
#: tests/test_stages.py; this is the ordering claim, which a source read settles and a stub
#: cannot. `find`, not `index`: a missing literal must read as -1 and fail this check rather
#: than raise and take the rest of the suite with it.
RESOLVED_AT = PIPELINE.find("stt.backend().info()")
STAGE3_LINE_AT = PIPELINE.find("starting  {artefact.duration")
check(
    "#186: the backend is resolved before the stage-3 start line is written",
    0 <= RESOLVED_AT < STAGE3_LINE_AT,
    f"resolved at {RESOLVED_AT}, line at {STAGE3_LINE_AT}",
)

# --- #197: one renderer, one write site per loop ---------------------------------------
#: #197: the continuation line's shape, spelled once. `pipeline._chunk_line` delegates to
#: `stages.progress_line` and the three model-calling loops call it, so a second copy of the
#: format string -- which is how the four callers would drift apart -- fails here.
CONTINUATION = "           {unit} {done}/{total}"
_continuation_in = [
    str(path.relative_to(REPO))
    for path in sorted((REPO / "src" / "dyak").rglob("*.py"))
    if CONTINUATION in path.read_text(encoding="utf-8")
]
check(
    "#197: the continuation line is rendered in exactly one place",
    _continuation_in == ["src/dyak/stages.py"],
    f"spelled in {_continuation_in}",
)

#: The call each counting loop makes, and the blocking call it must precede. The unit word is
#: part of the claim: `sections` in two stages is the same word for the same reason -- a
#: SECTIONS entry and a note section are both "the next section" to a reader -- and stage 6's
#: `frames` is the frame it is about to caption.
_LOOPS = (
    ("caption.py", 'progress_line(out, "frames"', "reply = llm.chat("),
    ("synth.py", 'progress_line(out, "sections"', "outcome = synthesizer.section("),
    ("audit.py", 'progress_line(progress, "sections"', "reply = llm.chat("),
)
_loop_sources = {m: (REPO / "src" / "dyak" / m).read_text(encoding="utf-8") for m, _, _ in _LOOPS}
check(
    "#197: and every counting loop writes its line before that unit's blocking call",
    all(
        line in _loop_sources[m]
        and call in _loop_sources[m]
        and 0 <= _loop_sources[m].index(line) < _loop_sources[m].index(call)
        for m, line, call in _LOOPS
    ),
    str(
        {
            m: (
                _loop_sources[m].find(line),
                _loop_sources[m].find(call),
            )
            for m, line, call in _LOOPS
        }
    ),
)

check(
    # Stage 3's adapter, not a fourth spelling: the unit travels as an argument and the shape
    # stays in `stages.py`.
    "#197: and stage 3 delegates to it instead of spelling the shape again",
    'progress_line(out, "chunks"' in PIPELINE and "           chunks " not in PIPELINE,
    "pipeline.py still spells the continuation shape itself",
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
# comment (the dead `DYAK_GATEWAY_URL` in `deps.py:497`) is not a live
# variable, and the docstrings that name one describe a real read. There is no
# dynamically built name any more: #95 removed `DEFAULT_ENDPOINTS`, so
# `DYAK_OPENAI_URL` and `DYAK_WHISPERCPP_URL` are ordinary constants in
# `settings.py` and the constant harvest below already sees both.
RUNNING = (REPO / "docs" / "running.md").read_text(encoding="utf-8")

live_vars: set[str] = set()
for src_path in sorted((REPO / "src" / "dyak").rglob("*.py")):
    src_tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    for node in ast.walk(src_tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            live_vars.update(re.findall(r"DYAK_[A-Z_]+", node.value))

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
# the same statement `deps.py:497` records in a comment.
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

# Every subcommand and flag registered in `cli.py` must be named in `running.md`, and a flag
# the file names must be registered. A name is not documentation: #248, where the test was
# `f not in RUNNING` -- a substring of the whole file -- so `dyak settings` passed on the
# prose mentions of the settings layer while the command itself had no section, and
# `term link`, `term index` and `term check` passed on the word "term".
#
# The documented form is the one the file uses: a heading `## `dyak X``, or a usage line
# `dyak ... X` -- that is how `term`'s three children are documented, under their parent's
# section. A mention inside a sentence does not count; writing one where a command belongs
# is a deliberate act, which is the point of the check.
registered_subs: set[str] = set()
registered_flags: set[str] = set()
for node in ast.walk(CLI_TREE):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "add_parser" and node.args and isinstance(node.args[0], ast.Constant):
            registered_subs.add(node.args[0].value)
        if (
            node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            if str(node.args[0].value).startswith("--"):
                registered_flags.add(node.args[0].value)
registered = registered_subs | registered_flags

# argparse registers `--help` on every parser without an `add_argument` call, so it is a flag
# a reader may legitimately find in the file without it being registered here (#248).
IMPLICIT_FLAGS = {"--help"}
# Every `--flag` token the file names, so both directions read the file the same way.
doc_flags = set(re.findall(r"--[a-z][a-z0-9-]*", RUNNING))


def _named_as_command(name: str) -> bool:
    """Is `name` used as a command in running.md, rather than appearing as a word?"""
    return (
        re.search(
            rf"(?m)^(?:#+\s*)?`?dyak(?:[ \t]+[A-Za-z][\w-]*)*[ \t]+{re.escape(name)}\b",
            RUNNING,
        )
        is not None
    )


undocumented = sorted(n for n in registered_subs if not _named_as_command(n))
check(
    "every cli.py subcommand is named in docs/running.md as a command",
    not undocumented,
    f"registered, not named as a command in running.md: {undocumented}",
)
unnamed_flags = sorted(f for f in registered_flags if f not in doc_flags)
check(
    "every cli.py flag is named in docs/running.md",
    not unnamed_flags,
    f"registered, not in running.md: {unnamed_flags}",
)
ghost_flags = sorted(doc_flags - registered_flags - IMPLICIT_FLAGS)
check(
    "docs/running.md names no flag the cli does not register",
    not ghost_flags,
    f"in running.md, not registered in cli.py: {ghost_flags}",
)
check(
    "the registry the cli check runs against is populated",
    len(registered) >= 15,
    f"only {len(registered)} names known: {sorted(registered)}",
)

# --- 8b. the --vault-subdir default the help states is the one the export uses --------
# #183: the help rendered a bare "(default: <name>/)" -- "(default: glimpse/)" before
# c2824d7, "(default: dyak/)" after it -- while `Bundle.export_to_vault` appended the
# lecture name, so the help named the exact shared layout #118 removed: a shared directory
# made the second export silently replace the first. The layout is pinned by
# `#118: two lectures keep two notes (default subdir is per bundle)` in
# tests/test_stages.py; the sentence was not, and `--help` is the surface a user reads
# while deciding whether to pass the flag -- docs/running.md:100 states the same default,
# and section 8 pins only the flag's name.
#
# The citation is a check name rather than a line number on purpose: the assertion it named
# was at :2748 when this comment was written (27bae36) and has moved twice since -- :2778 at
# c237531, :2791 at c29b8a5 -- and a name is what a reader can grep. Pinned so a rename
# cannot leave the sentence pointing at nothing.
STAGES_TEXT = (REPO / "tests" / "test_stages.py").read_text(encoding="utf-8")
check(
    "the layout check section 8b names exists in test_stages.py",
    "#118: two lectures keep two notes (default subdir is per bundle)" in STAGES_TEXT,
    "the named check is gone: re-point the citation, do not delete this assertion",
)


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """The parser for a subcommand. `argparse` exposes no public accessor for this."""
    action = next(a for a in parser._actions if getattr(a, "dest", None) == "cmd")
    return action.choices[name]


PROCESS_PARSER = _subparser(dyc.build_parser(), "process")
VAULT_SUBDIR_HELP = next(a.help for a in PROCESS_PARSER._actions if a.dest == "vault_subdir")
#: Spelled out rather than derived from `DEFAULT_EXPORT_SUBDIR`: the point is that a change to
#: the constant or to the layout fails here, so a human re-reads the help and the docs row.
GOLDEN_SUBDIR_DEFAULT = "default: <vault>/dyak/<lecture>/, one directory per lecture, #118"
check(
    "--vault-subdir's help states the per-lecture default, not the bare subdir",
    GOLDEN_SUBDIR_DEFAULT in VAULT_SUBDIR_HELP and "default: dyak/)" not in VAULT_SUBDIR_HELP,
    VAULT_SUBDIR_HELP,
)
check(
    "the subdir the help names is the one DEFAULT_EXPORT_SUBDIR is interpolated into",
    f"<vault>/{dyb.DEFAULT_EXPORT_SUBDIR}/<lecture>/" in VAULT_SUBDIR_HELP,
    VAULT_SUBDIR_HELP,
)

# The help's `<lecture>` is not a placeholder to be trusted: it is the claim that a bundle
# lands in a directory of its own. `export_to_vault(subdir=None)` is the code that decides
# that, so the claim is checked against it rather than against the sentence it came from.
with tempfile.TemporaryDirectory() as _tmp:
    _bundle_root = Path(_tmp) / "bundle" / "lecture-1"
    _bundle_root.mkdir(parents=True)
    (_bundle_root / "note.md").write_text("note", encoding="utf-8")
    _vault = Path(_tmp) / "vault"
    _vault.mkdir()
    _bundle = dyb.Bundle(root=_bundle_root, artefacts={"note": _bundle_root / "note.md"})
    _bundle.export_to_vault(_vault)
    _landed = _vault / dyb.DEFAULT_EXPORT_SUBDIR / _bundle_root.name / "note.md"
    # Asserted inside the `with`: outside it the temporary vault is already gone, and a
    # missing file would read as a failed export rather than as a deleted fixture.
    check(
        "the per-lecture directory the help names is where export_to_vault lands a bundle",
        _landed.is_file(),
        f"expected {_landed}",
    )

#: Measured on the tree: `argparse`'s formatter wraps to the terminal width, and this sentence
#: breaks after "is" at `COLUMNS=80` while it is one line at `COLUMNS=200`. So the golden
#: substring is asserted against `action.help` (unwrapped) and this check normalises the render
#: to prove the same string actually reaches the user.
check(
    "the pinned sentence reaches the rendered help",
    GOLDEN_SUBDIR_DEFAULT in " ".join(PROCESS_PARSER.format_help().split()),
    PROCESS_PARSER.format_help(),
)

# --- 8c. the --help exit-code sentence names every cause the ladder returns -------------
# #238: the rendered `dyak --help` said "{4} if frames were soft" while `cli.py` returned 4 from
# two places. The second cause landed with #223's guard (`cli.py:442`, guarded on `considered`),
# which refuses the note when stage 6 found the audio and the video describing different
# lectures; the frame gate above it (`if not result.quality.ok`) is the older cause.
# `exitcodes.DESCRIPTIONS[4]` is generic and was already right, and the ADR row and
# docs/quality.md were updated with the gate -- only the help sentence was missed, because
# nothing read it.
#
# This is the check whose absence was the bug, and it is derived from the ladder rather than
# from a golden string: the guard of every `return ec.QUALITY_GATE_FAILED` in `main` is
# collected, and each one must be a cause the sentence names. A new return site fails here, a
# lost one fails here, and so does a reworded sentence that drops a cause and leaves a user
# whose A/V disagrees looking at their frame quality.
HELP_CAUSES = {
    # guard marker -> the words the help sentence must carry for that cause
    "quality.ok": "frames were soft",
    "caption.ok": "audio and video disagreed",
}


def _code_4_guards(tree: ast.AST) -> list[str]:
    """`ast.unparse` of the `if` test guarding every `return ec.QUALITY_GATE_FAILED`."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        returns_4 = any(
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Attribute)
            and stmt.value.attr == "QUALITY_GATE_FAILED"
            for stmt in node.body
        )
        if returns_4:
            out.append(ast.unparse(node.test))
    return out


CODE_4_GUARDS = _code_4_guards(CLI_TREE)
check(
    "the ladder has two code-4 return sites to judge",
    len(CODE_4_GUARDS) == 2,
    f"guards found: {CODE_4_GUARDS}",
)
check(
    "every code-4 return site in the ladder is a cause this check knows",
    all(any(marker in guard for marker in HELP_CAUSES) for guard in CODE_4_GUARDS),
    f"guards {CODE_4_GUARDS} do not all match {sorted(HELP_CAUSES)}: a new cause needs its "
    "wording in the --help sentence and an entry in HELP_CAUSES",
)

for _marker, _wording in HELP_CAUSES.items():
    _guard = next((g for g in CODE_4_GUARDS if _marker in g), None)
    check(
        f"the --help sentence names the code-4 cause behind `{_marker}`",
        _guard is not None and _wording in dyc.build_parser().description,
        f"guard={_guard!r} wording={_wording!r} description={dyc.build_parser().description!r}",
    )

check(
    "the --help sentence interpolates ec.QUALITY_GATE_FAILED, so its number follows the code",
    any(
        isinstance(node, ast.FormattedValue)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "QUALITY_GATE_FAILED"
        for node in ast.walk(CLI_TREE)
    ),
    "no {ec.QUALITY_GATE_FAILED} interpolation found in cli.py",
)
#: Asserted on the wrapped render too, not only on `parser.description`: nothing else proves the
#: sentence reaches the user, and argparse wraps at the terminal width -- so the two wordings
#: are looked for in the normalised text, the way section 8b does it.
HELP_RENDERED = " ".join(dyc.build_parser().format_help().split())
check(
    "both code-4 causes reach the rendered --help, not just the parser attribute",
    all(wording in HELP_RENDERED for wording in HELP_CAUSES.values()),
    HELP_RENDERED,
)

# --- 8d. every honoured key is documented with the default the resolver returns (#243) --------
# #243's `dyak settings` answers "what settings do we have" at a terminal; this is the same answer
# as a document, and it is re-derived here so it cannot go stale. Each cell comes from the thing
# that owns it:
#
#   * the set is `settings.HONOURED_SETTINGS`, which settings.py derives from its own `DYAK_*`
#     constants (`_ENV_NAMES`) -- not a list retyped into the doc, and `tests/test_deps.py` holds
#     that set to the source in both directions;
#   * the flat key is `settings.settings_key(env)`, the one mapping between the two names;
#   * the default is the `default=` argument at that key's own `resolve()` call site, evaluated in
#     the namespace of the module that passes it. `resolve()` carries no default of its own -- the
#     call site supplies it -- so a table built from `resolve(env).value` would read `None` in
#     every row and be wrong twenty times.
#
# `DYAK_SETTINGS` is the one honoured variable with no row: it names the file being read, so the
# file cannot carry it and `settings_path` reads it directly (settings.py:177).
#
# A default that is a path on this host is written `$HOME`/`$TMPDIR` in the doc, because the
# document has to read the same on another machine. The comparison normalises the evaluated value
# the same way, so that is a rule rather than a hand-kept list of exceptions.
SETTINGS_HEADING = "### Every honoured key and its default"


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so `resolve(ENDPOINT_ENV)` can be named.

    The same harvest `tests/test_deps.py` runs for its call-site check. The suites are standalone
    scripts, so the lines are repeated rather than imported from a file that runs a suite on
    import.
    """
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


def _imported_constants(tree: ast.Module, known: dict[str, str]) -> dict[str, str]:
    """`from .settings import NAME as ALIAS` bindings, resolved to the literal in settings.py."""
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


def _default_arguments() -> dict[str, set[tuple[str, str]]]:
    """`{env name: {(module, default source)}}`, one entry per `default=` a resolve() call passes.

    A call without `default=` is not recorded: that is the "unset means not configured" case, and
    the doc writes `none` for it.
    """
    sites: dict[str, set[tuple[str, str]]] = {}
    for src_path in sorted((REPO / "src" / "dyak").rglob("*.py")):
        module = ".".join(src_path.relative_to(REPO / "src").with_suffix("").parts)
        tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
        consts = {**_string_constants(tree), **_imported_constants(tree, SETTINGS_CONSTANTS)}
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
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                env = arg.value
            elif isinstance(arg, ast.Name) and arg.id in consts:
                env = consts[arg.id]
            elif isinstance(arg, ast.Attribute) and arg.attr in SETTINGS_CONSTANTS:
                env = SETTINGS_CONSTANTS[arg.attr]
            else:
                continue
            for keyword in node.keywords:
                if keyword.arg == "default":
                    sites.setdefault(env, set()).add((module, ast.unparse(keyword.value)))
    return sites


def _evaluated(module: str, source: str) -> str:
    """The default expression's value, evaluated where it is written, or `none` for `None`.

    `str(DEFAULT_VAULT)`, `str(DEFAULT_CONCURRENCY)` and `tempfile.gettempdir()` are the shapes
    that are not literals, and a hand-kept table of what they evaluate to is the drift this check
    exists to remove -- so the expression is evaluated in its own module's namespace. It is this
    repository's own source, read a few lines above; nothing here is user input.

    `None` is the resolver's own answer for a key no call site gives a default to, so an explicit
    `default=None` -- `dyak settings` passes one through a table (#247) -- reads the same as no
    `default=` at all. Without that the doc's `none` would disagree with the string "None".
    """
    value = eval(source, vars(importlib.import_module(module)))
    return "none" if value is None else str(value)


def _as_documented(text: str) -> str:
    """`text` with this host's home and temp directory written `$HOME`/`$TMPDIR`."""
    tokens = ((str(Path.home()), "$HOME"), (str(tempfile.gettempdir()), "$TMPDIR"))
    for path, token in sorted(tokens, key=lambda pair: len(pair[0]), reverse=True):
        text = text.replace(path, token)
    return text


SETTINGS_CONSTANTS = _string_constants(
    ast.parse((REPO / "src" / "dyak" / "settings.py").read_text(encoding="utf-8"))
)
SETTINGS_SECTION = RUNNING.split(SETTINGS_HEADING, 1)
documented_settings: dict[str, tuple[str, str]] = {}
if len(SETTINGS_SECTION) == 2:
    # The section ends at the next level-2 heading; a level-3 one inside it does not ("\n## "
    # needs the space, so "###" does not match).
    for line in SETTINGS_SECTION[1].split("\n## ", 1)[0].splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`DYAK_"):
            documented_settings[cells[0].strip("`")] = (cells[1].strip("`"), cells[2].strip("`"))

HONOURED_VARS = {f"DYAK_{key.upper()}" for key in deps.HONOURED_SETTINGS}
check(
    "every honoured setting has a row in the settings table, and only those (#243)",
    set(documented_settings) == HONOURED_VARS,
    f"in the table, not honoured: {sorted(set(documented_settings) - HONOURED_VARS)}; "
    f"honoured, no row: {sorted(HONOURED_VARS - set(documented_settings))}",
)
check(
    "the settings table is populated, so the check above is not vacuous (#243)",
    len(documented_settings) >= 15,
    f"{len(documented_settings)} rows",
)

# The resolver is asked with nothing set: this file isolates the environment at the top, and the
# settings *file* is pointed at an absent path, because a developer's own
# `~/.config/dyak/settings.toml` would otherwise supply half these values and the check would
# pass against the wrong layer.
DEFAULT_SITES = _default_arguments()
os.environ[deps.SETTINGS_PATH_ENV] = str(Path(tempfile.mkdtemp()) / "absent.toml")
settings_problems: list[str] = []
for _var, _key in sorted((f"DYAK_{key.upper()}", key) for key in deps.HONOURED_SETTINGS):
    # The env name is built from the key by the inverse of `settings_key`, and that inverse is the
    # whole mapping between the two name systems -- so it has to round-trip. `resolve` would raise
    # on a name that is not honoured, and a raise is not a finding a reviewer can read.
    if dys.settings_key(_var) != _key:
        settings_problems.append(
            f"{_var}: settings_key gives {dys.settings_key(_var)!r}, not {_key!r}"
        )
    _flat, _shown = documented_settings.get(_var, ("<no row>", "<no row>"))
    if _flat != _key:
        settings_problems.append(f"{_var}: row says {_flat!r}, key is {_key!r}")
    _values = {
        _as_documented(_evaluated(module, source))
        for module, source in DEFAULT_SITES.get(_var, set())
    }
    if len(_values) > 1:
        settings_problems.append(
            f"{_var}: the code passes more than one default: {sorted(_values)}"
        )
        continue
    _derived = next(iter(_values), "none")
    if _shown != _derived:
        settings_problems.append(f"{_var}: doc says {_shown!r}, the call site says {_derived!r}")
    _resolved = dys.resolve(_var, default=None if _derived == "none" else _derived)
    if _resolved.value != (None if _derived == "none" else _derived) or (
        _resolved.source != dys.SOURCE_DEFAULT
    ):
        settings_problems.append(
            f"{_var}: with nothing set resolve() gave {_resolved.value!r} from {_resolved.source!r}"
        )
os.environ.pop(deps.SETTINGS_PATH_ENV, None)
check(
    "every documented default is the one the resolver returns when nothing is set (#243)",
    not settings_problems,
    "; ".join(settings_problems) or f"{len(documented_settings)} rows agree",
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

# --- 10b. an amendment does not claim a rename to the same name --------------------
# #182: `docs/adr/0001-lecture-pipeline.md:64` read a self-rename until this fix -- "renamed
# `glimpse` → `glimpse`" from 0caec8b, "renamed `dyak` → `dyak`" from c2824d7. 0caec8b
# introduced the amendment with the names already collapsed -- it applied the rename it was
# performing to the sentence describing it -- and c2824d7 applied the next rename to what was
# left, so each rename made it worse and no single commit is at fault.
#
# A grep for an old name cannot be the guard. `git grep -n glimpse --
# docs/adr/0001-lecture-pipeline.md` returned nothing while the sentence was wrong: the
# corrupted text contains only the name that is current. And after the fix the same file
# *does* contain `glimpse`, so a name-presence check would fail on correct text. What is
# checkable is the relation between two names, which needs the shape of the sentence.
#
# The regex takes the arrow shape: `renamed `a` → `b``, the unbackticked mixes, and the ASCII
# `->` a terminal may substitute for `→`. Deliberately the arrow form only -- a prose rule
# ("renamed X to Y", no arrow at all) would start passing on wording nobody checked, which is
# what section 10's COST_MARKERS comment warns about. A reworded amendment is caught by the
# companion check below instead.
RENAME_ARROW = re.compile(r"renamed\s+`?([A-Za-z0-9_.-]+)`?\s*(?:→|->)\s*`?([A-Za-z0-9_.-]+)`?")
renames: list[tuple[str, str, str]] = []  # (where, old, new)
for _doc in sorted((REPO / "docs").rglob("*.md")):
    for _old, _new in RENAME_ARROW.findall(_doc.read_text(encoding="utf-8")):
        renames.append((str(_doc.relative_to(REPO)), _old, _new))
self_renames = [f"{where}: {old} -> {new}" for where, old, new in renames if old == new]
check(
    "no document claims a package was renamed to the name it already had",
    not self_renames,
    "; ".join(self_renames),
)
check(
    "the rename-shape check actually finds a rename to judge",
    renames,
    f"no `renamed X -> Y` sentence matched in docs/: {renames}",
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

# --- 12. the input's fingerprint is recorded, never verified (#94) ---------------------
# `source-provenance.json` now carries the input's `path`, `size_bytes` and `sha256`. Two
# claims in `probe.py`'s own docstrings are what keep that from being a liability, and both
# are checkable from the source rather than by running anything:
#
#   * **nothing verifies it.** The input is not in the bundle -- it can be moved, deleted, or
#     live on a USB disk that is gone by the time anyone reads the record -- so its digest can
#     only be written down, while stage 12 keeps verifying the bundle's own files by stat.
#     #79's transcript digest is checkable because the transcript *is* in the bundle; this one
#     is not. Requiring the document would fail bundles that are correct today, including
#     every bundle written before #94.
#   * **the digest is streamed, not read whole.** A lecture recording is hundreds of MB and can
#     be tens of GB; `read_bytes()` is the shape the two older `_digest` helpers have
#     (`stt/core.py`, `bundle.py`) and it is right for a transcript, wrong for the input.
PROBE_SRC = (REPO / "src" / "dyak" / "probe.py").read_text(encoding="utf-8")
# The claim is checked against the AST, not the text: `probe.py`'s own docstring names
# `read_bytes()` to say why that shape was rejected for the input, and a text scan would read
# the sentence explaining the choice as the choice. Same reason section 5 parses the import
# graph for its import invariant -- a word scan cannot see an import -- and #259 added the
# word-level rule beside it, where a scan is exactly the right instrument.
_probe_tree = ast.parse(PROBE_SRC)
probe_digest_calls = [
    node.attr
    for node in ast.walk(_probe_tree)
    if isinstance(node, ast.Attribute) and node.attr == "file_digest"
]
probe_whole_reads = [
    node.attr
    for node in ast.walk(_probe_tree)
    if isinstance(node, ast.Attribute) and node.attr == "read_bytes"
]
check(
    "the input's record stays optional, so stage 12 never requires the source (#94)",
    probe.PROVENANCE_NAME not in dyro.REQUIRED
    and probe.PROVENANCE_NAME not in dyro.REQUIRED_WHEN_VIDEO,
    f"REQUIRED={sorted(dyro.REQUIRED)} WHEN_VIDEO={sorted(dyro.REQUIRED_WHEN_VIDEO)}",
)
check(
    "the source digest is streamed, never read whole (#94)",
    probe_digest_calls and not probe_whole_reads,
    f"file_digest x{len(probe_digest_calls)}, read_bytes x{len(probe_whole_reads)}",
)
# --- 13. every line citation resolves ---------------------------------------------------
# #221: the tree cites code as `path:NNN`, and a citation whose line has moved points a reader
# at the wrong place. The class a checkout can see is narrower than that: a citation that
# points at nothing. Measured at 42b513a, where #221's decision took its census, the 60 tracked
# text files of 65 hold 34 citations: 32 resolve, 12 are range occurrences (7 distinct ranges),
# 0 name a basename two files share, 0 point past a file's last line, 0 are inverted, and 2
# resolve to nothing at all -- `docs/adr/0001-lecture-pipeline.md` cites shipboard's
# `whisper_wake_proxy.py` at line 152 and `whisper_idle_stop.sh` at line 21. Those 2 are
# skipped by design, because a check cannot fail on a path this repository does not own; the
# skip list and the live counts are printed by the check below on every run, so a third one is
# visible rather than silently tolerated.
#
# A citation resolves three ways, in this order: as a tracked path (`src/dyak/bundle.py`), as a
# unique tracked basename (`bundle.py`), or as the unique tail of a tracked path
# (`stt/core.py`, the module-relative shape the stage code uses). What is left over splits in
# two, and the split is why this check can be green today. A path whose first component is a
# tracked top-level entry claims a file *in this repository*, so if no tracked file answers to
# it the citation points at nothing and this fails: a deleted file, a rename, a typo. Anything
# else -- a bare name such as `whisper_idle_stop.sh`, or a path under a root this repository
# does not have -- is another repository's file, and is skipped.
#
# The ceiling, stated rather than implied: this catches 0 of the 5 drifts that motivated #221.
# Two are in tracked files, and neither is an existence failure -- the section-8b comment above
# named the layout assertion by line and the assertion moved twice under it, and #214's 257.2 s
# sentence carries no citation at all -- while three live in a review comment, a PR body and an
# issue comment, which a checkout cannot read. What it does guard is a citation that stops
# resolving: a path this repository no longer has, a line past EOF, an inverted range, a
# basename two files share.
#
# Deliberately not checked: the cited line's content. 21 of the 34 citations name a concept
# ("the frames-count line", "the loop") rather than a literal, so no tree-side rule can verify
# them; and for the 11 that carry one, "the literal appears on the line" is not the claim.
CITATION_SUFFIXES = (".py", ".md", ".sh", ".toml", ".json", ".yml", ".yaml", ".txt", ".tsv")
#: `P:N` or `P:N-M`, where `P` ends in a scanned suffix and no word character, `/`, `.` or `-`
#: precedes it. That is what keeps `#221`, `12:30`, `https://host:443`, `run_id:1` and
#: `key:value` out of the scan.
CITATION_RE = re.compile(
    rf"(?<![\w/.-])([A-Za-z0-9_./-]+(?:{'|'.join(re.escape(s) for s in CITATION_SUFFIXES)}))"
    r":(\d+)(?:-(\d+))?"
)
CITED = [
    line
    for line in subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout.splitlines()
    if line
]
CITED_TEXT = [name for name in CITED if name.endswith(CITATION_SUFFIXES)]
#: Read once: the text the scan walks is the text whose length it checks, so the second read a
#: separate line count would need is skipped. Measured here that read is ~6 ms of a ~50 ms scan
#: (ls-files ~1, read+split ~5, regex ~43) -- worth keeping, but not the reason this is cheap;
#: the regex pass is. A tracked file that is not on disk is left out of the map, so a citation
#: to it fails as a missing file rather than raising out of the suite.
CITED_LINES = {
    name: (REPO / name).read_text(encoding="utf-8", errors="replace").splitlines()
    for name in CITED_TEXT
    if (REPO / name).is_file()
}
#: basename -> the tracked text paths carrying it. A bare-name citation resolves only when
#: there is exactly one candidate (0 ambiguous today).
BY_BASENAME: dict[str, list[str]] = {}
for _name in CITED_LINES:
    BY_BASENAME.setdefault(_name.rsplit("/", 1)[-1], []).append(_name)
TOP_LEVEL = {name.split("/", 1)[0] for name in CITED}


def _citation_target(cited: str) -> tuple[str | None, str]:
    """The tracked path a citation names, plus why it names none.

    `(path, "")` when it resolves, `("", reason)` when it cannot and this check should fail,
    `(None, "")` when it names something outside this repository and is skipped.
    """
    if cited in CITED_LINES:
        return cited, ""
    if cited in CITED_TEXT:
        return "", f"{cited} is tracked but not on disk"
    candidates = BY_BASENAME.get(cited, [])
    if not candidates:
        candidates = [name for name in CITED_LINES if name.endswith("/" + cited)]
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return "", f"{len(candidates)} tracked files share that name"
    if cited.split("/", 1)[0] in TOP_LEVEL:
        return "", "no tracked file has that path"
    return None, ""


citations: list[str] = []
resolved: list[str] = []
skipped: list[str] = []
unresolved: list[str] = []
for _name, _lines in CITED_LINES.items():
    for _number, _line in enumerate(_lines, start=1):
        for _match in CITATION_RE.finditer(_line):
            first, last = int(_match.group(2)), _match.group(3)
            where = f"{_name}:{_number}"
            citations.append(_match.group(0))
            target, why = _citation_target(_match.group(1))
            if target is None:
                skipped.append(f"{where} -> {_match.group(0)}")
                continue
            if why:
                unresolved.append(f"{where} cites {_match.group(0)} -- {why}")
                continue
            count = len(CITED_LINES[target])
            end = int(last) if last else first
            if end < first:
                unresolved.append(
                    f"{where} cites {_match.group(0)} -- the range ends before it starts"
                )
            elif first < 1 or end > count:
                unresolved.append(f"{where} cites {_match.group(0)} -- {target} has {count} lines")
            else:
                resolved.append(f"{target}:{first}-{end}")

check(
    # The scan is the whole check, so a scan that read nothing or matched nothing would make
    # the assertion below true for the wrong reason.
    "#221: the citation scan reads the tracked text files and finds citations",
    bool(CITED_LINES) and bool(citations),
    f"{len(CITED_LINES)} files, {len(citations)} citations "
    f"({sum(1 for c in citations if '-' in c.split(':', 1)[1])} ranges), {len(resolved)} resolved, "
    f"{len(skipped)} not tracked here (skipped): {skipped}",
)
check(
    "#221: every `path:NNN` citation resolves to a real line in a real file",
    not unresolved,
    "; ".join(unresolved[:5]) if unresolved else f"{len(resolved)} resolved",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all claim checks passed")
