"""Stage 7: turn an aligned lecture into a structured note.

The first stage that needs a model, and the only place the pipeline is allowed to be
creative. Everything before it measures; everything after it checks.

## Two implementations of one protocol

`Synthesizer` is a Protocol with two implementations, and the choice is recorded in
provenance either way:

- `LLMSynthesizer` calls the configured endpoint, one request per section.
- `TemplateSynthesizer` emits the same eight-section skeleton from the transcript alone,
  with no model at all.

`TemplateSynthesizer` exists so the pipeline is runnable and testable end to end without a
GPU, and so the failure mode of an unavailable endpoint is a *worse note* rather than no
note. The note must survive a failed synthesis; the template is that guarantee
made concrete rather than aspirational. It is also the oracle: if the LLM output is worse
than a blank skeleton, the model is not earning its place in the pipeline.

## Why per-section, not one request

A 4 500-second lecture is 152 000 characters of ASR. That does not fit a context window with
room for instructions, and truncation from the wrong end loses the end of the lecture -- the
part a student reads the night before the exam. Sections are cut on frame boundaries, so
each request is bounded by something that means something visually rather than by a
character count that means nothing.

## The eight sections are not negotiable

The synthesizer skill fixes them, and stage 8 lints against them. A section with no material
in the transcript is written as an explicit "not covered" line. An invented section is a lie
about the lecture; a missing one is a lie about the note's completeness. The second is
recoverable, the first is not.

## Where the structure comes from

The eight sections are **not** defined by any ADR. They come from the synthesizer skill,
§ "Структура выходного документа":

    ~/.dsh/skills/academic-lecture-synthesizer/SKILL.md

References to "the skill" throughout this module mean that file. The skill uses no `D<n>`
notation of its own, so a section reference can never be resolved to a numbered decision —
which is why they are named by name rather than by number.
## Where the two passes of the skill live

The skill is organised as pass 1 (clean the ASR against `glossary.md`) and pass 2 (synthesize),
and forbids mixing them. Stage 7 is pass 2 and receives raw ASR on purpose: it does not clean,
because a paraphrase written in the wrong request is unverifiable prose. What pass 1 covers
falls where it can be **checked** rather than trusted: `audit.py` finds glossary variants in
the finished note and `repair.py` substitutes the canonical term, deterministically, against
the same glossary file -- so a term that was cleaned is cleaned because a rule fired, not
because a model said it had cleaned it.

The system prompt below carries the skill's binding rules -- boundary of trust, LaTeX notation,
the dimension check, `[реконструкция]`, the per-family "why works / where breaks / trade-off"
requirement, and the organisational-content separation -- condensed to what a per-section
writer can act on. A rule with no counterpart in the skill does not belong in that prompt;
that is what keeps the two contracts from drifting apart again.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import caption, llm

REPORT_NAME = "synth.json"
PROVENANCE_NAME = "synth-provenance.json"
NOTE_NAME = "note.md"
#: The stage-7 calls, verbatim. Written to the stage's own directory since #38 and never
#: promoted from it until #76 -- so the note in the bundle was written by 8 calls the bundle
#: did not record. ADR-0004 D4: a regression gate that cannot attribute a difference is not
#: a gate. Stage 9 names its equivalent `audit-llm-transcript.json`.
LLM_TRANSCRIPT_NAME = "synth-llm-transcript.json"
#: What `pipeline.publishes_stage7_transcript` promotes when stage 7 runs.
STAGE7_ARTEFACTS = (NOTE_NAME, REPORT_NAME, PROVENANCE_NAME)

#: the skill's document structure. `(number, title, one line on what belongs here)`. The lint stage checks
#: the note against this list, so a section title that drifts here drifts in two places.
#:
#: The titles are the skill's own headings, `SKILL.md` § "Структура выходного документа",
#: **verbatim**. The first generation here paraphrased four of them ("Карта источников",
#: "Теоретический конспект и методы", "Математический фундамент", "Вопросы для
#: самопроверки"), and the lint stage then certified a document whose headings did not match
#: the skill it claims to implement: the check passed because both places carried the same
#: drift. The briefs are the skill's per-section requirements, compressed to what a
#: per-section request can state -- the full wording lives in the skill, not here.
SECTIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "Паспорт лекции",
        "предмет, тема, лектор/курс, номер пары; Executive Summary — 2–3 предложения: о чём лекция и какую инженерную задачу она решает",
    ),
    (
        2,
        "Организационный блок",
        "требования, формат сдачи, дедлайны, лабораторные, специфика экзамена, позиция лектора — весь организационный материал только здесь",
    ),
    (
        3,
        "Карта источников и литературы",
        "всё упомянутое — курсы, авторы, книги, репозитории, статьи, инструменты — таблицей «Источник | Контекст в лекции | Зачем»; недостающие характеристики помечать, не додумывать",
    ),
    (
        4,
        "Понятийный аппарат и постановка задачи",
        "термины с определениями; математическая формализация: состояние, управление, наблюдение, цель — с явными размерностями; ограничения; критерии качества с явным «максимизируется/минимизируется»",
    ),
    (
        5,
        "Теоретический конспект и систематизация методов",
        "классификация методов; по каждому семейству: идея → формулировка → плюсы → минусы → границы применимости → trade-off; где кончается зона гарантий",
    ),
    (
        6,
        "Математический ликбез / фундамент",
        "формулы, разложения, линеаризация, свойства матриц — каждая конструкция с практическим смыслом для инженера, а не только вывод",
    ),
    (
        7,
        "Численная реализация и инструменты",
        "сравнение подходов к вычислениям, библиотеки с их ролью в задаче, код или псевдокод, ловушки реализации",
    ),
    (
        8,
        "Вопросы для самопроверки и подготовки к экзамену",
        "5–7 вопросов на понимание, каждый с кратким критерием правильного ответа — экзаменационная формулировка, не пересказ параграфа",
    ),
)

NOT_COVERED = "в лекции не затрагивается"

#: The skill's binding rules, condensed to what a per-section writer can act on. Every rule
#: here names its skill counterpart: the boundary of trust (§ "Проход 1", "Граница доверия"),
#: the math rules (§ "Проход 2. Правила математики"), the content rules (§ "Проход 2. Правила
#: содержания") and the organisational-content separation.
SYSTEM = """\
Ты — конспектор инженерной лекции (навык academic-lecture-synthesizer, проход 2: синтез).
Пиши на русском, в разметке Markdown.

ТЕРМИНЫ И ГРАНИЦА ДОВЕРИЯ:
1. Только то, что прозвучало в транскрипте. Ничего не добавляй от себя. Пояснение
   от себя помечай [инженерная заметка].
2. Формулу, продиктованную словами и не восстановленную однозначно, дай в
   стандартной форме и помечай [реконструкция].
3. Фрагмент, который не восстанавливается однозначно, помечай
   ⚠️ [неразборчиво: <буквально что услышали>]. Выдуманная формула хуже пропуска.
4. Если раздел пуст — напиши одну строку «в лекции не затрагивается».
   Не выдумывай то, чего в лекции не было.

МАТЕМАТИКА:
5. Вся математика в LaTeX: $...$ строчная, $$...$$ выключная. Векторы
   \\mathbf{x}, матрицы \\mathbf{A}, скаляры без выделения — один стиль на весь текст.
6. Проверь размерности каждого равенства: скаляр не складывается с матрицей, вектор
   с вектором — только через inner product или покомпонентно, индексы произведения
   согласованы по всем вхождениям. Размерности и области определения укажи явно:
   $\\mathbf{x} \\in \\mathbb{R}^n$, $\\mathbf{A} \\in \\mathbb{R}^{m \\times n}$.

СОДЕРЖАНИЕ:
7. Каждому семейству методов нужны три вещи: почему работает, где ломается,
   чем жертвуем (trade-off). Требования, дедлайны, экзамен — не сюда: их пишет
   организационный блок (раздел 2).
8. Не пересказывай структуру этого документа. Не пиши воду.

ФОРМАТ: ответ — только тело раздела, без заголовка верхнего уровня и без обращений
к читателю. Разговорную речь («ну», «как бы», «вы поняли») вычисти."""


@dataclass(frozen=True)
class Section:
    """One slice of the lecture: the frames on screen while it was spoken."""

    number: int
    title: str
    brief: str
    start_ms: int
    end_ms: int
    text: str
    frames: tuple[str, ...] = ()
    word_count: int = 0
    #: `(frame filename, caption)` per frame, in order. Stage 6 computed these and stage 7 was
    #: given only `frames` -- the filenames, which carry no information about the slide. 57
    #: captions / 55 404 characters reached no one, and the writer described a lecture it could
    #: not see (#84). Refusals are replaced at build time with prose, so the sentinel string
    #: never reaches a writer as if it were content.
    captions: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "brief": self.brief,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "frames": list(self.frames),
            # The count, not the texts: `captions.json` already holds the texts, and duplicating
            # 55 kB into `synth.json` would put two copies of the same prose in one bundle with
            # no rule about which is authoritative.
            "captions": len(self.captions),
            "word_count": self.word_count,
        }


@dataclass
class Note:
    markdown: str
    #: Per section: how it was produced and whether it looks complete. A section the model
    #: declined to fill is reported, not silently shipped.
    sections: list[dict] = field(default_factory=list)
    synthesizer: str = "template"
    model: str | None = None
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        return sum(1 for s in self.sections if s.get("present"))

    def as_dict(self) -> dict:
        return {
            "synthesizer": self.synthesizer,
            "model": self.model,
            "degraded": self.degraded,
            "sections": self.sections,
            "notes": self.notes,
            "bytes": len(self.markdown.encode("utf-8")),
        }


@dataclass(frozen=True)
class SectionBody:
    """One section's prose, and how the model stopped writing it.

    A caller that sees only the string cannot tell a section that ended naturally from one the
    endpoint cut off at its output cap. `finish_reason == "length"` is that second case: the
    text is real but incomplete, and shipping it as a clean section is how a truncated
    derivation passes lint and audit as finished content (#124).
    """

    text: str
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@runtime_checkable
class Synthesizer(Protocol):
    """What stage 7 needs from anything that can write prose."""

    name: str

    def section(self, section: Section, transcript: str) -> SectionBody:
        """The body of one of the skill's eight sections. No top-level heading."""


# --- section cutting ---------------------------------------------------------------

#: What stage 7 is told about a frame the captioner refused. The sentinel itself is a claim
#: by a different model ("nothing here that the excerpt does not already state"), and passing
#: it through verbatim would hand a Russian writer an English 19-character string in the
#: position where a slide description belongs. 16 of the 57 captions on lecture 1 are refusals
#: (#83). The wording says what the frame is -- the lecturer was on something already covered
#: -- and does not ask the writer to conclude the lecture has a gap.
CAPTION_REFUSED = "на экране нет содержания сверх стенограммы"


def caption_text(alignment: dict) -> str | None:
    """Stage 6's description of one frame, in a form a writer can use, or None if there is
    nothing to send.

    None means the frame carries no caption at all: either it was gated out, or stage 6
    recorded a status without a string. Both are omitted rather than sent as an empty line,
    because a blank under a filename reads as "nothing was on screen" -- a claim, and an
    unsupported one.
    """
    raw = (alignment.get("caption") or "").strip()
    if not raw:
        return None
    if raw == caption.NO_NEW_INFORMATION:
        return CAPTION_REFUSED
    return raw


def build_sections(alignments: list[dict], limit: int = 12) -> list[Section]:
    """Cut the lecture into bounded slices on frame boundaries.

    Frames, not character counts: a section that ends mid-sentence because it hit a token
    limit is a section nobody can read. Slicing on frame boundaries means each chunk is a
    stretch of lecture during which one thing was on screen.

    Empty frames are collapsed into the preceding slice rather than becoming their own
    section, so a gap in frame detection does not become a heading with no content.
    """
    usable = [a for a in alignments if a.get("caption_status") != "GATED_OUT"]
    if not usable:
        return []
    per = max(1, (len(usable) + limit - 1) // limit)

    out: list[Section] = []
    cursor = 0
    number = 1
    while cursor < len(usable):
        chunk = usable[cursor : cursor + per]
        text = " ".join((c.get("text") or "").strip() for c in chunk).strip()
        frames = tuple(c["frame"] for c in chunk)
        described: list[tuple[str, str]] = []
        for c in chunk:
            text_of_frame = caption_text(c)
            if text_of_frame is not None:
                described.append((c["frame"], text_of_frame))
        out.append(
            Section(
                number=number,
                title=f"Фрагмент {number}",
                brief=f"кадры {chunk[0]['frame']}–{chunk[-1]['frame']}",
                start_ms=chunk[0].get("on_screen_ms", [0, 0])[0],
                end_ms=chunk[-1].get("on_screen_ms", [0, 0])[-1],
                text=text,
                frames=frames,
                captions=tuple(described),
                word_count=sum((c.get("word_count") or 0) for c in chunk),
            )
        )
        number += 1
        cursor += per
    return out


# --- template synthesizer -----------------------------------------------------------


class TemplateSynthesizer:
    """The eight-section skeleton, filled from the transcript. No model.

    Deterministic, and therefore the only synthesizer whose output can be asserted in a test.
    """

    name = "template"

    def __init__(self, source: str, transcript: str, sections: list[Section]) -> None:
        self.source = source
        self.transcript = transcript
        self.sections = sections

    def section(self, section: Section, _transcript: str) -> SectionBody:
        # The template does not invent content. It reports what it has, and says plainly
        # that the prose is missing. A reader can tell this is not a finished note.
        body = (
            f"**Источник:** `{Path(self.source).name}`, {len(self.transcript)} символов "
            f"транскрипта.\n\n"
            f"**Окно:** {section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с, "
            f"{len(section.frames)} кадров, {section.word_count} слов.\n\n"
            f"**Предобработка не выполнена.** Этот конспект собран без модели "
            f"синтеза: доступный текст стенограммы сохранён в `transcript.txt`, "
            f"структура разделов и тайминги кадров — в `captions.json`.\n\n"
            "> Синтез без модели не восстанавливает термины, не оформляет математику и "
            "не отделяет учебный материал от организационного. Это каркас, а не конспект."
        )
        return SectionBody(text=body)


# --- llm synthesizer ----------------------------------------------------------------


class LLMSynthesizer:
    """One request per section, against the configured OpenAI-compatible endpoint."""

    name = "llm"

    def __init__(self, config: llm.Config, transcript: llm.Transcript) -> None:
        self.config = config
        self.transcript = transcript

    def section(self, section: Section, transcript: str) -> str:
        # Truncated from the front of this section's text, never the transcript: the tail
        # of a slide is what a student needs, and a token limit that eats the end is a
        # silent content loss.
        budget = 12_000
        body = transcript if len(transcript) <= budget else transcript[:budget] + "\n[…]\n"
        if section.captions:
            # Before the transcript, not after it: `body` is the only thing `budget` bounds,
            # so anything above it survives truncation. The alternative -- captions last --
            # makes the captions the first thing a long section loses, which is the same
            # defect #84 describes, reached from the other direction.
            described = "\n".join(f"{name} — {text}" for name, text in section.captions)
            on_screen = (
                "Описания кадров (стадия 6): что было на экране, пока шёл этот фрагмент.\n"
                "Это не цитата лектора. Описание экрана не даёт права добавить в конспект "
                "утверждение, которого нет в стенограмме: бери отсюда термин, формулу, "
                "название или структуру слайда, а утверждения о том, что было сказано, "
                "только из стенограммы.\n\n"
                f"{described}\n\n"
            )
        else:
            on_screen = f"Кадры на экране: {', '.join(section.frames) or 'нет'}.\n\n"
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Раздел {section.number} из 8: «{section.title}».\n"
                    f"Что в нём должно быть: {section.brief}\n\n"
                    f"Тайминг: {section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с.\n\n"
                    f"{on_screen}"
                    f"Стенограмма этого фрагмента:\n\n{body}\n"
                ),
            },
        ]
        reply = llm.chat(messages, self.config)
        self.transcript.record(7, f"section {section.number}", messages, reply)
        return SectionBody(text=reply.text, finish_reason=reply.finish_reason)


# --- note assembly ------------------------------------------------------------------


def assemble(note_title: str, bodies: dict[int, str], sections: list[Section]) -> str:
    """Interleave the eight required sections with whatever the synthesizer produced.

    The headings come from `SECTIONS`, never from the model. A model that emits its own
    heading would renumber the note, and stage 8's structural lint would then be checking a
    document that changed shape to satisfy it.
    """
    # The provenance footer goes directly under the title, not at the end. Appended last it
    # would land inside the final section's body -- and stage 8's
    # `claims-empty-but-is-not` rule then reads the footer as that section's content, which
    # is a false positive caused by where the text sits, not by what it says.
    parts = [f"# {note_title}", ""]
    if sections:
        parts.append(
            "> Собрано из выравненных кадров и стенограммы. Тайминги — `captions.json`, "
            "решения качества — `quality.json`, структура синтеза — `synth.json`."
        )
        parts.append("")
    for number, title, brief in SECTIONS:
        body = bodies.get(number, "").strip()
        parts.append(f"## {number}. {title}")
        parts.append("")
        if not body:
            parts.append(NOT_COVERED)
        else:
            parts.append(body)
        parts.append("")
    return "\n".join(parts)


#: A heading that only exists in the document's own table of contents.
_TOC = re.compile(r"^\s{0,3}#{1,6}\s*(содержание|оглавление)\b", re.IGNORECASE)

#: `#` at any level. The eight section headings are emitted by `assemble` as `## N. Title`;
#: nothing inside a section body may sit at that level or above, or the document outline
#: gains unnumbered siblings of the sections and the eight-section structure stops being
#: the top level of anything.
_HEADING = re.compile(r"^(\s{0,3})(#{1,6})(\s+.*)$")


def normalise(body: str) -> str:
    """Push every heading in a section body below `##`, preserving relative nesting.

    The shallowest heading in the body becomes `###`; the rest keep their distance from it.
    Working from the minimum rather than demoting a fixed level keeps `####` under a `###`
    from climbing above it -- mapping every `#` to `###`, as this did before, collapsed a
    body that mixed `##` and `####` into a flat one.

    Demoting rather than deleting: the heading usually carries real information -- which
    slide the fragment was -- and the failure to preserve it is worse than the extra level.
    Stripping it would leave a note with unexplained structural breaks in it.

    Measured on lecture 1, 2026-10-04: leaving `##` alone produced 29 unnumbered `##`
    headings as siblings of the eight `## N.` sections, so `## N.` stopped being the top
    level of the document and stage 8's structural rules checked a different outline than
    the one a reader sees.
    """
    levels = [len(m.group(2)) for line in body.splitlines() if (m := _HEADING.match(line))]
    if not levels:
        return body
    # `##` is the section level; a body heading may not be at or above it. The shift is
    # 3 - min, clamped at 0 so a body that starts at `####` is not *promoted*, and the
    # result is clamped at 6 so a deep body cannot produce `########`, which is not a
    # heading in Markdown, it is a paragraph.
    shift = max(0, 3 - min(levels))

    def demote(match: re.Match[str]) -> str:
        indent, hashes, rest = match.group(1), match.group(2), match.group(3)
        return f"{indent}{'#' * min(6, len(hashes) + shift)}{rest}"

    return "\n".join(_HEADING.sub(demote, line) for line in body.splitlines())


def synthesise(
    alignments: list[dict],
    transcript_text: str,
    source: Path,
    synthesizer: Synthesizer,
    *,
    title: str | None = None,
    model: str | None = None,
) -> Note:
    sections = build_sections(alignments)
    note_title = title or f"Конспект — {Path(source).name}"
    bodies: dict[int, str] = {}
    reported: list[dict] = []
    warnings: list[str] = []
    breaker = llm.Breaker()
    stopped = False

    # The eight content sections map onto the numbered slices in order. A lecture with two
    # frames cannot support eight sections and pretending otherwise would be the exact
    # invention the skill forbids.
    for index, (number, section_title, brief) in enumerate(SECTIONS):
        payload = sections[index].text if index < len(sections) else ""
        if not payload.strip():
            reported.append(
                {
                    "number": number,
                    "title": section_title,
                    "present": False,
                    "reason": "no source material for this section",
                }
            )
            continue
        if stopped:
            # Seven more ladders against an endpoint already known to be down is the cost
            # #124 is about, and an unattempted section is not a failed synthesis.
            warnings.append(
                f"section {number}: not attempted; stage 7 stopped after "
                f"{breaker.limit} consecutive connection failures"
            )
            reported.append(
                {
                    "number": number,
                    "title": section_title,
                    "present": False,
                    "reason": "not attempted: stage 7 stopped after repeated connection failures",
                }
            )
            continue
        finish_reason: str | None = None
        try:
            outcome = synthesizer.section(sections[index], payload)
        except llm.NotConfiguredError as exc:
            warnings.append(f"section {number}: {exc}")
            body = ""
        except llm.EndpointError as exc:
            # One failed section must not lose the seven that succeeded. The note
            # survives a failed synthesis.
            warnings.append(f"section {number}: {exc}")
            body = ""
            if exc.connection:
                stopped = breaker.fail()
            else:
                breaker.ok()
        else:
            body = outcome.text
            finish_reason = outcome.finish_reason
            breaker.ok()
            if outcome.truncated:
                warnings.append(
                    f"section {number}: model output hit the length limit; the section is "
                    "truncated and its ending is missing"
                )
        first = body.splitlines()[0] if body.splitlines() else ""
        if first and _TOC.match(first):
            # A model told "section 5 of 8" will sometimes answer with the contents of the
            # whole document. Shipping that as section 5 duplicates eight headings and puts
            # a table of contents where the mathematics belongs.
            body = ""
            warnings.append(f"section {number}: model emitted a table of contents, discarded")
        else:
            body = normalise(body)
        bodies[number] = body
        reported.append(
            {
                "number": number,
                "title": section_title,
                "present": bool(body.strip()),
                "bytes": len(body.encode("utf-8")),
                "source_section": sections[index].title,
                "finish_reason": finish_reason,
            }
        )

    markdown = assemble(note_title, bodies, sections)
    missing = [n for n, _, _ in SECTIONS if not bodies.get(n, "").strip()]
    name = getattr(synthesizer, "name", "unknown")
    # Falling back to the template is itself a degradation, independent of whether the
    # sections came out filled. A template note has eight headings and no synthesis behind
    # them; reporting it as a clean run is the silent-degradation failure that
    # ADR-0001 D2 is written against.
    # against, and `process` will now refuse to exit 0 on one.
    used_template = name == "template"
    if used_template and "synthesis did not run: no model endpoint configured" not in warnings:
        warnings.append("synthesis did not run: no model endpoint configured")
    return Note(
        markdown=markdown,
        sections=reported,
        synthesizer=name,
        model=model,
        degraded=used_template or bool(warnings or missing),
        notes=warnings,
    )


def choose(env_config: llm.Config | None, fallback: Synthesizer) -> Synthesizer:
    """The configured synthesizer, or the template. Records which, without hiding it."""
    return LLMSynthesizer(env_config, llm.Transcript()) if env_config else fallback


def run(
    captions: Path,
    transcript_txt: Path,
    source: Path,
    destination: Path,
    *,
    config: llm.Config | None = None,
    title: str | None = None,
    stream=None,
) -> Note:
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    alignments = json.loads(captions.read_text(encoding="utf-8")).get("alignments", [])
    transcript = transcript_txt.read_text(encoding="utf-8") if transcript_txt.is_file() else ""

    try:
        config = config if config is not None else llm.Config.from_env()
    except llm.NotConfiguredError:
        config = None

    synthesizer = choose(config, TemplateSynthesizer(source, transcript, []))
    note = synthesise(
        alignments,
        transcript,
        source,
        synthesizer,
        title=title,
        model=config.model if config else None,
    )

    (destination / NOTE_NAME).write_text(note.markdown, encoding="utf-8")
    write(note, destination / REPORT_NAME)
    (destination / PROVENANCE_NAME).write_text(provenance(note, config) + "\n", encoding="utf-8")
    if isinstance(synthesizer, LLMSynthesizer):
        synthesizer.transcript.write(destination / LLM_TRANSCRIPT_NAME)

    for warning in note.notes[:5]:
        out.write(f"           note: {warning}\n")
    return note


def write(note: Note, target: Path) -> None:
    target.write_text(
        json.dumps(note.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def provenance(note: Note, config: llm.Config | None) -> str:
    return json.dumps(
        {
            "stage": 7,
            "tool": "dyak-synth-v1",
            "requires_model": False,
            "synthesizer": note.synthesizer,
            "model": note.model,
            "endpoint": config.redacted() if config else None,
            "degraded": note.degraded,
            "sections_required": [n for n, _, _ in SECTIONS],
            "sections_filled": [s["number"] for s in note.sections if s.get("present")],
            "why_two_implementations": (
                "TemplateSynthesizer runs with no model so the pipeline is end-to-end "
                "runnable and testable; it is also the oracle an LLM output must beat. "
                "The note must survive a failed synthesis."
            ),
        },
        indent=2,
        ensure_ascii=False,
    )
