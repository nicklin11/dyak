"""Stage 10: repair what can be repaired mechanically, and say what was not.

ADR-0001 D5: repair is a proposal, never a silent rewrite. The original note is kept; this stage
writes `note.repaired.md` beside it and `repair.json` naming every change and every finding
it declined to touch. A repair the reader cannot audit is indistinguishable from corruption.

## What is repairable

Only findings whose correction is unambiguous and local:

- unbalanced `$` / `$$` -- close the delimiter, or escape a stray one
- an empty `$$` block -- delete it
- glossary drift -- substitute the canonical term

## What is not

Dimension mismatch, unsourced numbers, and every tier-2 critic finding. These need a
judgement about what the lecturer meant, and a mechanical substitution would produce a
plausible-looking note that is wrong in a new place. They are carried into `repair.json`
under `declined`, which is the point: the note is written either way, and the reader is told
exactly which parts of it are still suspect.

A section that declares itself empty and then is not is declined for the same reason, and
that is a revision of this module's own reading (#263). Either the marker is wrong or the
text is, and the two readings delete opposite things: "restore the honest marker" deleted
the lecture text and kept the marker, which is a judgement about what the lecturer meant
dressed as a formatting fix. Measured on `~/.local/state/dyak/занятие--2` before the
change: 3,334 characters of «2. Организационный блок» became the bare marker, the repaired
note re-linted clean, and the run's own `report.json` kept `ok: true` -- so the record said a
repair had happened while every character of that section's body was gone.

## Idempotence

Running repair twice changes nothing the second time. Stated as a test, because a repair
stage that appends a closing delimiter on every run would silently grow the note, and the
only symptom would be a note that gets worse every time the pipeline runs.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import audit as audit_mod
from . import lint as lint_mod
from . import llm

REPORT_NAME = "repair.json"
PROVENANCE_NAME = "repair-provenance.json"
REPAIRED_NOTE = "note.repaired.md"

#: Findings this stage will act on. Everything else is declined by default.
REPAIRABLE = frozenset(
    {
        "math/inline-unbalanced",
        "math/display-unbalanced",
        "math/empty",
        "term/drift",
    }
)

#: Findings that are deliberately out of reach, with the reason. Carried into the report.
DECLINED = {
    "dimension/mismatch": "a dimension error needs the intended equation, not a substitution",
    "dimension/undeclared": "the fix is a declaration the note's author did not write",
    "number/unsourced": "a number may be derived rather than spoken; deleting it loses content",
    "critic/finding": "a model's criticism is not evidence; a human decides",
    "structure/missing-section": "the material is absent, not the formatting",
    "asset/missing": "the frame is missing, not the reference",
    # Was repairable until #263, and the reason is the module's own test for a repair: the
    # marker and the section's text contradict each other, so "the correction" is a choice
    # between deleting the text and deleting the marker. `lint.py:384` is the linter's
    # whole-line test, and the finding it reports at `lint.py:392` is what a reader acts on;
    # repair declining is what keeps repair from quietly acting on the choice instead.
    # Measured: acting on it deleted 3,334 characters and the repaired note re-linted clean.
    "structure/claims-empty-but-is-not": (
        "the marker and the text contradict each other; deleting the text to keep the marker "
        "is a judgement about what the lecturer meant (#263)"
    ),
}


@dataclass
class Change:
    rule: str
    line: int
    before: str
    after: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "line": self.line, "before": self.before, "after": self.after}


@dataclass
class Report:
    changes: list[Change] = field(default_factory=list)
    declined: list[dict] = field(default_factory=list)
    original_bytes: int = 0
    repaired_bytes: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def summary(self) -> str:
        if not self.changes and not self.declined:
            return "nothing to repair"
        return f"{len(self.changes)} repaired, {len(self.declined)} declined"

    def as_dict(self) -> dict:
        return {
            "changed": self.changed,
            "original_bytes": self.original_bytes,
            "repaired_bytes": self.repaired_bytes,
            "repairable_rules": sorted(REPAIRABLE),
            "changes": [c.as_dict() for c in self.changes],
            "declined": self.declined,
        }


def _balance_inline(line: str) -> str:
    """Close an odd trailing `$`, or escape a stray one on an otherwise math-free line.

    A line with an odd count is either a delimiter that opened and never closed -- the
    common case, and the one that eats the rest of the note -- or a literal `$` that should
    have been escaped. Escaping is only safe when the line has no other math, so that is the
    condition, and a line with real math is closed instead.
    """
    count = len(re.findall(r"(?<!\\)\$", line))
    if count % 2 == 0:
        return line
    if "\\" in line or re.search(r"[A-Za-zА-Яа-я]", line):
        # Has real content: assume an unclosed delimiter and close it.
        return line + "$"
    return line.replace("$", "\\$")


def repair(
    markdown: str,
    audit_report: audit_mod.Report,
    glossary: audit_mod.Glossary,
    lint_report: lint_mod.Report | None = None,
) -> tuple[str, Report]:
    """Return the repaired markdown and what was done to it."""
    report = Report(original_bytes=len(markdown.encode("utf-8")))
    findings = list(audit_report.findings)
    if lint_report is not None:
        findings += lint_report.findings

    text = markdown

    for finding in findings:
        entry = {
            "rule": finding.rule,
            "line": finding.line,
            "detail": finding.detail,
            "reason": DECLINED.get(finding.rule, "not a mechanical fix"),
        }
        report.declined.append(entry)

    lines = text.splitlines()

    # 1. Unbalanced and empty math, by line. Line numbers come from the lint report, which is
    #    the only stage that produced them.
    if lint_report is not None:
        for finding in lint_report.findings:
            if finding.rule not in REPAIRABLE or finding.line == 0:
                continue
            index = finding.line - 1
            if not 0 <= index < len(lines):
                continue
            before = lines[index]
            if finding.rule == "math/inline-unbalanced":
                after = _balance_inline(before)
            elif finding.rule == "math/display-unbalanced":
                after = before + "$$" if before.count("$$") % 2 else before.replace("$$", "", 1)
            elif finding.rule == "math/empty":
                after = re.sub(r"\$\$\s*\$\$", "", before)
            else:
                continue
            if after != before:
                lines[index] = after
                report.changes.append(Change(finding.rule, finding.line, before, after))
                report.declined = [
                    d
                    for d in report.declined
                    if d["rule"] != finding.rule or d["line"] != finding.line
                ]

    # The math fixes above were applied to `lines`, not to `text`. Without this join every
    # delimiter repair was recorded in the report and then discarded -- the stage reported
    # "1 repaired" and returned a byte-identical note. Same shape as stage 5's move-then-read
    # and stage 6's work-dir paths: a write that never reached the thing it claimed to write.
    text = "\n".join(lines)

    # `splitlines`/`join` drops the trailing newline. Without this, a repair run with
    # nothing to repair returns a note that differs from its input in its last byte --
    # so the "original kept / nothing changed" promise is violated by a file that looks
    # identical until someone runs `git diff` and finds "\ No newline at end of file".
    if markdown.endswith("\n") and not text.endswith("\n"):
        text += "\n"

    # 2. Glossary drift: substitute the canonical term. The glossary holds confirmed forms
    #    only, which is what makes substitution safe here and not in stage 7. The variant is
    #    matched as a WHOLE WORD: the audit's own test is a substring one (`audit.py:189`) and
    #    it reports drift without correcting it (`audit.py:181`), so its over-matching costs a
    #    warning the reader judges, where the same over-match here rewrote «нормализация» into
    #    «НОРМАлизация» (#277). Measured on `~/.local/state/dyak/занятие--2/note.md` with a
    #    glossary built from `mscs/_terms`: a variant is a frontmatter `aliases:`/`forms:`
    #    bullet, the canonical is the note's H1 heading, and the length guard below leaves 122
    #    of the 132 bullets. The note holds 53 occurrences of those variants under this
    #    stage's case-insensitive match: 35 whole-word and 18 inside a longer word
    #    («функционала», «градиента»), which is the 18 the boundary is for. The entries that
    #    hit drop from 10 to 8, and the repaired note differs on 5 lines, each restoring the
    #    line the substring match had rewritten.
    for variant, canonical in glossary.entries:
        # The audit's guard (`audit.py:187`, "too short to match without false positives"),
        # kept: it cannot be what widens the rule to variants that were skipped before.
        if len(variant) < 4:
            continue
        # `(?<!\w) ... (?!\w)`, not `\b`: `\b` wants a word/non-word transition, so it refuses
        # to match a variant ending in a non-word character -- measured, `\bC\+\+\b` does not
        # match "C++ и" but does match inside "C++x" -- while the lookarounds ask only that
        # neither neighbour be a word character.
        pattern = re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)", re.IGNORECASE)
        hit = pattern.search(text)
        if hit:
            # The fix log records the changed LINE. "ilqf -> iLQR" is not before/after
            # text; the reader needs the sentence that was rewritten, because that is the
            # sentence they have to re-check against the recording.
            start = text.rfind("\n", 0, hit.start()) + 1
            end = text.find("\n", hit.end())
            end = len(text) if end == -1 else end
            line_no = text.count("\n", 0, start) + 1
            before_line = text[start:end]
            after_line = pattern.sub(lambda _m, value=canonical: value, before_line)
            report.changes.append(Change("term/drift", line_no, before_line, after_line))
        # A function, not a string. `re.sub` parses backslash escapes in a string
        # replacement, and a glossary canonical containing `\mathbf{x}` raises
        # "bad escape \m" -- a term catalogue is exactly where LaTeX belongs.
        new_text = pattern.sub(lambda _m, value=canonical: value, text)
        if new_text != text:
            text = new_text

    report.repaired_bytes = len(text.encode("utf-8"))
    return text, report


def run(
    note: Path,
    audit_report_path: Path,
    destination: Path,
    *,
    glossary_path: Path | None = None,
    lint_report_path: Path | None = None,
    stream=None,
    config: llm.Config | None = None,
) -> Report:
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    markdown = note.read_text(encoding="utf-8") if note.is_file() else ""

    areport = audit_mod.Report()
    if audit_report_path.is_file():
        payload = json.loads(audit_report_path.read_text(encoding="utf-8"))
        # Field by field, not `Finding(**f)`. The serialised form carries `key`, which is a
        # derived property and not a constructor argument, so splatting it raised
        # TypeError on the first finding -- which meant stage 10 crashed on every real run
        # and passed every test, because the test note happened to have no findings at all.
        areport.findings = [
            audit_mod.Finding(
                f["tier"],
                f["rule"],
                f["severity"],
                f["line"],
                f["detail"],
                f.get("quote", ""),
                f.get("citation", ""),
                f.get("confidence", ""),
            )
            for f in payload.get("findings", [])
        ]

    lreport = None
    if lint_report_path and lint_report_path.is_file():
        lreport = lint_mod.Report()
        payload = json.loads(lint_report_path.read_text(encoding="utf-8"))
        lreport.findings = [
            lint_mod.Finding(f["rule"], f["severity"], f["line"], f["detail"])
            for f in payload.get("findings", [])
        ]

    glossary = (
        audit_mod.Glossary.parse(glossary_path.read_text(encoding="utf-8"))
        if glossary_path and glossary_path.is_file()
        else audit_mod.Glossary()
    )

    repaired, report = repair(markdown, areport, glossary, lreport)
    (destination / REPAIRED_NOTE).write_text(repaired, encoding="utf-8")
    (destination / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / PROVENANCE_NAME).write_text(provenance(report, config) + "\n", encoding="utf-8")

    for change in report.changes[:8]:
        out.write(f"           fixed {change.rule} line {change.line}\n")
    declined_rules = sorted({d["rule"] for d in report.declined})
    if declined_rules:
        out.write(f"           declined: {', '.join(declined_rules)}\n")
    return report


def provenance(report: Report, config: llm.Config | None = None) -> str:
    return json.dumps(
        {
            "stage": 10,
            "tool": "dyak-repair-v1",
            # The #77 decision names four stage provenances, and this is the fourth: `endpoint`
            # is `Config.redacted()`, which carries the seed, so `repair-provenance.json` says
            # which sample the run was asked for like synth, audit and caption do. There is no
            # `prompt_sha256` here and that is deliberate (#257): this stage sends no prompt --
            # it repairs mechanically and declines with a reason -- so a digest of a prompt
            # would have nothing to cover. `config` is optional because a run without a model
            # has no sample to describe, which `requires_model: False` below says from the
            # other side; nothing in the repair decision depends on the endpoint.
            "endpoint": config.redacted() if config else None,
            "requires_model": False,
            "output": REPAIRED_NOTE,
            "original_kept": True,
            "repairable_rules": sorted(REPAIRABLE),
            "declined_reasons": DECLINED,
            "idempotent": True,
            "changes": len(report.changes),
        },
        indent=2,
        ensure_ascii=False,
    )
