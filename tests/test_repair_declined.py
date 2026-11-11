#!/usr/bin/env python3
"""Regression: a finding that was applied is not also reported as declined (#283).

`repair.repair` stamps every finding it received into `report.declined`, then drops the entries
it acts on -- the math rules keyed by (`rule`, `line`) (`repair.py:277-281`). The glossary loop
appended its `Change` and never dropped the `term/drift` entry, so one run told the reader both
that the term was declined as "not a mechanical fix" *and* that `changes` recorded that exact
line rewritten. `repair.json` cannot mean both (`repair.py:5`: the report names "every change
and every finding it declined to touch").

Measured on the issue's own input -- `Применяется Пантрягин и его принцип максимума.` with the
row `пантрягин -> Понтрягин (П.С. Понтрягин)` -- driven through the real `audit.run` ->
`repair.run` path, so the evidence is the written `repair.json` and not an in-memory report:

    before: changes=[term/drift line 5]  declined=[term/drift line 5, ...]  <- the contradiction
    after:  changes=[term/drift line 5]  declined=[...]                      <- same input

The same single-record shape hid a second half of the defect: the substitution rewrites every
accepted occurrence in the note while the record was taken from the first hit, so a variant on
three lines left two rewritten lines recorded nowhere. Both are pinned here, and the
declined-only path is pinned too, because a fix that simply emptied `declined` for `term/drift`
would pass the first two checks and lie about #263's declined lines.
"""

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()
sys.path.insert(0, str(REPO / "src"))
from dyak import audit as dyau  # noqa: E402
from dyak import repair as dyrp  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else repr(detail)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + text if text else ''}")
    if not cond:
        failures.append(name)


GLOSSARY = "| Вариант ASR | Канон |\n|---|---|\n{variants}\n"
ISSUE_NOTE = "Заголовок\n\nЕщё строка\n\nПрименяется Пантрягин и его принцип максимума.\n"
ISSUE_GLOSSARY = GLOSSARY.format(variants="| пантрягин | Понтрягин (П.С. Понтрягин) |")
MULTI_NOTE = (
    "Считаем ilqf на первой строке.\nПотом снова ilqf на второй.\nИ ещё раз ilqf на третьей.\n"
)
MULTI_GLOSSARY = GLOSSARY.format(variants="| ilqf | iLQR |")
# The canonical is not on the line, the form is all lowercase Cyrillic and it is not the
# canonical's -- G2 declines it (`repair.py:_guard_lowercase_cyrillic`), the shape #263 added.
DECLINED_NOTE = "Считаем пантрягин и всё.\n"
DECLINED_GLOSSARY = GLOSSARY.format(variants="| пантрягин | Принцип максимума Понтрягина |")


def run_stage(note_text: str, glossary_text: str, lint_payload: dict | None = None) -> dict:
    """Drive the real audit and repair stages over `note_text` and return the written record."""
    with tempfile.TemporaryDirectory(prefix="dyak-repair-declined-") as tmp:
        root = Path(tmp)
        note = root / "note.md"
        note.write_text(note_text, encoding="utf-8")
        glossary = root / "glossary.md"
        glossary.write_text(glossary_text, encoding="utf-8")
        transcript = root / "transcript.txt"
        transcript.write_text("", encoding="utf-8")
        dyau.run(note, transcript, root / "audit", glossary_path=glossary)
        lint_path = None
        if lint_payload is not None:
            lint_path = root / "lint.json"
            lint_path.write_text(json.dumps(lint_payload), encoding="utf-8")
        dyrp.run(
            note,
            root / "audit" / "audit.json",
            root / "repair",
            glossary_path=glossary,
            lint_report_path=lint_path,
        )
        payload = json.loads((root / "repair" / "repair.json").read_text(encoding="utf-8"))
        provenance = json.loads(
            (root / "repair" / "repair-provenance.json").read_text(encoding="utf-8")
        )
        findings = json.loads((root / "audit" / "audit.json").read_text(encoding="utf-8"))
        repaired = (root / "repair" / note.name.replace(".md", ".repaired.md")).read_text(
            encoding="utf-8"
        )
    payload["_audit_findings"] = [(f["rule"], f["line"]) for f in findings["findings"]]
    payload["_changes"] = [(c["rule"], c["line"]) for c in payload["changes"]]
    payload["_declined"] = [(d["rule"], d["line"]) for d in payload["declined"]]
    payload["_reasons"] = {(d["rule"], d["line"]): d["reason"] for d in payload["declined"]}
    payload["_pair_reasons"] = {d["reason"] for d in payload["declined"]}
    payload["_provenance_changes"] = provenance["changes"]
    payload["_repaired"] = repaired
    return payload


def term_lines(pairs: list[tuple[str, int]]) -> set[int]:
    return {line for rule, line in pairs if rule == "term/drift"}


# The finding must really have been there: the audit warned about line 5, and the repair then
# rewrote it. A check that only compared two empty sets would pass on any note at all.
issued = run_stage(ISSUE_NOTE, ISSUE_GLOSSARY)
check(
    "the run that wrote the record really applied a finding (#283)",
    ("term/drift", 5) in issued["_audit_findings"]
    and issued["_changes"] == [("term/drift", 5)]
    and "Применяется Понтрягин (П.С. Понтрягин) и его принцип максимума." in issued["_repaired"],
    f"audit={issued['_audit_findings']} changes={issued['_changes']} repaired={issued['_repaired']!r}",
)
check(
    "a term the repair applied is not also under declined (#283)",
    term_lines(issued["_declined"]) == set(),
    f"declined={issued['_declined']} reasons={sorted(issued['_pair_reasons'])}",
)
check(
    "and the provenance counts the same records (#283)",
    issued["_provenance_changes"] == len(issued["changes"]) == 1,
    f"provenance={issued['_provenance_changes']} changes={issued['_changes']}",
)

# The substitution rewrites every accepted occurrence; the record must name every line it
# rewrote, or the reader is shown one third of the write and told nothing about the rest.
multi = run_stage(MULTI_NOTE, MULTI_GLOSSARY)
check(
    "every rewritten line is recorded, not only the first hit (#283)",
    multi["_changes"] == [("term/drift", 1), ("term/drift", 2), ("term/drift", 3)]
    and term_lines(multi["_declined"]) == set()
    and all(c["before"] != c["after"] for c in multi["changes"])
    and multi["_repaired"]
    == "Считаем iLQR на первой строке.\nПотом снова iLQR на второй.\nИ ещё раз iLQR на третьей.\n",
    f"changes={multi['_changes']} declined={multi['_declined']}",
)

# The other direction: an occurrence repair declines keeps its entry, with its reason. A fix
# that emptied `declined` of term/drift entries would pass the checks above and break this one.
declined = run_stage(DECLINED_NOTE, DECLINED_GLOSSARY)
check(
    "a term the repair declined keeps its entry and its reason (#283)",
    ("term/drift", 1) in declined["_audit_findings"]
    and declined["_changes"] == []
    and term_lines(declined["_declined"]) == {1}
    and declined["_reasons"][("term/drift", 1)] == "not a mechanical fix",
    f"changes={declined['_changes']} declined={declined['_declined']} "
    f"reason={declined['_reasons'].get(('term/drift', 1))!r}",
)

# The math loop's own bookkeeping (`repair.py:277-281`) is the convention this fix follows, so
# it is pinned here too: applied -> change record and no declined entry, unchanged.
math = run_stage(
    "Строка с $x.\n",
    GLOSSARY.format(variants="| содержимое | содержание |"),
    lint_payload={
        "findings": [
            {
                "rule": "math/inline-unbalanced",
                "severity": "WARN",
                "line": 1,
                "detail": "the line opens math and never closes it",
            }
        ]
    },
)
check(
    "the math loop keeps its own bookkeeping (#283)",
    ("math/inline-unbalanced", 1) in math["_changes"]
    and ("math/inline-unbalanced", 1) not in math["_declined"]
    and math["changes"][0]["before"] != math["changes"][0]["after"],
    f"changes={math['_changes']} declined={math['_declined']} repaired={math['_repaired']!r}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all repair-declined checks passed")
