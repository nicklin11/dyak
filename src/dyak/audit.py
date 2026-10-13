"""Stage 9: audit the note. Deterministic first, model second, never the other way round.

Two tiers, and the order is the design.

**Tier 1 is deterministic and carries the load.** Every check here is a parse or a set
operation: terms that drifted from the glossary, equations whose dimensions do not agree,
numbers in the mathematics that appear nowhere in the transcript. These are the failures a
human cannot catch by reading -- dimension mismatch is invisible to someone who does not
already know the answer, and an invented coefficient looks exactly like a remembered one.

**Tier 2 is a critic in a context that did not write the note**, and per ADR-0001 D5 it is
given the finished note *and the raw transcript*. It never sees stage 7's instructions or
stage 7's conversation, so it is not reviewing its own work in its own context -- but it is
told what the lecturer actually said, which is the half of the job that matters. The
measured failure this exists for is not bad prose; it is a section carrying three mutually
exclusive representations of the same dynamical system, where the formulas contradicted
both the note's prose and the physics.

**A finding without a citation is discarded, and the discard is logged.** This is the whole
point of ADR-0001 D5 and it is enforced mechanically, not requested politely: `parse_critic_findings`
checks that the quoted text is verbatim from this section, that the citation is verbatim
from the transcript, and that a confidence was stated. A finding failing any of the three
goes into `dropped` with its reason. A critic that fabricates a plausible-sounding
quotation is then not a finding at all, which is the correct outcome -- the alternative is
a note annotated with confident nonsense.

Tier 2 is optional and its absence is recorded. A note audited by tier 1 alone is reported
as `tier2: NOT_CONFIGURED`, not as audited-and-clean.

## On the dimension engine

This is not a theorem prover and does not claim to be. What it does:

1. Collects declarations `\\mathbf{x} \\in \\mathbb{R}^n` and records each symbol's rank
   (scalar / vector / matrix) and shape.
2. Splits each equation at `=` and, within each side, at top-level `+` and `-`.
3. Rejects a sum whose terms disagree on whether they are scalar or non-scalar, and a
   product whose declared shapes cannot multiply.

That is the "скаляр не складывается с матрицей" class from ADR-0001 D5 and it catches the mistake
this project already cares about. It will not catch a wrong constant, a transposed index, or
a formula that is right about the wrong problem. Every finding it reports names the line, and
a finding it cannot produce is not implied by its silence -- `audited` here means "these
rules ran", never "the mathematics is correct".
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import llm, stages

REPORT_NAME = "audit.json"
PROVENANCE_NAME = "audit-provenance.json"
LLM_TRANSCRIPT_NAME = "audit-llm-transcript.json"
ALL_ARTEFACTS = (REPORT_NAME, PROVENANCE_NAME, LLM_TRANSCRIPT_NAME)

SCALAR, VECTOR, MATRIX = "scalar", "vector", "matrix"
RANKS = {SCALAR: 0, VECTOR: 1, MATRIX: 2}


@dataclass(frozen=True)
class Finding:
    tier: str
    rule: str
    severity: str
    line: int
    detail: str
    quote: str = ""
    #: A verbatim fragment of the transcript. ADR-0001 D5: a finding without one is not evidence,
    #: it is a guess about what was said -- so it is discarded, and the drop is logged.
    citation: str = ""
    #: `high` / `medium` / `low`, as the auditor stated it. Required, not defaulted: a
    #: finding whose author would not commit to a confidence has not really been reviewed.
    confidence: str = ""

    @property
    def key(self) -> str:
        """Stable id for the repair stage to address this finding by."""
        return f"{self.rule}@{self.line}"

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "detail": self.detail,
            "quote": self.quote,
            "citation": self.citation,
            "confidence": self.confidence,
            "key": self.key,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    tier1_rules_run: int = 0
    tier2: str = "NOT_CONFIGURED"
    tier2_findings: int = 0
    tier2_coverage: dict = field(default_factory=dict)
    #: Sections the critic was sent only in part, by heading. A section above `SECTION_CHARS`
    #: can pass a review of the part that was sent, so the cut is named here and in the run
    #: line rather than left in the request (#123).
    tier2_truncated: list[str] = field(default_factory=list)
    #: Critic findings discarded for want of a citation, each with the reason. ADR-0001 D5 makes
    #: this a logged outcome, not a silent one: a filter that hides its own discards is a
    #: filter nobody can audit.
    dropped: list[dict] = field(default_factory=list)

    def add(self, tier: str, rule: str, severity: str, line: int, detail: str, quote: str = ""):
        self.findings.append(Finding(tier, rule, severity, line, detail, quote))

    @property
    def ok(self) -> bool:
        """No ERROR-tier findings. ADR-0001 D5: annotate-and-flag, never discard the note."""
        return not any(f.severity == "ERROR" for f in self.findings)

    def summary(self) -> str:
        errors = sum(1 for f in self.findings if f.severity == "ERROR")
        warns = sum(1 for f in self.findings if f.severity == "WARN")
        dropped = f", {len(self.dropped)} discarded" if self.dropped else ""
        return (
            f"{self.tier1_rules_run} rules, {errors} errors, {warns} warnings"
            f"{dropped}, tier 2 {self.tier2}"
        )

    def as_dict(self) -> dict:
        return {
            "tier1_rules_run": self.tier1_rules_run,
            "tier2": self.tier2,
            "tier2_findings": self.tier2_findings,
            "tier2_coverage": self.tier2_coverage,
            "tier2_truncated": self.tier2_truncated,
            "dropped": self.dropped,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


# --- glossary -----------------------------------------------------------------------


@dataclass(frozen=True)
class Glossary:
    """`| variant | canonical | domain | date |`, the format the project already uses."""

    entries: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, text: str) -> Glossary:
        out: list[tuple[str, str]] = []
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            variant, canonical = cells[0], cells[1]
            # Skip the header and the separator rows.
            if not variant or set(variant) <= {"-", ":"} or variant.lower() == "вариант asr":
                continue
            if not canonical or set(canonical) <= {"-", ":"}:
                continue
            out.append((variant.lower(), canonical))
        return cls(tuple(out))


def check_term_drift(
    markdown: str, glossary: Glossary, lines: list[tuple[int, str]], report: Report
) -> None:
    """A glossary variant that survived into the note is a term the ASR mangled.

    Reported, not corrected. The right fix is the last revision the lecturer actually said,
    and the glossary holds what was confirmed, not what was guessed at synthesis time.
    """
    report.tier1_rules_run += 1
    for number, line in lines:
        for variant, canonical in glossary.entries:
            if len(variant) < 4:  # too short to match without false positives
                continue
            if variant in line.lower() and canonical.lower() not in line.lower():
                report.add(
                    "tier1",
                    "term/drift",
                    "WARN",
                    number,
                    f"«{variant}» appears un-normalised; glossary says «{canonical}»",
                    quote=line.strip()[:100],
                )


# --- dimensions ---------------------------------------------------------------------


#: `\mathbf{x} \in \mathbb{R}^n` and `\mathbf{A} \in \mathbb{R}^{m \times n}` both occur in
#: real notes, so the exponent's braces are optional. Requiring them made every declaration
#: written the common way invisible, and the engine then reported the note as unchecked
#: rather than as clean -- the same failure shape as checking the wrong symbol.
_DECL = re.compile(
    r"(?P<sym>\\[a-zA-Z]+\s*\{[a-zA-Z]\}|\\[a-zA-Z]+|[a-zA-Z])\s*\\in\s*\\mathbb\{R\}"
    r"\s*\^\s*(?:\{(?P<braced>[^}]+)\}|(?P<bare>[a-zA-Z0-9]+))"
)
#: `\mathbf{x}`, `\bm x`, `X`, but not `\mathbb`, `\in`, `\le`.
_SYMBOL = re.compile(
    r"\\(?:mathbf|bm|boldsymbol)\s*\{?([a-zA-Z])\}?|\\frac\{([a-zA-Z])\}\{|(?<![\\a-zA-Z])([a-zA-Z])(?![a-zA-Z])"
)
_COMMANDS = frozenset(
    """in mathbb le ge geq ne eq left right cdot times sum int frac sqrt log exp max min
    quad qquad forall exists pm mid leq geq approx sim infty partial nabla lvert rvert
    lVert rVert hat bar tilde vec dot d dT operatorname text mathrm mathbb mathcal""".split()
)


def _declared_shapes(markdown: str) -> dict[str, str]:
    """`symbol -> shape string` from every `\\in \\mathbb{R}^{...}` in the note."""
    shapes: dict[str, str] = {}
    for match in _DECL.finditer(markdown):
        sym = _clean_symbol(match.group("sym"))
        shape = match.group("braced") or match.group("bare")
        if sym and shape:
            shapes[sym] = shape.replace(" ", "")
    return shapes


def _clean_symbol(raw: str) -> str:
    """The variable name inside a declaration's left-hand side.

    `\\mathbf{x}` is `x`, not `m`. Taking the first ASCII letter instead returns the first
    letter of the *command name*, so every bold symbol declared in the note collapsed onto
    the same key `m` -- and the dimension engine then compared `x` and `A` against nothing
    and reported a clean bill of health. A rule that silently checks nothing is worse than
    one that is absent, because it is indistinguishable from a rule that passed.
    """
    braced = re.search(r"\\[a-zA-Z]+\s*\{\s*([a-zA-Z])\s*\}", raw)
    if braced:
        return braced.group(1)
    bare = re.search(r"([a-zA-Z])\s*$", raw)
    return bare.group(1) if bare else ""


def _rank_of(symbol: str, shapes: dict[str, str]) -> int | None:
    """0 scalar, 1 vector, 2 matrix, or None when the note never declared it.

    `\\mathbb{R}^1` and `\\mathbb{R}^{1}` are scalars. Everything else single-bracketed --
    `n`, `m`, `d`, even `n-1` -- is a vector, because that is what a one-dimensional space
    means regardless of which symbol names its dimension. Testing `shape.isdigit()` instead
    returned None for every symbolic dimension, which is nearly all of them, and the engine
    reported a note it had never examined as clean.
    """
    shape = shapes.get(symbol)
    if shape is None:
        return None
    if "times" in shape:
        return MATRIX
    return SCALAR if shape in ("0", "1") else VECTOR


def _split_top(text: str, ops: str) -> list[str]:
    """Split on operators outside braces, backslash commands and parens."""
    parts, depth, buf = [], 0, []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch in ops and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _symbols_in(term: str) -> list[str]:
    out: list[str] = []
    for match in _SYMBOL.finditer(term):
        sym = match.group(1) or match.group(2) or match.group(3)
        if not sym or sym in _COMMANDS:
            continue
        out.append(sym)
    return out


def check_dimensions(markdown: str, lines: list[tuple[int, str]], report: Report) -> None:
    """Scalar plus vector is the mistake ADR-0001 D5 names, and the one this catches.

    Requires the note to have declared shapes. A note that never writes `\\in \\mathbb{R}^n`
    gets no dimension findings and `audit/dimensions-skipped` says so, rather than the
    absence being read as a clean bill of health.
    """
    report.tier1_rules_run += 1
    shapes = _declared_shapes(markdown)
    if not shapes:
        report.add(
            "tier1",
            "audit/dimensions-skipped",
            "INFO",
            0,
            "the note declares no domains, so no dimension check ran",
        )
        return

    for number, line in lines:
        for group in re.findall(r"\$\$(.+?)\$\$|(?<!\$)\$([^$]+)\$(?!\$)", line, re.S):
            body = group[0] or group[1]
            for side in _split_top(body, "="):
                terms = _split_top(side, "+-")
                if len(terms) < 2:
                    continue
                ranks = set()
                unknown: list[str] = []
                for term in terms:
                    local = set()
                    for sym in _symbols_in(term):
                        rank = _rank_of(sym, shapes)
                        if rank is not None:
                            local.add(rank)
                    # A term's rank is the maximum it contains: `A x` is a vector even
                    # though both symbols are declared.
                    if local:
                        ranks.add(max(local))
                    else:
                        unknown.append(term.strip()[:40])
                # A summand built entirely from undeclared symbols cannot be rank-checked.
                # Silence about it would read as "this sum was verified".
                if unknown:
                    report.add(
                        "tier1",
                        "dimension/undeclared",
                        "WARN",
                        number,
                        f"{len(unknown)} of {len(terms)} terms of a sum use only "
                        f"undeclared symbols, so those terms were not checked: "
                        f"{'; '.join(dict.fromkeys(unknown))[:80]}",
                        quote=body[:100],
                    )
                    continue
                if len(ranks) > 1:
                    report.add(
                        "tier1",
                        "dimension/mismatch",
                        "ERROR",
                        number,
                        "terms of different rank summed: "
                        + ", ".join(f"{r}: {t[:40]}" for r, t in zip(sorted(ranks), terms)),
                        quote=body[:100],
                    )
                    # `continue`, not `return`. A rule that stops at its first finding
                    # reports one error in a note that has three, and the report then reads
                    # as "1 error found" -- a statement about the note that is false.
                    continue


# --- numbers ------------------------------------------------------------------------


_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")


def check_unsourced_numbers(
    markdown: str, transcript: str, lines: list[tuple[int, str]], report: Report
) -> None:
    """A number in the mathematics that the lecturer never said.

    The cheapest available proxy for "the model invented this coefficient". Not proof -- a
    number can legitimately be derived, and a coefficient can be written a different way --
    so it is a WARN that names the number and asks for it to be checked, not an ERROR that
    deletes the line.
    """
    report.tier1_rules_run += 1
    if not transcript.strip():
        report.add(
            "tier1", "audit/numbers-skipped", "INFO", 0, "no transcript to check numbers against"
        )
        return
    haystack = transcript.replace(",", ".")
    for number, line in lines:
        for match in re.finditer(r"\$\$(.+?)\$\$|(?<!\$)\$([^$]+)\$(?!\$)", line, re.S):
            body = match.group(0)
            for raw in _NUMBER.findall(body):
                if (
                    len(raw.replace(".", "")) < 2
                ):  # a bare digit is an index or an exponent, not a coefficient
                    continue
                if raw.replace(",", ".") not in haystack:
                    report.add(
                        "tier1",
                        "number/unsourced",
                        "WARN",
                        number,
                        f"{raw} appears in the mathematics but not in the transcript",
                        quote=body[:100],
                    )
                    # Every unsourced number is named, not just the first: a reader who
                    # fixes 0.37 and re-runs must not be handed the same report again.
                    continue


def check_empty_math(markdown: str, lines: list[tuple[int, str]], report: Report) -> None:
    """`$$` with nothing between the delimiters renders as a blank block."""
    report.tier1_rules_run += 1
    for number, line in lines:
        for body in re.findall(r"\$\$\s*\$\$", line):
            report.add("tier1", "math/empty", "WARN", number, "an empty `$$` block", quote=body)
            continue


# --- tier 2 -------------------------------------------------------------------------

# The example below is not decorative: it is the shape the model copies, and the previous one
# could not survive `parse_critic_findings` -- its citation was split across two lines, so the
# `- ` line held `[источник: соответствие` with no closing `]`, and its citation was the
# phrase "соответствие динамической системы" against a formula the note does not contain. The
# model reproduced that shape: 45 of 45 findings on the replay were discarded (#240). Both
# fragments here are real -- the note sentence and the transcript span it was checked against,
# from the 2026-10-12 run -- and the whole item is one line, which is what the guard reads.
CRITIC_SYSTEM = """\
Ты — рецензент конспекта лекции. Твоя единственная задача — найти в конспекте ошибки.

Тебе дан конспект и ПОЛНАЯ стенограмма лекции, как она была распознана. Ты не писал
этот конспект и не видишь истории его создания.

Правила:
- НИЧЕГО не исправляй. Только перечисли найденное.
- Не хвали, не пересказывай, не задавай вопросов.
- Считай формулы. Если размерности не сходятся — это находка.
- Если формула верна сама по себе, но НЕ следует из стенограммы — это тоже находка.
- Если ты не уверен, что находка настоящая — так и напиши. Тебе разрешено ответить
  ровно: НЕ УВЕРЕН. Это полезный ответ, а не отказ.
- Если всё верно — ответь ровно: OK

ФОРМАТ ОТВЕТА — только список, каждый пункт с новой строки, строго в этом виде:

- <дословный фрагмент конспекта>: <что не так> [источник: <дословный фрагмент стенограммы>] [уверенность: высокая|средняя|низкая]

Требования к каждому пункту, иначе он будет отброшен:
1. Первый фрагмент — ДОСЛОВНО из проверяемого конспекта. Не пересказ, не исправление.
2. [источник: ...] — ДОСЛОВНО из стенограммы, именно то, что там прозвучало.
   Находка без цитаты в стенограмме будет отброшена: это мнение, а не свидетельство.
3. [уверенность: ...] — одно из трёх слов. Находка без неё тоже отбрасывается.

Пример (одной строкой — многострочный пункт не читается до конца):

- Если коэффициенты симметричной матрицы вещественны, то вещественны и её собственные значения: в стенограмме названа только симметричность, вывода о собственных значениях нет [источник: там симметричность, ну, симметричность, понятно] [уверенность: высокая]

Оба фрагмента скопированы, ни один не пересказан. Первый — строка из конспекта, не твой заголовок и не твоя формулировка. [источник: ...] — то, что прозвучало в стенограмме, слово в слово. Не оборачивай цитату в кавычки и не дописывай знаков: кавычки, тире и запятые, которых нет в стенограмме, — твоя пунктуация, и проверка читает её как пересказ.

Если находок нет — одно слово: OK"""

#: The auditor's three permitted answers that are not findings.
ABSTAIN = {"НЕ УВЕРЕН", "НЕ УВЕРЕН.", "НЕ УВЕРЮ", "НЕ УВЕРЕН!", "NOT SURE", "NOT SURE."}

_CITATION = re.compile(r"\[источник:\s*(?P<cite>.+?)\]", re.IGNORECASE)
_CONFIDENCE = re.compile(r"\[уверенность:\s*(?P<conf>[^\]]+)\]", re.IGNORECASE)
_CONFIDENCE_WORDS = {"высокая": "high", "средняя": "medium", "низкая": "low"}
_STAMP = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*", re.M)


def _norm(text: str) -> str:
    """Collapse whitespace and drop `[hh:mm:ss]` stamps, for substring comparison only.

    The timestamps are stripped because a citation is evidence about what was *said*; the
    clock offset is metadata the critic has no reason to reproduce. Whitespace, case and the
    stamps are the whole of it -- everything else a citation is compared with is in
    `_verbatim`, which says what a quotation is allowed to be wrapped in (#240).
    """
    return re.sub(r"\s+", " ", _STAMP.sub("", text)).strip().lower()


#: What a citation may be wrapped in without changing what it quotes. The critic writes
#: `[источник: «...»]` although the format line asks for a bare fragment, and on the
#: 2026-10-12 run that alone discarded 17 of 45 findings (#240).
_CITE_WRAPPERS = "«»\"'“”„‟`()[]{}"
#: Punctuation the critic may leave at either end of a citation, for the same reason: it is the
#: critic's sentence, not the lecturer's.
_CITE_EDGE = ",.;:!?—–-…"
#: Separators the critic uses to join two spans inside one citation. Each side is a real
#: transcript span and the join is the critic's (#240).
_CITE_SPLIT = re.compile(r"\.{3}|…|;")
#: The shortest fragment that counts as a citation. A shorter match is not evidence that the
#: lecturer said it -- "двойной путь" (12 characters) sits inside both the paraphrase the issue
#: is about and the real sentence it paraphrases -- so a citation split on `…`/`...`/`;` has to
#: yield a fragment at least this long (#240). A whole citation is matched as it always was,
#: at any length: an exact short quote was kept before this change and is still kept.
MIN_FRAGMENT_CHARS = 20


def _strip_cite_wrappers(text: str) -> str:
    """Drop the quoting a citation may be wrapped in, and the punctuation at its edges.

    Measured on the 2026-10-12 run (#240, comment 6137266759): of 68 discards, 67 were this --
    a citation that is in the transcript character for character once `«»`/quotes/brackets and
    a stray trailing comma are removed. A quotation mark is not a difference in what was said,
    and the guard was discarding findings over guillemets.

    The two strips alternate until the text stops changing, because the critic writes both
    orders: `"цитата",` needs the comma off before the quote is at the end, and `(цитата)`
    needs the bracket off before the parenthesis is.
    """
    text = text.strip()
    while True:
        before = text
        while text and text[0] in _CITE_WRAPPERS:
            text = text[1:].lstrip()
        while text and text[-1] in _CITE_WRAPPERS:
            text = text[:-1].rstrip()
        text = text.strip(_CITE_EDGE).strip()
        if text == before:
            return text


def _cite_fragments(text: str) -> list[str]:
    """The spans one citation quotes, when the critic joins two with `…`, `...` or `;`."""
    return [part.strip() for part in _CITE_SPLIT.split(text) if part.strip()]


def _verbatim(cite: str, haystack: str, *, fragments: bool) -> str:
    """The normalised span of `cite` that is verbatim in `haystack`, or "".

    `fragments` accepts a citation that quotes two places and marks the gap with `…`/`...`/`;`:
    each side is still exact and the join is the critic's (#240). The note-quote side passes
    False -- the quote is the finding's evidence anchor, not a summary of it.
    """
    text = _norm(_strip_cite_wrappers(cite))
    if text and text in haystack:
        return text
    if fragments:
        for part in _cite_fragments(text):
            part = _norm(part)
            if len(part) >= MIN_FRAGMENT_CHARS and part in haystack:
                return part
    return ""


def parse_critic_findings(
    raw: str, section: str, transcript: str
) -> tuple[list[Finding], list[dict]]:
    """ADR-0001 D5's enforced format, checked mechanically rather than trusted.

    Returns the findings that survived and the ones discarded with the reason for each.
    A finding is kept only if all three hold: the quoted text is verbatim from this
    section, the citation is verbatim from the transcript, and a confidence was stated.
    Anything else is discarded *and logged* -- an enforced format that silently drops
    half its output is indistinguishable from one that is not enforced at all.

    "Verbatim" is `_verbatim`: whitespace, case and `[hh:mm:ss]` stamps are ignored, the
    citation may be wrapped in quotes or brackets and may carry edge punctuation, and a
    citation that joins two spans with `…`/`...`/`;` counts if one span is long enough. What
    it still rejects is a paraphrase -- text that is not in the transcript at all (#240).
    """
    haystack_note, haystack_transcript = _norm(section), _norm(transcript)
    kept: list[Finding] = []
    dropped: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        body = line[1:].strip()
        if not body:
            continue

        citation = _CITATION.search(body)
        confidence = _CONFIDENCE.search(body)
        quote = body[: body.find(":")] if ":" in body else ""

        if citation is None:
            dropped.append({"line": body[:300], "reason": "no citation into the transcript"})
            continue
        if confidence is None:
            dropped.append({"line": body[:300], "reason": "no confidence stated"})
            continue
        stated = _CONFIDENCE_WORDS.get(_norm(confidence.group("conf")))
        if stated is None:
            # The reason is a stable string and the offending word is a separate field:
            # someone grepping `dropped` for "why was this thrown away" should not have to
            # parse a formatted message to find the answer.
            dropped.append(
                {
                    "line": body[:300],
                    "reason": "confidence is not a level",
                    "stated": confidence.group("conf").strip(),
                }
            )
            continue
        cite = citation.group("cite").strip()
        if not _verbatim(cite, haystack_transcript, fragments=True):
            dropped.append(
                {"line": body[:300], "reason": "citation is not verbatim in the transcript"}
            )
            continue
        if not _verbatim(quote, haystack_note, fragments=False):
            dropped.append({"line": body[:300], "reason": "quote is not verbatim in the note"})
            continue

        kept.append(
            Finding(
                tier="tier2",
                rule="critic/finding",
                severity="WARN",
                line=0,
                detail=body[:300],
                quote=quote.strip(),
                citation=cite,
                confidence=stated,
            )
        )
    return kept, dropped


#: Characters of one section sent to the tier-2 critic. A section longer than this is audited
#: only in part -- that is a real limit, not an accident -- and which sections hit it is
#: reported (`Report.tier2_truncated`), never applied silently (#123).
SECTION_CHARS = 6000


@dataclass(frozen=True)
class _Section:
    """One note section's request: what the critic is sent, and what the report says about it."""

    index: int
    heading: str
    text: str
    messages: list[dict]
    over_limit: bool


def _section_request(index: int, part: str, transcript_text: str) -> _Section | None:
    """The request for one `## ` section, or None when it has no body to audit.

    Shared by the sequential loop and the concurrent one so a section cannot be sent one thing
    in one mode and another in the other: the transcript-first order (#123) and the
    `SECTION_CHARS` cut are decided here, once.
    """
    heading, _, body = part.partition("\n")
    if not body.strip() or body.strip() == "в лекции не затрагивается":
        return None
    section_text = body.strip()
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM},
        {
            "role": "user",
            "content": (
                # The transcript first and the section last (#123). All eight requests then
                # share the transcript as a common prefix, which is the condition a
                # provider prefix-cache is built for; section-first made every request's
                # prefix unique, so none of them could share anything.
                f"## СТЕНОГРАММА ЛЕКЦИИ\n\n{transcript_text.strip()}"
                f"\n\n## КОНСПЕКТ — {heading.strip()}\n\n{section_text[:SECTION_CHARS]}"
            ),
        },
    ]
    return _Section(
        index=index,
        heading=heading.strip(),
        text=section_text,
        messages=messages,
        over_limit=len(section_text) > SECTION_CHARS,
    )


def _fold_reply(
    section: _Section,
    reply: llm.Reply,
    llm_log: llm.Transcript,
    out: list[Finding],
    all_dropped: list[dict],
    transcript_text: str,
) -> int:
    """Record one reply and fold it into the findings; returns the findings it added.

    One function for both loops, so a reply cannot mean two things: the transcript record --
    which ADR-0004 D4 makes provenance -- and the OK/abstain/parse handling all happen here, in
    the order the caller walks the sections.
    """
    # `section.index` is the call's position in the note. Recorded so the transcript writes the
    # calls in that order (ADR-0004 D4) rather than in the order this loop happens to walk them
    # (#153); `llm.Transcript.as_dict` is where that order is applied.
    llm_log.record(
        9, f"critic: {section.heading[:40]}", section.messages, reply, section=section.index
    )
    text = reply.text.strip()
    if text.upper() == "OK":
        return 0
    if text.upper() in ABSTAIN:
        # An abstention is a result. It is recorded so that a section which was
        # reviewed and produced nothing is distinguishable from one nobody read.
        out.append(
            Finding("tier2", "critic/abstained", "INFO", 0, "the auditor was not sure", text)
        )
        return 0
    kept, dropped = parse_critic_findings(text, section.text, transcript_text)
    for entry in dropped:
        entry["section"] = section.heading[:60]
    out.extend(kept)
    all_dropped.extend(dropped)
    return len(kept)


def tier2_critic(
    markdown: str,
    transcript_text: str,
    config: llm.Config,
    llm_log: llm.Transcript,
    limit: int = 6,
    stream=None,
) -> tuple[list[Finding], list[dict], dict]:
    """A zero-shot critic that is given the transcript it is auditing the note against.

    Sections are sent one at a time and the first `limit` findings are kept: one request
    per section keeps each call's context small, and a cap keeps the audit's cost bounded
    and its latency predictable. The cap is reported, not silently applied.

    `DYAK_LLM_CONCURRENCY` changes what the cap *is*, and the two meanings do not produce the
    same audit (#153). At 1, the default, the cap stops requests: a section reached once
    `limit` findings exist is never sent, and it is reported in `skipped`. Above 1 every
    section is sent -- there is no "so far" to stop on -- and the cap becomes a limit on the
    findings that survive, cut after the loop with the remainder counted in `cap_dropped`.
    That second reading is the owner's decision on #153, taken rather than guessed. A reader of
    the artefact can tell which applied: `cap_scope` is `"findings"` only in the second case,
    and the sequential coverage keeps exactly the keys it had before this knob existed.

    Cost, stated: each request carries the section *and* the whole transcript, so the
    stage is N times the transcript in context. That is ADR-0001's own estimate for this
    stage (~106k tokens for a 90-minute lecture), not an accident of the implementation.

    `stream` is appended rather than made keyword-only, so the existing positional callers
    keep working (#197). It is the parameter, not the local `out` below -- `out` is the
    findings list here, and `audit()`'s stream binding happens after this call.
    """
    # The counter counts units *examined*, so on a capped audit it can reach `sections 7/8`
    # while the result line says `5/8 sections reviewed`: the cap being visible, not a
    # contradiction (#197).
    progress = stream if stream is not None else sys.stdout
    sections = re.split(r"^## ", markdown, flags=re.M)[1:]
    workers = llm.concurrency()
    reviewed: list[str] = []
    truncated: list[str] = []
    skipped = 0
    out: list[Finding] = []
    all_dropped: list[dict] = []
    # The cap counts FINDINGS, not answers. An auditor that abstains on six sections has
    # not spent the budget -- letting abstentions consume it would stop the audit reaching
    # the sections where the errors actually are.
    findings_so_far = 0
    cap_dropped = 0

    if workers == 1:
        # The sequential run, kept as the loop it always was: `DYAK_LLM_CONCURRENCY=1` is
        # byte-identical to the behaviour before the knob existed, and the way to keep that a
        # fact rather than a hope is to leave the path that produced it alone.
        for index, part in enumerate(sections):
            # First statement of the body, before the cap and empty-body checks (#197): a section
            # skipped by either still advances the counter, so the cap cannot freeze it. The unit
            # is a note section, and the first line is `sections 0/N` with no interval.
            stages.progress_line(progress, "sections", index, len(sections))
            if findings_so_far >= limit:
                skipped += 1
                continue
            section = _section_request(index, part, transcript_text)
            if section is None:
                skipped += 1
                continue
            reviewed.append(section.heading)
            if section.over_limit:
                # Reported, not silent. A section audited only in part can pass a review of the
                # part that was sent, which is a clean bill of health for text nobody read (#123).
                truncated.append(section.heading)
            try:
                reply = llm.chat(section.messages, config)
            except llm.NotConfiguredError:
                raise
            except llm.EndpointError as exc:
                out.append(Finding("tier2", "critic/unavailable", "WARN", 0, str(exc)))
                break
            findings_so_far += _fold_reply(
                section, reply, llm_log, out, all_dropped, transcript_text
            )
    else:
        # `DYAK_LLM_CONCURRENCY > 1`. The sections are planned and dispatched in note order on
        # this thread, the requests run in workers, and the replies are folded back in the same
        # order -- so the transcript, the findings and the counter are all in section order
        # whatever order the endpoint answers in (#153, ADR-0004 D4).
        plans: list[_Section | None] = []
        for index, part in enumerate(sections):
            # Written here, before the request is dispatched: with the calls in flight the
            # dispatch is what makes the count an observable fact, and a line written per
            # completion would interleave nondeterministically (#197).
            stages.progress_line(progress, "sections", index, len(sections))
            section = _section_request(index, part, transcript_text)
            plans.append(section)
            if section is None:
                skipped += 1
        jobs: list[Callable[[], llm.Reply]] = []
        sending: list[_Section] = []
        for section in plans:
            if section is None:
                continue
            sending.append(section)
            jobs.append(lambda section=section: llm.chat(section.messages, config))
        replies = llm.map_bounded(
            jobs,
            workers,
            # The sequential loop breaks at the first failed call, so this one stops
            # dispatching at the first failure too; the calls already in flight finish.
            stop_when=lambda outcome: isinstance(outcome, llm.EndpointError),
        )
        for section, outcome in zip(sending, replies):
            if outcome is None:
                # Left alone after an earlier call failed. The sequential loop stops there as
                # well; the difference is that this one cannot un-send the calls in flight.
                skipped += 1
                continue
            reviewed.append(section.heading)
            if section.over_limit:
                truncated.append(section.heading)
            if isinstance(outcome, llm.NotConfiguredError):
                raise outcome
            if isinstance(outcome, llm.EndpointError):
                out.append(Finding("tier2", "critic/unavailable", "WARN", 0, str(outcome)))
                continue
            findings_so_far += _fold_reply(
                section, outcome, llm_log, out, all_dropped, transcript_text
            )
        # The cap as a report limit (the owner's decision on #153): every section was sent, so
        # what is left to bound is what the report holds.
        cap_dropped = max(0, len(out) - limit)
        out = out[:limit]

    # The cap is part of the result, not an implementation detail. A reader who sees
    # "3 sections reviewed" in a note they know has eight must be told the other five were
    # not examined -- an unstated cap reads as a clean bill of health.
    coverage = {
        "sections": len(sections),
        "reviewed": reviewed,
        "skipped": skipped,
        "finding_cap": limit,
        # The two readings of the cap, and which one this run took. Sequentially it is the
        # sections the cap kept from being sent; concurrently nothing is left unsent, so it is
        # the findings it cut. `cap_scope` and `cap_dropped` are added below rather than here:
        # at 1 the artefact keeps the exact keys it had before the knob existed, which is what
        # "an unset variable changes nothing" has to mean for a bundle that is diffed.
        "capped": (skipped > 0 and findings_so_far >= limit) if workers == 1 else cap_dropped > 0,
        "dropped": len(all_dropped),
        "transcript_chars": len(transcript_text.strip()),
        "section_chars": SECTION_CHARS,
        "truncated": truncated,
        "enforced": "quote verbatim in note, citation verbatim in transcript, confidence stated",
    }
    if workers > 1:
        coverage["cap_scope"] = "findings"
        coverage["cap_dropped"] = cap_dropped
    return (out, all_dropped, coverage)


# --- orchestration -------------------------------------------------------------------


def audit(
    markdown: str,
    transcript: str,
    glossary: Glossary,
    *,
    config: llm.Config | None = None,
    llm_transcript: llm.Transcript | None = None,
    tier2_limit: int = 6,
    stream=None,
) -> Report:
    from .lint import _outside_code

    lines = _outside_code(markdown)
    report = Report()
    check_term_drift(markdown, glossary, lines, report)
    check_dimensions(markdown, lines, report)
    check_unsourced_numbers(markdown, transcript, lines, report)
    check_empty_math(markdown, lines, report)

    if config is None:
        report.tier2 = "NOT_CONFIGURED"
    else:
        try:
            found, dropped, coverage = tier2_critic(
                markdown,
                transcript,
                config,
                llm_transcript or llm.Transcript(),
                tier2_limit,
                stream=stream,
            )
        except llm.NotConfiguredError as exc:
            report.tier2 = f"NOT_CONFIGURED: {exc}"
            found, dropped = [], []
        else:
            report.tier2 = (
                f"ran, {len(found)} findings, {len(dropped)} discarded, "
                f"{len(coverage['reviewed'])}/{coverage['sections']} sections reviewed"
            )
            if coverage.get("cap_scope") == "findings":
                # The line is where a cap is believed (#123). With the sections concurrent the
                # cap no longer leaves a section unexamined, so the count it hid is the
                # findings it cut, and saying "capped at 6" alone would read as six examined
                # and none lost.
                report.tier2 += (
                    f" (capped at {tier2_limit} findings; "
                    f"{coverage['cap_dropped']} collected findings were dropped)"
                )
            elif coverage["capped"]:
                report.tier2 += f" (capped at {tier2_limit} findings)"
            report.tier2_findings = len(found)
            report.tier2_coverage = coverage
            report.tier2_truncated = list(coverage.get("truncated", []))
            if report.tier2_truncated:
                # In the run line, not only in the artefact: a section reviewed in part reads
                # as reviewed, and the line is where that would be believed (#123).
                report.tier2 += (
                    f" ({len(report.tier2_truncated)} section(s) truncated at "
                    f"{SECTION_CHARS} chars)"
                )
            report.dropped.extend(dropped)
            report.findings.extend(found)

    out = stream if stream is not None else sys.stdout
    for finding in report.findings[:10]:
        out.write(f"           {finding.severity} {finding.rule}: {finding.detail}\n")
    if len(report.findings) > 10:
        out.write(f"           ... and {len(report.findings) - 10} more in audit.json\n")
    if report.dropped:
        out.write(
            f"           note: {len(report.dropped)} critic findings were discarded for "
            "want of a verbatim citation; they are listed in audit.json under `dropped`\n"
        )
    if report.tier2_truncated:
        out.write(
            f"           note: {len(report.tier2_truncated)} section(s) exceeded "
            f"{SECTION_CHARS} characters and were audited only in part: "
            f"{', '.join(report.tier2_truncated)}\n"
        )
    if report.tier2 == "NOT_CONFIGURED":
        out.write(
            "           note: tier 2 did not run. Tier 1 rules ran; the mathematics has "
            "not been reviewed by a second model.\n"
        )
    return report


def run(
    note: Path,
    transcript_txt: Path,
    destination: Path,
    *,
    glossary_path: Path | None = None,
    config: llm.Config | None = None,
    stream=None,
) -> Report:
    destination.mkdir(parents=True, exist_ok=True)
    markdown = note.read_text(encoding="utf-8") if note.is_file() else ""
    transcript = transcript_txt.read_text(encoding="utf-8") if transcript_txt.is_file() else ""
    glossary = (
        Glossary.parse(glossary_path.read_text(encoding="utf-8"))
        if glossary_path and glossary_path.is_file()
        else Glossary()
    )
    trace = llm.Transcript()
    report = audit(
        markdown, transcript, glossary, config=config, llm_transcript=trace, stream=stream
    )
    (destination / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / PROVENANCE_NAME).write_text(provenance(report) + "\n", encoding="utf-8")
    if trace.calls:
        trace.write(destination / LLM_TRANSCRIPT_NAME)
    return report


def provenance(report: Report) -> str:
    return json.dumps(
        {
            "stage": 9,
            "tool": "dyak-audit-v1",
            "tier1": {
                "requires_model": False,
                "rules": [
                    "term/drift",
                    "dimension/mismatch",
                    "number/unsourced",
                    "math/empty",
                ],
                "scope": (
                    "rank agreement in sums over declared shapes. NOT a proof checker: a "
                    "wrong constant, a transposed index and a correct formula for the wrong "
                    "problem are all outside it."
                ),
                "rules_run": report.tier1_rules_run,
            },
            "tier2": {
                "requires_model": True,
                "status": report.tier2,
                "findings": report.tier2_findings,
                "discarded": len(report.dropped),
                "coverage": report.tier2_coverage,
                "context": (
                    "the note AND the raw transcript (ADR-0001 D5). Never stage 7's "
                    "instructions and never stage 7's conversation: the critic did not "
                    "write the note, but it is told what the lecturer actually said"
                ),
                "enforced": (
                    "every finding must quote the note verbatim, cite the transcript "
                    "verbatim, and state a confidence. Anything else is discarded and "
                    "logged under `dropped` -- the filter does not hide its own discards"
                ),
                "cost": (
                    "one request per section, each carrying the whole transcript: N times "
                    "the transcript in context. ADR-0001 measured this stage at ~106k "
                    "tokens for a 90-minute lecture"
                ),
            },
            "severity": {
                "ERROR": "exit 5, blocks the note",
                "WARN": "annotated, the note is still written",
                "INFO": "a check that could not run, an abstention, or a skipped section",
            },
        },
        indent=2,
        ensure_ascii=False,
    )
