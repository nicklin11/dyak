"""Across-run finding counts — the ledger #93 asked for.

A zero in `lint.json` or `audit.json` means "this run produced no finding". It does not mean
the rule ran and found nothing, and after one run there is no way to tell the two apart. This
module keeps a small sidecar in the bundle, updated after each stage, so a later run can say
"`structure/missing-section` has produced 0 findings over 4 recorded runs" rather than leaving
the zero to be read as an absence.

The ledger is **not** a registered artefact: it is a record of runs, like `timings.json`, not a
product of a stage. That also means `Bundle.clear_root` leaves it alone (it only removes names
in `RUN_ARTEFACTS`), which is what makes the history survive the next run of the same lecture.

The rule universe is declared by the stages (`lint.RULES`, `audit.TIER1_RULES`,
`audit.TIER2_RULES`) and materialised here with zero counters, so a rule that never fired is
present rather than missing. A rule found in an older ledger but no longer declared is kept:
the ledger's job is to remember, not to prune.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import audit, lint

NAME = "findings-ledger.json"
TOOL = "dyak-findings-ledger-v1"

#: `rule -> stage` for every rule the two stages can emit.
RULE_STAGE: dict[str, int] = {
    **{rule: 8 for rule in lint.RULES},
    **{rule: 9 for rule in audit.TIER1_RULES},
    **{rule: 9 for rule in audit.TIER2_RULES},
}
RULES: tuple[str, ...] = tuple(RULE_STAGE)


def _entry(stage: int) -> dict:
    return {
        "stage": stage,
        "findings": 0,
        "runs_with_findings": 0,
        "last_seen": "",
    }


def empty() -> dict:
    return {
        "tool": TOOL,
        "runs": 0,
        "last_run": "",
        "updated_at": "",
        "rules": {rule: _entry(stage) for rule, stage in RULE_STAGE.items()},
    }


def load(path: Path) -> dict:
    """The ledger at `path`, with every declared rule present.

    A missing, unreadable or corrupt ledger starts from empty rather than failing the run:
    the ledger is a convenience, and a bad sidecar must not take the note with it. An
    undeclared rule that a previous version recorded is preserved.
    """
    data = empty()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if not isinstance(payload, dict):
        return data
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        return data
    data["runs"] = int(payload.get("runs", 0)) if isinstance(payload.get("runs"), int) else 0
    data["last_run"] = str(payload.get("last_run", ""))
    data["updated_at"] = str(payload.get("updated_at", ""))
    for rule, entry in rules.items():
        if not isinstance(entry, dict):
            continue
        base = data["rules"].get(rule, _entry(RULE_STAGE.get(rule, 0)))
        base["stage"] = int(entry.get("stage", base["stage"]))
        base["findings"] = int(entry.get("findings", 0))
        base["runs_with_findings"] = int(entry.get("runs_with_findings", 0))
        base["last_seen"] = str(entry.get("last_seen", ""))
        data["rules"][rule] = base
    return data


def record(path: Path, run_id: str, findings) -> dict:
    """Fold one stage's findings into the ledger and write it back.

    `runs` counts runs, not calls: stages 8 and 9 are two calls with one `run_id`, and the
    first call of a run is what increments the counter.
    """
    data = load(path)
    if run_id and data["last_run"] != run_id:
        data["runs"] += 1
        data["last_run"] = run_id
    counts = Counter(getattr(finding, "rule", "") for finding in findings)
    for rule, count in sorted(counts.items()):
        if not rule:
            continue
        entry = data["rules"].setdefault(rule, _entry(RULE_STAGE.get(rule, 0)))
        entry["findings"] += count
        entry["runs_with_findings"] += 1
        entry["last_seen"] = run_id
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return data


def snapshot(path: Path) -> dict:
    """The ledger as it stands, with every declared rule present."""
    return load(path)
