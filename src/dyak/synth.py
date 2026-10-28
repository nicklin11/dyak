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

The eight sections are different *jobs*, not different stretches of the lecture. A passport, an
exam-question set and a mathematics primer cannot be written by one prompt without one of them
dominating the answer. So each request carries the section's own brief -- and **the whole
lecture**, because the material for `Математический ликбез` is wherever the mathematics was
spoken, not wherever a frame boundary happened to fall.

This was not always so. Until #117 each request received one time slice, so `Вопросы для
самопроверки` was written from whatever was said between 2695 and 3079 seconds, and on a
58-frame lecture the last four slices reached no request at all: about 31% of the lecture was
absent from a note stage 12 verified as complete. The prompt also named the section
"Фрагмент 7" rather than its skill title, so the writer was never told what it was writing. A
time window is not a topic, and a slice label is not a brief.

`SECTION_BUDGET` bounds one request against a runaway, and it is not a cost control: stage 9
already sends the whole transcript in every request. What the budget keeps out is recorded as
reduced coverage (`Note.coverage`) and named in the run's warnings, so a shortened note is a
reported fact rather than a smaller note.

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

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import caption, exitcodes as ec, llm, runner, stages

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
#:
#: That compression is what dropped the skill's table rules: §4, §5 and §7 each ask for a
#: table, and none of the three briefs named one. The generated note has tables in sections 3
#: (33 data rows, 5 tables) and 7 (8 data rows, 1 table) -- and §7's is there although its
#: brief named none, so the brief is not the only thing that can produce a table. It is the
#: only place a rule can be *stated*: the skill file itself is never sent to the model
#: (`synthesise` sends `SYSTEM` plus this brief), so editing only `SKILL.md` changes nothing
#: at run time. The four rules are back below, and `tests/test_stages.py` fails if one is
#: dropped again.
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
        "термины с определениями — таблицей, если терминов ≥ 3; математическая формализация: "
        "состояние, управление, наблюдение, цель — с явными размерностями; ограничения; "
        "критерии качества с явным «максимизируется/минимизируется»",
    ),
    (
        5,
        "Теоретический конспект и систематизация методов",
        "классификация методов — сравнительной таблицей, если семейств ≥ 3 и есть общие оси "
        "сравнения; по каждому семейству: идея → формулировка → плюсы → минусы → границы "
        "применимости → trade-off; где кончается зона гарантий",
    ),
    (
        6,
        "Математический ликбез / фундамент",
        "формулы, разложения, линеаризация, свойства матриц — каждая конструкция с практическим смыслом для инженера, а не только вывод",
    ),
    (
        7,
        "Численная реализация и инструменты",
        "сравнение подходов к вычислениям — таблицей: точность, стоимость, память, когда что "
        "уместно; библиотеки с их ролью в задаче, код или псевдокод, ловушки реализации",
    ),
    (
        8,
        "Вопросы для самопроверки и подготовки к экзамену",
        "5–7 вопросов на понимание, каждый с кратким критерием правильного ответа — экзаменационная формулировка, не пересказ параграфа",
    ),
)

NOT_COVERED = "в лекции не затрагивается"

#: How much lecture text one section request may carry, in characters.
#:
#: A guard against one unbounded request, not a token budget: stage 9 sends the whole
#: transcript in every request already, so the 12 000 this replaces (#117) bought nothing and
#: cost the note the tail of every slice. A 4 500-second lecture is about 152 000 characters of
#: ASR and fits; a lecture that does not fit is reported as reduced coverage rather than
#: silently shortened.
SECTION_BUDGET = 200_000

#: Fraction of the transcript that must reach the model for the note to count as complete.
#: Below it stage 12 refuses exit 0 with its own code, separate from the missing-artefact code
#: (#117). 0.95 rather than 1.0: a marker or a rounding boundary must not fail a run that sent
#: essentially the whole lecture.
COVERAGE_THRESHOLD = 0.95

#: #74 A2: the subsection rule, as one string, because the style is the owner's preference and
#: changing it must stay one edit. Numbered `N.M` is the recommendation -- it matches the
#: skill's own `### N.` numbering and the handmade baseline, and section 8's self-test can then
#: point at a specific subsection. The generated note numbered sections 2, 4, 5 and 6 and left
#: 1, 3, 7 and 8 unnumbered: inconsistent inside one document, because nothing in `SYSTEM`
#: asked for the same treatment everywhere.
SUBSECTION_RULE = """\
СТРУКТУРА: в каждом разделе, где материал членится, подразделы нумеруй как N.M в одной
строке с заголовком («### 5.2 Сравнительная таблица семейств») — одинаково во всех
разделах, а не по выбору на раздел."""

#: The skill's binding rules, condensed to what a per-section writer can act on. Every rule
#: here names its skill counterpart: the boundary of trust (§ "Проход 1", "Граница доверия"),
#: the math rules (§ "Проход 2. Правила математики"), the content rules (§ "Проход 2. Правила
#: содержания") and the organisational-content separation.
SYSTEM = (
    """\
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
к читателю. Разговорную речь («ну», «как бы», «вы поняли») вычисти.

"""
    + SUBSECTION_RULE
)


@dataclass(frozen=True)
class Section:
    """One unit of writing work: what a single section request is given.

    Two producers. `build_sections` cuts the lecture into time slices, on frame boundaries --
    a frame boundary is where one thing stopped being on screen. `synthesise` then hands every
    request the *whole* lecture, assembled by `gather` (#117). Only the second reaches a model
    today; the slice survives because a frame boundary is worth keeping visible inside the
    payload, and because it is what names a truncation.
    """

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
    #: Words of the transcript that reached a request / words in the transcript. 1.0 when the
    #: whole lecture fits `SECTION_BUDGET`, which is the normal case. Recorded because the note
    #: can otherwise omit a third of the course while every stage reports success (#117).
    coverage: float = 1.0

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
            "coverage": round(self.coverage, 4),
            "coverage_threshold": COVERAGE_THRESHOLD,
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


# --- the whole lecture as one payload -----------------------------------------------


@dataclass(frozen=True)
class Lecture:
    """The whole lecture as one request payload, and how much of it the budget let through.

    `text` is what every section request carries. `dropped` names the slices the budget kept
    out, so a shortened payload is a reported fact rather than a quietly smaller note.
    """

    text: str
    frames: tuple[str, ...] = ()
    captions: tuple[tuple[str, str], ...] = ()
    start_ms: int = 0
    end_ms: int = 0
    words_sent: int = 0
    words_total: int = 0
    dropped: tuple[str, ...] = ()

    @property
    def coverage(self) -> float:
        """Words that reached a request / words in the transcript. 1.0 when nothing was cut."""
        if not self.words_total:
            return 1.0
        return self.words_sent / self.words_total


def transcript_section(transcript_text: str) -> Section:
    """The whole transcript as one slice, for a run where no frame survived.

    `build_sections` cuts on frame boundaries, so it has nothing to cut once stage 5 has
    gated every frame out -- and stage 7 then sent all eight sections an empty payload, so
    the note was eight headings of "no source material" written without a model call while a
    perfectly good transcript sat in the bundle (#345). This is the fallback that keeps it.

    No timing is invented here: with no frames there is nothing to derive one from, and
    `_lecture_block` says "без кадров" rather than printing `0-0 с`.
    """
    text = transcript_text.strip()
    return Section(
        number=1,
        title="",
        brief="",
        start_ms=0,
        end_ms=0,
        text=text,
        word_count=len(text.split()),
    )


def _slice_label(section: Section) -> str:
    """How a slice is named when it does not reach the model."""
    if not section.frames:
        return f"фрагмент {section.number} (без кадров)"
    return (
        f"фрагмент {section.number} ({section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с)"
    )


def _lecture_block(section: Section) -> str:
    """One slice's transcript under a marker naming it.

    The marker is what lets a truncated payload say *which* slice it lost, and it hands the
    writer the frame boundary the slice was cut on -- a frame boundary is where one thing
    stopped being on screen, which is the one thing a time slice is good for.
    """
    if section.frames:
        head = (
            f"[Фрагмент {section.number}: кадры {section.frames[0]}–{section.frames[-1]}, "
            f"{section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с]"
        )
    else:
        head = f"[Фрагмент {section.number}: без кадров, только стенограмма]"
    return f"{head}\n{section.text}"


def gather(sections: list[Section], *, budget: int = SECTION_BUDGET) -> Lecture:
    """Every slice, in order, as the payload for one section request.

    Slices are still cut on frame boundaries (`build_sections`) -- that is where one thing
    stopped being on screen -- but they are *concatenated* here rather than indexed per
    section, because the eight sections are semantic, not temporal (#117).

    Slices are added while they fit. The first that does not is truncated at the boundary, so
    the head of the lecture always reaches the model, and every slice from there on is named in
    `dropped`. A lecture that fits is sent whole.
    """
    if not sections:
        return Lecture(text="")

    blocks: list[str] = []
    frames: list[str] = []
    captions: list[tuple[str, str]] = []
    used = 0
    words_sent = 0
    dropped: list[str] = []

    for section in sections:
        block = _lecture_block(section)
        if used + len(block) <= budget:
            blocks.append(block)
            used += len(block)
            words_sent += section.word_count
            continue
        room = budget - used
        if room > 0 and not dropped:
            # A slice larger than the whole budget: keep its head rather than sending nothing.
            blocks.append(block[:room] + "\n[…]\n")
            words_sent += round(section.word_count * (room / len(block)))
            # The budget is spent. Without this a later, smaller slice still passes the fit
            # test and lands *after* the gap, so the payload reads slice 1 then slice 12 with
            # nothing in between -- and that slice is then missing from `dropped`, which is the
            # one thing this function exists to prevent.
            used = budget
        dropped.append(_slice_label(section))

    for section in sections:
        frames.extend(section.frames)
        captions.extend(section.captions)

    return Lecture(
        text="\n\n".join(blocks),
        frames=tuple(frames),
        captions=tuple(captions),
        start_ms=sections[0].start_ms,
        end_ms=sections[-1].end_ms,
        words_sent=words_sent,
        words_total=sum(s.word_count for s in sections),
        dropped=tuple(dropped),
    )


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
        window = (
            f"{section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с, "
            f"{len(section.frames)} кадров, {section.word_count} слов"
            if section.frames
            else f"без кадров (только стенограмма), {section.word_count} слов"
        )
        body = (
            f"**Источник:** `{Path(self.source).name}`, {len(self.transcript)} символов "
            f"транскрипта.\n\n"
            f"**Окно:** {window}.\n\n"
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

    def section(self, section: Section, transcript: str) -> SectionBody:
        # `transcript` is the whole lecture, already trimmed to `SECTION_BUDGET` by `gather` --
        # the only place that knows where the slice boundaries are, and so the only place that
        # can name what a truncation dropped (#117). Truncating again here would be a second,
        # unreported loss, which is what the previous `budget = 12_000` was.
        if section.captions:
            # Captions are not counted against the transcript budget: they are the smaller half
            # of the material and the half a slide's term or formula lives in. Counting them
            # would make the frames the first thing a long lecture loses -- #84 reached from
            # the other direction.
            described = "\n".join(f"{name} — {text}" for name, text in section.captions)
            on_screen = (
                "Описания кадров (стадия 6): что было на экране в этой лекции.\n"
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
                    f"Лекция целиком, {section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с. "
                    f"Пиши только этот раздел, но опирайся на всю лекцию: материал для него "
                    f"может быть сказан в любом её месте.\n\n"
                    f"{on_screen}"
                    f"Стенограмма лекции:\n\n{transcript}\n"
                ),
            },
        ]
        reply = llm.chat(messages, self.config)
        # `section.number` is the call's position in the note, and the transcript writes the
        # calls back in that order: with `DYAK_LLM_CONCURRENCY > 1` this record happens in a
        # worker, so the append order is the completion order and ADR-0004 D4 needs the
        # section order instead (#153).
        self.transcript.record(
            7, f"section {section.number}", messages, reply, section=section.number
        )
        return SectionBody(text=reply.text, finish_reason=reply.finish_reason)


# --- note assembly ------------------------------------------------------------------


def assemble(
    note_title: str,
    bodies: dict[int, str],
    sections: list[Section],
    *,
    transcript_only: bool = False,
) -> str:
    """Interleave the eight required sections with whatever the synthesizer produced.

    The headings come from `SECTIONS`, never from the model. A model that emits its own
    heading would renumber the note, and stage 8's structural lint would then be checking a
    document that changed shape to satisfy it.

    `transcript_only` states the run's input in the provenance line. The default line claims
    "выравненных кадров и стенограммы", and a fallback run has no aligned frame: saying so is
    the difference between a note that is missing the screen and one that pretends it is not
    (#345).
    """
    # The provenance footer goes directly under the title, not at the end. Appended last it
    # would land inside the final section's body -- and stage 8's
    # `claims-empty-but-is-not` rule then reads the footer as that section's content, which
    # is a false positive caused by where the text sits, not by what it says.
    parts = [f"# {note_title}", ""]
    if sections:
        if transcript_only:
            parts.append(
                "> Собрано только из стенограммы: ни один кадр не прошёл контроль качества "
                "(см. `quality.json`). Содержимое экрана в конспект не попало."
            )
        else:
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


def _repeats_title(text: str, title: str) -> bool:
    """True when a body heading only says what the section heading above it already says.

    `assemble` writes `## N. Title`; a body that opens with `### N. Title` adds nothing, and
    all eight sections of the 2026-10-12 note did exactly that (#74) -- in three forms:
    `### 1. Паспорт лекции`, `### Раздел 2. Организационный блок` and
    `### Карта источников и литературы`. `text` is the heading line or just its text -- the
    hashes are stripped either way. Equality, not containment: a heading that adds a word
    ("Паспорт лекции и тема") is a real subheading and is kept.
    """

    def flat(value: str) -> str:
        # The hashes, the word `Раздел`, the number and the punctuation are not part of the
        # comparison. The artefact writes all three forms above for the same section, so a
        # comparison that required the digits would miss the third.
        value = re.sub(r"^\s*#{1,6}\s*", "", value)
        value = re.sub(r"^\s*(?:раздел|секция|section)\s*\d*\s*[.)]?\s*", "", value, flags=re.I)
        value = re.sub(r"^\s*\d+(?:[.)]\d+)*[.)]?\s*", "", value)
        return re.sub(r"[^\w]+", " ", value).strip().casefold()

    return bool(flat(text)) and flat(text) == flat(title)


def normalise(body: str, title: str | None = None) -> str:
    """Push every heading in a section body below `##`, preserving relative nesting.

    The shallowest heading in the body becomes `###`; the rest keep their distance from it.
    Working from the minimum rather than demoting a fixed level keeps `####` under a `###`
    from climbing above it -- mapping every `#` to `###`, as this did before, collapsed a
    body that mixed `##` and `####` into a flat one.

    Demoting rather than deleting: the heading usually carries real information -- which
    slide the fragment was -- and the failure to preserve it is worse than the extra level.
    Stripping it would leave a note with unexplained structural breaks in it. The one
    exception is a heading that repeats the section's own `title`, which carries no
    information at all and is dropped (#74) -- see `_repeats_title`.

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
    #
    # Computed over every heading, including a repeated title that is about to be dropped:
    # the headings that stay must keep the levels they would have had without the drop.
    shift = max(0, 3 - min(levels))
    kept: list[str] = []
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match is None:
            kept.append(line)
            continue
        indent, hashes, rest = match.group(1), match.group(2), match.group(3)
        if title is not None and _repeats_title(rest, title):
            continue
        kept.append(f"{indent}{'#' * min(6, len(hashes) + shift)}{rest}")
    return "\n".join(kept)


def _section_target(lecture: Lecture, number: int, title: str, brief: str) -> Section:
    """One section as the writer is asked for it.

    The skill's own number, title and brief -- not the slice's "Фрагмент N" (#117). A writer
    that is not told it is writing "Паспорт лекции" cannot write one. Every request carries the
    whole lecture, so the payload is `lecture.text` and the slices are only a frame boundary
    kept visible inside it.
    """
    return Section(
        number=number,
        title=title,
        brief=brief,
        start_ms=lecture.start_ms,
        end_ms=lecture.end_ms,
        text=lecture.text,
        frames=lecture.frames,
        captions=lecture.captions,
        word_count=lecture.words_sent,
    )


def _unattempted(number: int, title: str, limit: int) -> tuple[str, dict]:
    """What a section the breaker never sent is reported as: the note, and the section record.

    Shared by the sequential `stopped` branch and the concurrent one, because "not attempted"
    has to read the same in both -- it is the difference between a section that failed and a
    section nobody asked for (#124).
    """
    return (
        f"section {number}: not attempted; stage 7 stopped after "
        f"{limit} consecutive connection failures",
        {
            "number": number,
            "title": title,
            "present": False,
            "reason": "not attempted: stage 7 stopped after repeated connection failures",
        },
    )


def _finish_section(
    number: int, title: str, body: str, finish_reason: str | None
) -> tuple[str, dict, list[str]]:
    """Normalise one section's body and describe it: `(body, record, warnings)`.

    The TOC discard and the heading demotion are the same in both modes. A body that reached
    the note one way sequentially and another way concurrently would be a second definition of
    what a section is, and stage 8's structural lint would be checking whichever one it got.
    """
    extra: list[str] = []
    first = body.splitlines()[0] if body.splitlines() else ""
    if first and _TOC.match(first):
        # A model told "section 5 of 8" will sometimes answer with the contents of the
        # whole document. Shipping that as section 5 duplicates eight headings and puts
        # a table of contents where the mathematics belongs.
        body = ""
        extra.append(f"section {number}: model emitted a table of contents, discarded")
    else:
        body = normalise(body, title)
    return (
        body,
        {
            "number": number,
            "title": title,
            "present": bool(body.strip()),
            "bytes": len(body.encode("utf-8")),
            "finish_reason": finish_reason,
        },
        extra,
    )


def synthesise(
    alignments: list[dict],
    transcript_text: str,
    source: Path,
    synthesizer: Synthesizer,
    *,
    title: str | None = None,
    model: str | None = None,
    budget: int = SECTION_BUDGET,
    stream=None,
) -> Note:
    out = stream if stream is not None else sys.stdout
    slices = build_sections(alignments)
    # No frame survived stage 5 (or the source has none): the transcript is the only input
    # there is. Without this a gate failure erased the lecture -- `gather([])` returned
    # `Lecture(text="")` and all eight sections were written as "no source material" without
    # a single model call, while 1 308 transcribed words sat in the bundle (#345).
    transcript_only = not slices and bool(transcript_text.strip())
    if transcript_only:
        slices = [transcript_section(transcript_text)]
    # Every section request carries the whole lecture (#117). The slices are not indexed per
    # section: they exist to keep a frame boundary visible inside the payload, not to bound it.
    lecture = gather(slices, budget=budget)
    note_title = title or f"Конспект — {Path(source).name}"
    bodies: dict[int, str] = {}
    reported: list[dict] = []
    warnings: list[str] = []
    if transcript_only:
        gated = sum(1 for a in alignments if a.get("caption_status") == "GATED_OUT")
        warnings.append(
            (f"stage 5 rejected all {gated} frames" if gated else "the source produced no frames")
            + "; the note was written from the transcript alone and carries no screen content"
        )
    breaker = llm.Breaker()
    stopped = False
    workers = llm.concurrency()
    targets = [_section_target(lecture, number, title, brief) for number, title, brief in SECTIONS]

    if lecture.dropped:
        warnings.append(
            f"the transcript did not fit the {budget}-character budget: "
            + ", ".join(lecture.dropped)
            + f" did not reach the model; coverage {lecture.coverage:.0%}"
        )

    if workers > 1 and lecture.text.strip():
        # `DYAK_LLM_CONCURRENCY > 1`: the eight requests are dispatched in section order on
        # this thread and the replies are folded back in that order below, so
        # `synth-llm-transcript.json` and `synth.json` read in section order whatever order the
        # endpoint answers in (#153, ADR-0004 D4). The transcript's own ordering is handled by
        # `llm.Transcript.record`, which is told which section each call belongs to.
        def stop_when(outcome: object) -> bool:
            """The #124 rule, read off the replies in section order instead of mid-loop.

            The same `Breaker` and the same count as the sequential loop: only connection-level
            failures count, and any answer at all resets it. What differs is that the stop is
            decided between replies, so the calls dispatched while those replies were still
            coming back also run: the concurrent arm can spend up to
            `workers + BREAKER_MAX - 1` ladders where the sequential one spends `BREAKER_MAX`.
            """
            if not isinstance(outcome, llm.EndpointError):
                return False
            if outcome.connection:
                return breaker.fail()
            breaker.ok()
            return False

        prefetched = llm.map_bounded(
            [lambda target=target: synthesizer.section(target, lecture.text) for target in targets],
            workers,
            stop_when=stop_when,
        )
    else:
        prefetched = None

    for index, (number, section_title, brief) in enumerate(SECTIONS):
        # First statement of the body, before the no-material and stopped checks (#197): every
        # entry of the eight advances the counter, written or skipped, so no `continue` can
        # freeze it. The first line is `sections 0/8` and carries no interval -- no rate exists
        # for the text endpoint, and `llm.chat` says nothing mid-call.
        stages.progress_line(out, "sections", index, len(SECTIONS))
        if not lecture.text.strip():
            reported.append(
                {
                    "number": number,
                    "title": section_title,
                    "present": False,
                    "reason": "no source material for this section",
                }
            )
            continue
        target = targets[index]
        if prefetched is None:
            if stopped:
                # Seven more ladders against an endpoint already known to be down is the cost
                # #124 is about, and an unattempted section is not a failed synthesis.
                warning, entry = _unattempted(number, section_title, breaker.limit)
                warnings.append(warning)
                reported.append(entry)
                continue
            finish_reason: str | None = None
            try:
                outcome = synthesizer.section(target, lecture.text)
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
        else:
            outcome = prefetched[index]
            if outcome is None:
                # Not dispatched: the breaker had already tripped when this section came up.
                warning, entry = _unattempted(number, section_title, breaker.limit)
                warnings.append(warning)
                reported.append(entry)
                continue
            finish_reason = None
            if isinstance(outcome, llm.NotConfiguredError):
                warnings.append(f"section {number}: {outcome}")
                body = ""
            elif isinstance(outcome, llm.EndpointError):
                # The breaker was updated when this reply came back, in section order, by
                # `stop_when`; counting it again here would trip it at half the failures.
                warnings.append(f"section {number}: {outcome}")
                body = ""
            else:
                body = outcome.text
                finish_reason = outcome.finish_reason
                if outcome.truncated:
                    warnings.append(
                        f"section {number}: model output hit the length limit; the section is "
                        "truncated and its ending is missing"
                    )
        body, entry, extra = _finish_section(number, section_title, body, finish_reason)
        bodies[number] = body
        warnings.extend(extra)
        reported.append(entry)

    markdown = assemble(note_title, bodies, slices, transcript_only=transcript_only)
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
        coverage=lecture.coverage,
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
    budget: int = SECTION_BUDGET,
    stream=None,
) -> Note:
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    alignments, captions_error = caption.read_alignments(captions)
    if captions_error:
        # Stage 7 is the first consumer of stage 6's product, so a file it cannot read stops
        # the run here rather than at stage 12. `name="dyak"`, not the file name: runner.report
        # prints "(<name> wrote nothing to stderr)" and the file variant would be a false claim.
        raise runner.DependencyError(
            name="dyak",
            code=ec.ARTEFACT_VERIFICATION_FAILED,
            message=captions_error,
            remediation=(
                "re-run `dyak process` so stage 6 rewrites it. Unlike the transcript, an absent "
                "captions.json is not legitimate: every frame's alignment is this file, and "
                "reading it as 'no frames' would write a note that silently lost the screen "
                "content (#201)"
            ),
        )
    alignments = alignments or []
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
        budget=budget,
        stream=out,
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


def coverage_of(report_path: Path) -> float | None:
    """The coverage stage 7 recorded in `synth.json`, or None when it cannot be read.

    None is not zero. A bundle from before this metric existed has no coverage figure, and
    treating that as a bad note would report a missing measurement as a content failure; the
    missing-artefact check is the place for files that are not there (#117, #87).
    """
    if not report_path.is_file():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("coverage")
    return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class SectionGates:
    """What stage 7's own record says about the eight sections, read back by stage 12 (#128).

    Two different failures, and they are not the same failure. `not_covered` is a section the
    model left as `NOT_COVERED`; `truncated` is a section it wrote and then had cut off at the
    endpoint's output cap. The first is reported and not gated -- on a lecture whose material
    does not have the eight-section shape the skill assumes, sections 4-7 can be legitimately
    empty, and any threshold there would fail a correct note. The second is gated: the text is
    there, it is real, and its ending is missing, which is exactly the shape that passes lint
    and audit as finished content (#124).
    """

    total: int = 0
    not_covered: int = 0
    truncated: tuple[int, ...] = ()

    @property
    def not_covered_share(self) -> float:
        return self.not_covered / self.total if self.total else 0.0


def gates_of(report_path: Path) -> SectionGates | None:
    """The per-section numbers stage 7 recorded in `synth.json`, or None when unreadable.

    Read back rather than recomputed, for the same reason `coverage_of` is: the note is stage
    7's product, and a second implementation here would be a second thing to keep true. None
    means the record is not there to read, not that every section is fine -- the
    missing-artefact check is the place for files that are not there.
    """
    if not report_path.is_file():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return None
    records = [s for s in sections if isinstance(s, dict)]
    truncated = tuple(
        sorted(
            s["number"]
            for s in records
            if s.get("finish_reason") == "length" and isinstance(s.get("number"), int)
        )
    )
    return SectionGates(
        total=len(records),
        not_covered=sum(1 for s in records if not s.get("present")),
        truncated=truncated,
    )


def prompt_digest() -> str:
    """sha256 of what a stage-7 request actually carries: `SYSTEM` plus the eight briefs.

    Not `SYSTEM` alone (#77). The brief travels in the request's user message and it is the
    half #74 edits, so a digest that skipped it would not change when the prompt did -- which
    is the gap the decision records: two runs with different prompts are recorded identically
    today, and the 3961 -> 7785 word swing between the artefacts cannot be attributed from a
    bundle. Stage 9's equivalent is not here: `audit.prompt_digest` covers `CRITIC_SYSTEM` and
    the request's two envelope headings, and it is recorded as `prompt_sha256` beside `tool` in
    `audit-provenance.json`, which #256 landed.
    """
    return hashlib.sha256(
        "\n".join([SYSTEM, *(brief for _, _, brief in SECTIONS)]).encode("utf-8")
    ).hexdigest()


def provenance(note: Note, config: llm.Config | None) -> str:
    return json.dumps(
        {
            "stage": 7,
            "tool": "dyak-synth-v1",
            # The other half of the sampling record: `endpoint.seed` says which sample was
            # asked for, this says which prompt asked. Without both, a difference between two
            # notes of the same lecture cannot be attributed to anything (#77).
            "prompt_sha256": prompt_digest(),
            "requires_model": False,
            "synthesizer": note.synthesizer,
            "model": note.model,
            "endpoint": config.redacted() if config else None,
            "degraded": note.degraded,
            "coverage": round(note.coverage, 4),
            "coverage_threshold": COVERAGE_THRESHOLD,
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
