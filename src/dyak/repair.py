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


def _fold_shape(text: str) -> str:
    """Case-fold and drop every non-alphanumeric character: the shape G4 compares."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _guard_canonical_on_line(line: str, match: re.Match[str], canonical: str) -> bool:
    """G1: the canonical is already on the line, case-insensitively -> decline.

    This is `check_term_drift`'s own precondition (`audit.py:189`, "the canonical is not
    there") applied to the writer, so the rewrite can never be bolder than the report that
    describes it, and it is what makes a record whose `before` and `after` are equal
    impossible. On the real note it is the first applicable guard for 46 of the 53
    occurrences the pre-#277 rule can see.
    """
    return canonical.lower() in line.lower()


def _guard_lowercase_cyrillic(line: str, match: re.Match[str], canonical: str) -> bool:
    """G2: an all-lowercase-Cyrillic form that is not the canonical's -> decline.

    The bounded set a rule without morphology cannot decide: an ordinary Russian word, an
    inflected form of the term and a mangled term are the same string, and the corpus's
    variant column holds all three (measured on the derived glossary: 97 of the 132 entries
    are such forms). The form test, not a blanket lowercase refusal, is what keeps this guard
    away from a plain case normalisation (`норма` -> `НОРМА`); that line is declined by G1
    instead, because the detector is silent about it (`audit.py:189`).
    """
    text = match.group(0)
    if not all(ch in " -" or "а" <= ch <= "я" or ch == "ё" for ch in text):
        return False
    return text.lower() != canonical.lower()


def _guard_repeats_neighbour(line: str, match: re.Match[str], canonical: str) -> bool:
    """G3: the replacement would repeat the word before or after the match -> decline.

    Duplication is the report's headline shape (`Стандарт С++17` -> `Стандарт стандарт
    C++17`) and seeing it needs no morphology, so this binds where G2 does not reach: a
    capitalised or Latin variant can still be a word the canonical starts or ends with.
    """
    words = canonical.split()
    if not words:
        return False
    before = line[: match.start()].split()
    after = line[match.end() :].split()
    if before and _fold_shape(before[-1]) == _fold_shape(words[0]):
        return True
    return bool(after) and _fold_shape(after[0]) == _fold_shape(words[-1])


def _guard_canonical_wraps(line: str, match: re.Match[str], canonical: str) -> bool:
    """G4: the match is a proper substring of the canonical, ignoring case and punctuation.

    The canonical merely wraps what is already written (`компилятор c++17` against
    `стандарт c++17`), so substituting adds a word instead of repairing one. Note the limit,
    not an oversight: Cyrillic and Latin lookalikes are not folded, so the mixed-script
    spelling of the same term (`С++17` with a Cyrillic `С`) passes this guard.
    """
    text = _fold_shape(match.group(0))
    whole = _fold_shape(canonical)
    return bool(text) and text != whole and text in whole


# The four preconditions, in the order `_drift_ok` applies them, each labelled with the
# guard the decision names so a revert of one is identifiable from its failure (#287).
DRIFT_GUARDS = (
    ("G1 the canonical is already on the line", _guard_canonical_on_line),
    ("G2 an all-lowercase-Cyrillic form that is not the canonical's", _guard_lowercase_cyrillic),
    ("G3 the replacement would repeat a neighbouring word", _guard_repeats_neighbour),
    ("G4 the canonical merely wraps the match", _guard_canonical_wraps),
)


def _drift_ok(line: str, match: re.Match[str], canonical: str) -> bool:
    """Whether this whole-word glossary match may be replaced by its canonical (#287).

    A mechanical rule cannot tell a mangled term from a correct inflected Russian phrase:
    the tree carries no morphology, `Glossary.parse` lowercases the variant (`audit.py:172`)
    so the author's capitalisation is gone, and this corpus mixes Cyrillic and Latin
    lookalikes. The guards below are the decidable consequences of that ambiguity, so the
    rewrite declines what it cannot defend and leaves the text alone (#263's shape) rather
    than writing a word the lecturer never said. Two limits stay limits: morphology (G2 is
    the boundary, not a solution) and mixed script (G4 compares the letters as written).
    """
    for _name, guard in DRIFT_GUARDS:
        if guard(line, match, canonical):
            return False
    return True


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

    # 2. Glossary drift: substitute the canonical term where the replacement can be
    #    defended, and leave the line alone where it cannot. The glossary holds confirmed
    #    forms, but on this corpus the variant column holds ordinary Russian words and
    #    inflections as often as mangled terms. Measured on
    #    `~/.local/state/dyak/занятие--2/note.md` (487 lines) with the 122-entry glossary
    #    derived from `mscs/_terms`: unguarded, the rewrite corrupts 17 of the 19 lines it
    #    touches at the #277 head -- 7 with a duplicated word («стартуют все Прямые методы
    #    методы»), 4 inflected phrases turned nominative, 2 homonyms (the Dubins straight
    #    segments), 4 inflected forms («Смысловые части Функционал») -- to normalise 2, and
    #    changes 12 lines whose canonical `check_term_drift` never warned about
    #    (`audit.py:189`). The four guards are `_drift_ok`, and each one's effect is counted
    #    on the note rather than asserted: of the 53 occurrences the pre-#277 substring rule
    #    can see, G1 declines 46 as the first applicable guard and G2 the other 7 (of the 35
    #    whole-word ones, 28 and 7). G3 and G4 take no occurrence here -- G1 and G2 already
    #    cover the line -- but they are not decoration: applied alone they decline 6 and 9 of
    #    the 53, and each is pinned by the case it exists for in `tests/test_repair_drift.py`
    #    (a capitalised «С++17» whose canonical repeats the neighbouring word, a Latin
    #    «c++17» the canonical wraps). With all four the note is byte-identical on every line
    #    that used to differ, while the class the rule exists for still fires («Пантрягин» ->
    #    «Понтрягин (П.С. Понтрягин)», «ilqf» -> «iLQR»). The price, stated: G1 declines the
    #    case-only normalisation too («норма» -> «НОРМА»), because `check_term_drift` is
    #    silent about that line (`audit.py:189` -- the canonical is there, case-insensitively)
    #    and the rewrite cannot be bolder than the report that describes it. The
    #    variant is matched as a WHOLE WORD: the audit's own test is a substring one
    #    (`audit.py:189`) and it reports drift without correcting it (`audit.py:181`), so its
    #    over-matching costs a warning the reader judges, where the same over-match here
    #    rewrote «нормализация» into «НОРМАлизация» (#277).
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
        # The fix log records the changed LINE, and "ilqf -> iLQR" is not before/after text:
        # the reader needs the sentence that was rewritten, because that is the sentence they
        # have to re-check against the recording. It must be the first ACCEPTED occurrence --
        # `pattern.search` lands on one the guards declined instead, and the report then
        # carries a change whose `before` and `after` are equal, measured at 3 of the 8
        # records on this note (line 8 once, line 87 twice) before the guards.
        first: Change | None = None
        for index, line in enumerate(lines):
            accepted = [m for m in pattern.finditer(line) if _drift_ok(line, m, canonical)]
            if not accepted:
                continue
            before_line = line
            # Joined from the pieces rather than handed to `re.sub`, which parses backslash
            # escapes in a replacement: a canonical containing `\mathbf{x}` raises
            # "bad escape \m", and a term catalogue is exactly where LaTeX belongs
            # (`tests/test_stages.py` pins that insertion, with a capitalised variant the
            # guards accept).
            parts: list[str] = []
            cursor = 0
            for match in accepted:
                parts.append(line[cursor : match.start()])
                parts.append(canonical)
                cursor = match.end()
            parts.append(line[cursor:])
            lines[index] = "".join(parts)
            if first is None:
                first = Change("term/drift", index + 1, before_line, lines[index])
        if first is not None:
            report.changes.append(first)

    # The glossary step above rewrote `lines`, so the join before it is no longer the last
    # word on the text -- the trailing newline included, which the same guard restores for
    # the same reason it did then (`splitlines`/`join` drops it).
    text = "\n".join(lines)
    if markdown.endswith("\n") and not text.endswith("\n"):
        text += "\n"

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

    glossary, glossary_facts = audit_mod.load_glossary(glossary_path)

    repaired, report = repair(markdown, areport, glossary, lreport)
    (destination / REPAIRED_NOTE).write_text(repaired, encoding="utf-8")
    (destination / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / PROVENANCE_NAME).write_text(
        provenance(report, config, glossary=glossary_facts) + "\n", encoding="utf-8"
    )

    # The same line stage 9 prints, from the shared loader and narrator (#267, #270): stage 10
    # substitutes the canonical term from this glossary (`:323`), so it has the same blind spot
    # and must not be the only stage that stays quiet about it.
    line = audit_mod.glossary_note(glossary_facts)
    if line is not None:
        out.write(f"           {line}\n")

    for change in report.changes[:8]:
        out.write(f"           fixed {change.rule} line {change.line}\n")
    declined_rules = sorted({d["rule"] for d in report.declined})
    if declined_rules:
        out.write(f"           declined: {', '.join(declined_rules)}\n")
    return report


def provenance(
    report: Report, config: llm.Config | None = None, glossary: dict | None = None
) -> str:
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
            # `None` when no glossary was supplied, and otherwise the four-key state stage 9
            # records too (#267, #270): `path`, `is_file`, `entries`, `error`. This stage reads
            # the same file through the same loader (`audit.load_glossary`), so a reader who
            # compares the two provenances must not find the states collapsed on one side --
            # the identity of "not configured" and "supplied and unusable" is the defect both
            # issues name. `endpoint` above is the same null-when-absent convention.
            "glossary": glossary,
        },
        indent=2,
        ensure_ascii=False,
    )
