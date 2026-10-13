#!/usr/bin/env python3
"""Regression: the term/drift rewrite declines what it cannot defend (#287).

#277 narrowed *where* a variant may be found (a whole word, not a substring). It did not
change *whether* the occurrence found may be replaced, and on the real note the whole-word
occurrences are as often an ordinary Russian word or an inflection of the term as they are a
mangled term. Measured on `~/.local/state/dyak/занятие--2/note.md` (487 lines) with the
122-entry glossary derived from `mscs/_terms`, unguarded: 19 lines differ from the original,
17 of them corruptions (7 duplicated words, 4 inflected phrases turned nominative, 2 homonyms,
4 inflected forms) to normalise 2, and 12 lines change whose canonical `check_term_drift`
never warned about (`audit.py:189`).

The four guards in `repair.DRIFT_GUARDS` are the decidable consequences of that ambiguity,
and this file pins each one against the sentence it came from. The note is user data and
cannot be a fixture, so the sentences below are the measured fragments -- the ones quoted in
#287 and in the decision -- driven through the real `repair.repair`, no model and no settings.

Every glossary row below is written in the derived glossary's format -- the lowercased
`aliases:`/`forms:` bullet on the left, the note's heading on the right -- but only some of them
are rows of that derived set. The derived ones are `прямые методы`, `прямые`, `прямых методов`,
`функционала`, `функционал`, `градиент`, `градиента`, `ilqf` and `пантрягина` -> `Принцип
максимума Понтрягина`; the rest are marked hand-built where they are defined. The synthetic
`Пантрягин` -> `Понтрягин (П.С. Понтрягин)` pair is hand-built: the derived set's Понтрягин row
is the `пантрягина` one above. The `С++17`/`c++17` family is hand-built too, exactly as the
issue builds it.

`tests/test_stages.py` still owns the #277 boundary; this file owns the corpus shape, which is
why it is separate (`tests/test_claims.py` requires it to be wired into ci.yml).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()
sys.path.insert(0, str(REPO / "src"))
from dyak import audit as dyau  # noqa: E402
from dyak import lint as dyln  # noqa: E402
from dyak import repair as dyrep  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else repr(detail)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + text if text else ''}")
    if not cond:
        failures.append(name)


def repaired(text: str, entries: tuple[tuple[str, str], ...]) -> tuple[str, list]:
    """Repair `text` with exactly these rows and return it with its term/drift changes."""
    glossary = dyau.Glossary(entries)
    fixed, report = dyrep.repair(text, dyau.Report(), glossary, None)
    return fixed, [c for c in report.changes if c.rule == "term/drift"]


def warned(text: str, entries: tuple[tuple[str, str], ...]) -> list[int]:
    """The lines `check_term_drift` warns about -- the recorder's side of the same text."""
    report = dyau.Report()
    dyau.check_term_drift(text, dyau.Glossary(entries), dyln._outside_code(text), report)
    return sorted({f.line for f in report.findings if f.rule == "term/drift"})


# The real note's sentences, as #287 and the decision quote them: 299, 107 and 454 are the
# three the issue measured. Those three rows are in the derived set (a lowercased `mscs/_terms`
# bullet each, the note's heading as canonical, no hand-editing).
NOTE_299 = "Исходная непрерывная постановка, от которой стартуют все прямые методы:"
NOTE_107 = (
    "Смысловые части функционала (по объяснению лектора): какая-то из частей может отсутствовать."
)
NOTE_454 = "Для машины Дубинса траектория — только прямые и дуги окружностей."
ROWS_299 = (("прямые методы", "Прямые методы"),)
ROWS_107 = (("функционала", "Функционал"),)
ROWS_454 = (("прямые", "Прямые методы"),)

note = "\n".join(["содержимое"] * 2 + [NOTE_299, NOTE_107, NOTE_454] + ["содержимое"] * 2) + "\n"
rows = (
    ("прямые методы", "Прямые методы"),
    ("функционала", "Функционал"),
    ("прямые", "Прямые методы"),
)
fixed, changes = repaired(note, rows)
check(
    "the three measured corruptions come back byte-identical (#287)",
    fixed == note and not changes,
    f"changed={fixed != note} changes={[(c.line, c.before[:40], c.after[:40]) for c in changes]}",
)

# G1 -- the detector's own precondition (`audit.py:189`), applied to the writer. Line 299 is
# the real case: the canonical is already there, in the occurrence being looked at, so the
# rewrite would write a second `методы` and the audit says nothing about the line.
check(
    "G1 declines an occurrence whose canonical is already on the line (#287)",
    repaired(NOTE_299, ROWS_299) == (NOTE_299 + "", []),
    repaired(NOTE_299, ROWS_299)[0],
)
g1_line = "Здесь Прямые методы, и ещё раз прямые методы."
check(
    "G1 declines the occurrence even when the canonical sits elsewhere on the line (#287)",
    repaired(g1_line + "\n", ROWS_299)[0] == g1_line + "\n",
    repaired(g1_line + "\n", ROWS_299)[0],
)

# G2 -- the all-lowercase-Cyrillic boundary: an ordinary word and an inflection of the term
# are the same string to a rule with no morphology. The inflected-form family (107, 194, 339,
# 401) and the inflected-phrase family (66, 74, 202, 309) are both this guard.
check(
    "an inflected form of a term is declined, not turned nominative (#287)",
    repaired(NOTE_107, ROWS_107) == (NOTE_107, []),
    repaired(NOTE_107, ROWS_107)[0],
)
check(
    "G2 declines the homonym too -- the Dubins straight segments are not the method (#287)",
    repaired(NOTE_454, ROWS_454) == (NOTE_454, []),
    repaired(NOTE_454, ROWS_454)[0],
)

# The inflected-phrase family (the real lines 66, 74, 202 and 309, «методом прямых методов»)
# is the case where G2 is the only guard that can decide: the canonical is not on the line, no
# neighbour repeats it, and it does not wrap the match -- so reverting G2 alone has to fail
# here, and this check names it.
phrase = "Решение задачи методом прямых методов не рассматривается.\n"
check(
    "G2 declines the inflected phrase when it is the only guard that can decide (#287)",
    repaired(phrase, (("прямых методов", "Прямые методы"),)) == (phrase, []),
    repaired(phrase, (("прямых методов", "Прямые методы"),))[0],
)

# G3 -- duplication, the report's headline shape, needs no morphology and so binds where G2
# cannot reach: a capitalised or Latin variant is still a word the canonical repeats. The row is
# hand-built -- `С++17` is the issue's own example and is not in the derived set.
check(
    "G3 declines a replacement that would repeat the neighbouring word (#287)",
    repaired("Стандарт С++17 и стандарт С++20\n", (("С++17", "стандарт C++17"),))[0]
    == "Стандарт С++17 и стандарт С++20\n",
    repaired("Стандарт С++17 и стандарт С++20\n", (("С++17", "стандарт C++17"),))[0],
)

# G4 -- the canonical merely wraps what is written, so substituting adds a word rather than
# repairing one. Note the limit this guard leaves: the Cyrillic `С` spelling is *not* folded.
# The rows here and in the check below are the same hand-built `С++17`/`c++17` family.
check(
    "G4 declines a canonical that merely wraps the match (#287)",
    repaired("компилятор c++17 собирает проект\n", (("c++17", "стандарт c++17"),))[0]
    == "компилятор c++17 собирает проект\n",
    repaired("компилятор c++17 собирает проект\n", (("c++17", "стандарт c++17"),))[0],
)
check(
    "and the mixed-script spelling still passes G4, as the documented limit says (#287)",
    repaired("компилятор С++17 собирает проект\n", (("С++17", "стандарт C++17"),))[0]
    == "компилятор стандарт C++17 собирает проект\n",
    "repair._fold_shape (repair.py:141): 'С++17' with a Cyrillic С -> 'с17', "
    "not a substring of 'стандартc17'",
)

# The record must name the first ACCEPTED occurrence: before the guards, 3 of the 8 records on
# the real note carried `before == after` (line 8 once, line 87 twice), a change nobody can
# audit. Here the first occurrence is declined and the second accepted, so a search-based
# record would name the declined one and come back empty.
pair = "Считаем пантрягин, потом Пантрягин и ilqf.\n"
# `ilqf -> iLQR` is a derived row; `пантрягин -> Понтрягин` is hand-built (the derived set has the
# genitive `пантрягина`, not this nominative variant), which is what makes the first occurrence
# declined and the second accepted.
pair_rows = (("пантрягин", "Понтрягин"), ("ilqf", "iLQR"))
fixed, changes = repaired(pair, pair_rows)
check(
    "the record names the first accepted occurrence, never a declined one (#287)",
    changes[0].before == "Считаем пантрягин, потом Пантрягин и ilqf."
    and changes[0].after == "Считаем пантрягин, потом Понтрягин и ilqf."
    and all(c.before != c.after for c in changes),
    f"fixed={fixed.strip()!r} changes={[(c.line, c.before, c.after) for c in changes]}",
)
# The canonical is the derived row's; the variant is the nominative spelling, which the derived
# set does not carry, so the row as written is hand-built.
check(
    "a declined occurrence produces no change record at all (#287)",
    repaired("Считаем пантрягин и всё.\n", (("пантрягин", "Принцип максимума Понтрягина"),))[1]
    == [],
    "a lowercase Cyrillic form is declined, so nothing was substituted and nothing recorded",
)

# The class the rule exists for must still fire: a mangled term whose form is not a correct
# Russian word (capitalised / Latin) and whose canonical is absent from the line. `ilqf -> iLQR`
# is a derived row; the synthetic `Пантрягин -> Понтрягин (П.С. Понтрягин)` pair is hand-built.
check(
    "the drift the rule exists for still fires: Пантрягин and ilqf (#287)",
    repaired(
        "Применяется Пантрягин и ilqf.\n",
        (("Пантрягин", "Понтрягин (П.С. Понтрягин)"), ("ilqf", "iLQR")),
    )[0]
    == "Применяется Понтрягин (П.С. Понтрягин) и iLQR.\n",
    repaired(
        "Применяется Пантрягин и ilqf.\n",
        (("Пантрягин", "Понтрягин (П.С. Понтрягин)"), ("ilqf", "iLQR")),
    )[0],
)

# The five lines whose only change was a match inside a longer word (#277's class: the real
# lines 14, 99, 303, 323 and 463, all inflected forms of `функционал`) must stay byte-identical,
# so a future guard cannot quietly start corrupting them again.
inflected = (
    "\n".join(
        [
            "Решение функционалом задаётся так.",
            "минимум функционалу не нужен.",
            "о функционале речь ниже.",
            "и его градиент, и градиента норма.",
        ]
    )
    + "\n"
)
check(
    "the five #277-family lines stay byte-identical (#287)",
    repaired(inflected, (("функционал", "Функционал"), ("градиент", "Градиент")))[0] == inflected,
    repaired(inflected, (("функционал", "Функционал"), ("градиент", "Градиент")))[0],
)

# The two lines whose substitution was legitimate (473 and 475, both nominative `функционал`)
# are declined too -- priced in the decision, and still correct Russian. The text must be
# unchanged, and the audit must still warn where the canonical is genuinely absent.
legit = "Здесь функционал и штраф.\n"
check(
    "the two legitimate renames (473, 475) are declined too, and stay correct Russian (#287)",
    repaired(legit, (("функционал", "Функционал"),))[0] == legit,
    repaired(legit, (("функционал", "Функционал"),))[0],
)
check(
    "the audit keeps warning about the drift it warns about today (#287)",
    warned(NOTE_454 + "\n", ROWS_454) == [1],
    f"warned={warned(NOTE_454 + chr(10), ROWS_454)} of the line the rewrite declines",
)

# The case-only difference is now declined as well: `check_term_drift` is silent about it
# (`audit.py:189` -- the canonical is on the line, case-insensitively), so the writer applying
# the same precondition cannot be bolder than the report. Pinned, not left to be rediscovered.
# The row is hand-built: the derived set has no `норма` entry (this is `test_stages.py`'s pair).
check(
    "a case-only difference is declined by G1, exactly as the audit's silence says (#287)",
    repaired("Здесь норма в начале строки.\n", (("норма", "НОРМА"),))
    == ("Здесь норма в начале строки.\n", []),
    "the canonical is present case-insensitively, so the audit warns about nothing here",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all repair-drift checks passed")
