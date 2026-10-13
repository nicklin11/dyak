"""Stage 8: lint the note against the rules, deterministically.

No model. Every check here is either a delimiter count or a string match, and a model
producing a *probabilistic* verdict about whether `$...$` is balanced is strictly worse than
a counter. This stage is the mechanical floor under stage 7's prose: the model is creative,
this is the part that is not.

## What is checked, and why each one

The checks are the failure modes this pipeline has already demonstrated it can produce. Nothing
here is a style preference.

- **Structure.** The synthesizer skill fixes eight sections in an order
  (`~/.dsh/skills/academic-lecture-synthesizer/SKILL.md`, § "Структура выходного
  документа"). A note missing section 5 is not a note that failed a lint; it is a
  different document, and stage 9's reviewer needs to know which one it is looking at.
- **Delimiter balance.** An unclosed `$` turns the rest of the note into mathematics. It is
  the single most damaging thing a synthesis model does to a technical note, and it is
  trivially detectable.
- **Links inside math.** `[[term]]` inside `$...$` breaks both the math and the link. The
  term linker runs after this stage and has no way to know it is inside math.
- **Filler.** `ну`, `как бы`, `вы поняли` survive ASR into prose. A note carrying them reads
  as a transcript that was not read.
- **Contradictory math markers.** A note that claims `не затрагивается` in one section and
  then writes three paragraphs in another is lying about its own completeness.
- **Asset references.** A `![[frame.png]]` pointing at a file not in the bundle is a broken
  link in the vault.

## Severity

`ERROR` fails the gate (exit 4). `WARN` is reported and the note is written: a note with a
warning is still more useful than no note, and a bad note beats a missing one provided the
defects are named. Suppressing a note because it has a warning would make the tool useless
exactly when the source is worst.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import synth

REPORT_NAME = "lint.json"
PROVENANCE_NAME = "lint-provenance.json"

ERROR = "ERROR"
WARN = "WARN"

#: Words that carry no meaning in a technical note but survive verbatim from the lecture.
#: Matched as whole words, case-insensitively, on word boundaries -- `ну` inside `нулевой`
#: or `как` inside `какой` must not trigger.
FILLER = (
    "ну",
    "как бы",
    "вы поняли",
    "вы понимаете",
    "записывайте",
    "запишите",
    "это самое",
    "как говорится",
    "так сказать",
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    line: int
    detail: str

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "detail": self.detail,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0

    def add(self, rule: str, severity: str, line: int, detail: str) -> None:
        self.findings.append(Finding(rule, severity, line, detail))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """No ERRORs. Warnings do not block: see the module docstring."""
        return not self.errors

    def summary(self) -> str:
        if not self.findings:
            return f"{self.checked} checks, clean"
        return f"{self.checked} checks, {len(self.errors)} errors, {len(self.warnings)} warnings"

    def as_dict(self) -> dict:
        return {
            "checks": self.checked,
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }


def _outside_code(text: str) -> list[tuple[int, str]]:
    """`(line_number, line)` for every line outside a fenced code block.

    Everything inside a fence is a program or a YAML header. Linting a `$` inside a shell
    snippet would produce a finding about the user's code, in the user's note.
    """
    out: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append((number, line))
    return out


def check_structure(markdown: str, report: Report) -> None:
    """The synthesizer skill's eight sections, in order, under their exact titles.

    Source: `~/.dsh/skills/academic-lecture-synthesizer/SKILL.md`, § "Структура выходного
    документа". Not an ADR decision -- see the module docstring.
    """
    headings = [
        (int(m.group(1)), m.group(2).strip())
        for m in re.finditer(r"^## (\d+)\.\s+(.+)$", markdown, re.M)
    ]
    expected = [(n, t) for n, t, _ in synth.SECTIONS]
    if headings == expected:
        report.checked += 1
        return
    report.checked += 1
    found = {n for n, _ in headings}
    for number, title in expected:
        if number not in found:
            report.add(
                "structure/missing-section", ERROR, 0, f"section {number} «{title}» is absent"
            )
    for number, title in headings:
        canonical = next((t for n, t in expected if n == number), None)
        if canonical is None:
            report.add(
                "structure/unknown-section",
                ERROR,
                0,
                f"section {number} is not one of the eight the synthesizer skill defines",
            )
        elif canonical != title:
            report.add(
                "structure/renamed-section",
                ERROR,
                0,
                f"section {number} is titled «{title}», expected «{canonical}»",
            )
    order = [n for n, _ in headings]
    if order != sorted(order):
        report.add("structure/order", ERROR, 0, f"sections appear as {order}")


def check_delimiters(markdown: str, report: Report) -> None:
    """`$`, `$$`, `\\[`, `\\]` balance per line pair. An unclosed one eats the note."""
    lines = _outside_code(markdown)
    text = "\n".join(line for _, line in lines)
    report.checked += 1
    if "\\[" in text or "\\]" in text:
        opens = text.count("\\[")
        closes = text.count("\\]")
        if opens != closes:
            report.add(
                "math/display-unbalanced",
                ERROR,
                0,
                f"{opens} `\\\\[` against {closes} `\\\\]`",
            )
    if text.count("$$") % 2:
        report.add("math/display-unbalanced", ERROR, 0, "an odd number of `$$`")
    # Inline. Per line first, because a finding carrying a line number is worth having; then
    # document-wide as a fallback, because a `$` opened on one line and closed on the next is
    # a real defect no per-line count can see, and line 0 is the honest number for it.
    # Reporting both when one line is already unterminated would count a single defect twice
    # and leave a reader with two numbers and no way to tell which to fix.
    stripped = text.replace("$$", "")
    # The PAIRS, not the numbers: `lines` is a fence-filtered list indexed by POSITION, while
    # its numbers are DOCUMENT lines (_outside_code drops every fenced line). Re-indexing it
    # with a document number was #265 -- on the recorded note (461 of 487 lines outside
    # fences) a document line 432 finding quoted document line 458's text, and one at document
    # line 465 raised IndexError out of `lint.run`, which `pipeline.py:893` calls unguarded.
    odd = [(number, line) for number, line in lines if len(re.findall(r"(?<!\\)\$", line)) % 2]
    if odd:
        first_number, first_line = odd[0]
        report.add(
            "math/inline-unbalanced",
            ERROR,
            first_number,
            f"odd `$` on {len(odd)} line(s), first: {first_line.strip()[:70]}",
        )
    elif stripped.count("$") % 2:
        report.add(
            "math/inline-unbalanced",
            ERROR,
            0,
            "an odd number of `$` document-wide: a delimiter spans lines",
        )


def check_braces(markdown: str, report: Report) -> None:
    """LaTeX brace balance inside math, per group. `\\{` is a literal brace."""
    lines = _outside_code(markdown)
    report.checked += 1
    for number, line in lines:
        for group in re.findall(r"\$\$(.+?)\$\$|(?<!\$)\$([^$]+)\$(?!\$)", line, re.S):
            body = group[0] or group[1]
            depth = 0
            for index, char in enumerate(body):
                if char == "\\":
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth < 0:
                        break
            if depth:
                report.add(
                    "math/brace-unbalanced",
                    ERROR,
                    number,
                    f"unbalanced braces in: {body[:70]}",
                )
                return


def check_links_in_math(markdown: str, report: Report) -> None:
    """`[[wikilink]]` inside `$...$` breaks the math and the link both.

    The term linker runs after this stage and has no way to know it is inside math, so this
    is the only place the two can be kept apart.
    """
    report.checked += 1
    for number, line in _outside_code(markdown):
        for match in re.finditer(r"(?<!\$)\$([^$]+)\$(?!\$)", line):
            if "[[" in match.group(1):
                report.add(
                    "link/inside-math",
                    ERROR,
                    number,
                    f"wikilink inside inline math: {match.group(0)[:80]}",
                )
                return


# The stage-7 marker for an ASR span that could not be verified. Its payload is, by the
# skill's own wording, "буквально что услышали" -- literally what was heard. So a filler
# inside it is the transcriber's, not the note author's, exactly as with «...».
_UNREADABLE_RE = re.compile(r"\[неразборчиво[^\]]*\]?")


def _strip_quotes(line: str) -> str:
    """Blank out verbatim spans, preserving line numbers and column positions.

    A quoted span is verbatim by definition. The stage-9 audit requires verbatim citations
    to be checkable against the transcript, so a filler word inside a quote is not debris in
    the note -- it is the lecturer's speech, faithfully reproduced, and "repairing" it would
    falsify the record the audit exists to check. Measured on lecture 1, 2026-10-04:
    `«если вы понимаете одно, то очень легко понять другое»` was reported as
    `«вы понимаете» appears 1 times`.

    The same argument covers `[неразборчиво: ...]`, the stage-7 marker for a span the ASR
    could not verify, whose payload the skill defines as "буквально что услышали". Run 6
    flagged `ну` at line 120 of lecture 1:

        Адаптивное управление — отдельный курс; ... (какие-то базы —
        ⚠️ [неразборчиво: ну, какие-то базы]).

    and stage 10 declined to repair it: `"reason": "not a mechanical fix"`. The pipeline was
    right and the warning was noise -- a warning that can never be actioned teaches the
    reader to ignore warnings.

    An unclosed « on a line blanks to the end of that line only. A quote opened on one line
    and closed on another is the lecturer's paragraph break, and the debris after it is
    still the note author's.
    """
    line = _UNREADABLE_RE.sub(lambda m: " " * len(m.group()), line)
    out, inside, start = [], False, 0
    for index, char in enumerate(line):
        if char == "«":
            if not inside:
                start, inside = index, True
            out.append(" ")
        elif char == "»" and inside:
            inside = False
            out.append(" ")
        elif inside:
            out.append(" ")
        else:
            out.append(char)
    if inside:
        out = list(line[:start] + " " * (len(line) - start))
    return "".join(out)


def check_filler(markdown: str, report: Report) -> None:
    """Conversational debris that survived ASR into the prose.

    Fillers are matched as **token sequences**, not as substrings of the line, because most
    of `FILLER` is multi-word ("как бы", "вы поняли") and `_WORD` splits those into separate
    tokens. A single-word filler must still be a whole token: "ну" is debris, "нужно" is not.

    This was `word == filler or (filler == "как бы" and f"{word} бы" == filler)`, and the
    second clause reduces to `word == "как"` -- it never checked that a "бы" followed. So
    every occurrence of the ordinary Russian word "как" was reported as "как бы". Measured on
    lecture 1, 2026-10-04: 17 findings against 0 instances of "как бы" and 17 instances of
    "как" in correct constructions ("как и все", "как правило", "как задачу"). Stage 10 then
    spent its budget trying to repair clean prose.
    """
    report.checked += 1
    by_length: dict[int, list[tuple[str, ...]]] = {}
    for filler in FILLER:
        by_length.setdefault(len(filler.split()), []).append(tuple(filler.split()))
    widest = max(by_length)

    hits: dict[str, list[int]] = {}
    for number, line in _outside_code(markdown):
        words = [word.lower() for word in _WORD.findall(_strip_quotes(line))]
        for start in range(len(words)):
            for width in range(min(widest, len(words) - start), 0, -1):
                window = tuple(words[start : start + width])
                for candidate in by_length.get(width, ()):
                    if window == candidate:
                        hits.setdefault(" ".join(candidate), []).append(number)
    for filler, numbers in hits.items():
        report.add(
            "style/filler",
            WARN,
            numbers[0],
            f"«{filler}» appears {len(numbers)} times (first at line {numbers[0]})",
        )


def check_empty_section_claims(markdown: str, report: Report) -> None:
    """A section that says "not covered" and then writes paragraphs is lying.

    This is the check that catches a model padding the document to look complete, which is
    the single most damaging failure mode for a note whose purpose is to be trustworthy.

    The sentinel is matched as a **whole line**, which is the contract
    `TemplateSynthesizer` writes to (`parts.append(NOT_COVERED)` when a body is empty) --
    not as a substring anywhere in the section. Substring matching fired on the LLM
    synthesizer's own scoped deferral, which is honest and specific:

        ## 3. Карта источников
        ... 29 lines of content ...
        в лекции не затрагивается: конкретные формулы функционала качества.

    Measured on lecture 1, 2026-10-04: that line produced "«3. Карта источников» declares
    itself not covered and then has 29 more line(s)" as an ERROR. It is a blocking
    severity, so the pipeline refused to finish a run whose note was correct. The section
    says one narrow thing is out of scope; it does not declare itself empty.

    Both readings still work after the change: a template section carrying the bare sentinel
    *and* content is a real bug and is still reported; a bare sentinel alone is not.
    """
    report.checked += 1
    parts = re.split(r"^## ", markdown, flags=re.M)[1:]
    for part in parts:
        heading, _, body = part.partition("\n")
        lines = body.splitlines()
        if not any(line.strip() == synth.NOT_COVERED for line in lines):
            continue
        extra = [
            line
            for line in lines
            if line.strip() and line.strip() != synth.NOT_COVERED and not line.startswith("#")
        ]
        if extra:
            report.add(
                "structure/claims-empty-but-is-not",
                ERROR,
                0,
                f"«{heading.strip()}» declares itself not covered and then has "
                f"{len(extra)} more line(s)",
            )


def check_assets(markdown: str, images: Path, report: Report) -> None:
    """Every `![[...]]` or `![](...)` resolves inside the bundle."""
    report.checked += 1
    for match in re.finditer(r"!\[\[([^\]|]+)", markdown):
        name = match.group(1).strip()
        if name and not (images / Path(name).name).is_file():
            report.add(
                "asset/missing", ERROR, 0, f"image reference has no file in the bundle: {name}"
            )
            return


def check_mojibake(markdown: str, report: Report) -> None:
    """`Ð`, `Ñ` in a row: a string that was decoded with the wrong encoding somewhere.

    Worth its own rule because it is invisible to a reader who does not know Russian and
    looks like ordinary text to one who does.
    """
    report.checked += 1
    for number, line in _outside_code(markdown):
        hits = len(re.findall(r"[ÐÑ][-ÿ]", line))
        if hits >= 3:
            report.add(
                "text/mojibake", ERROR, number, f"{hits} mis-decoded pairs: {line.strip()[:70]}"
            )
            return


def check_duplicate_heading(markdown: str, report: Report) -> None:
    """Two sections with the same title means the document grew a shape it should not have."""
    report.checked += 1
    titles = [m.group(1).strip() for m in re.finditer(r"^## (.+)$", markdown, re.M)]
    seen: set[str] = set()
    for title in titles:
        if title in seen:
            report.add("structure/duplicate-heading", WARN, 0, f"«{title}» appears twice")
            return
        seen.add(title)


def lint(markdown: str, images: Path) -> Report:
    report = Report()
    check_structure(markdown, report)
    check_delimiters(markdown, report)
    check_braces(markdown, report)
    check_links_in_math(markdown, report)
    check_filler(markdown, report)
    check_empty_section_claims(markdown, report)
    check_assets(markdown, images, report)
    check_mojibake(markdown, report)
    check_duplicate_heading(markdown, report)
    return report


def run(note: Path, images: Path, destination: Path, *, stream=None) -> Report:
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    markdown = note.read_text(encoding="utf-8") if note.is_file() else ""
    report = lint(markdown, images)
    (destination / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / PROVENANCE_NAME).write_text(provenance(report) + "\n", encoding="utf-8")
    for finding in report.findings[:12]:
        out.write(
            f"           {finding.severity} {finding.rule} line {finding.line}: {finding.detail}\n"
        )
    if len(report.findings) > 12:
        out.write(f"           ... and {len(report.findings) - 12} more in lint.json\n")
    return report


def provenance(report: Report) -> str:
    return json.dumps(
        {
            "stage": 8,
            "tool": "dyak-lint-v1",
            "requires_model": False,
            "rules": [
                "structure/missing-section",
                "structure/renamed-section",
                "structure/order",
                "structure/duplicate-heading",
                "structure/claims-empty-but-is-not",
                "math/inline-unbalanced",
                "math/display-unbalanced",
                "math/brace-unbalanced",
                "link/inside-math",
                "asset/missing",
                "text/mojibake",
                "style/filler",
            ],
            "blocking": ERROR,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "code_fences_excluded": True,
        },
        indent=2,
    )
