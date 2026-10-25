#!/usr/bin/env python3
"""Diff two `audit.json` finding sets — the regression check from #20.

The audit's tier-2 critic is a model, so a re-audit is never byte-identical. This tool
compares what the two runs *found*, not how they phrased it:

  * a tier-1 finding is identified by its rule, line and severity. Those are computed from
    the note, so the same defect on the same note has the same identity;
  * a tier-2 finding is identified by its rule and a digest of its verbatim note quote. The
    quote is the one field the critic is required to copy exactly (ADR-0001 D5), so it is
    stable across rephrasings of the explanation. Two findings that quote the same line are
    counted as a multiset, not collapsed.

A disappeared finding is reported as loudly as a new one. On a model audit, both can mean
"the auditor changed its mind" rather than "the note changed" — that is why `--strict` is
opt-in and why a baseline records the model, prompt digest and parameters beside the run.

Usage:
    tools/audit_baseline.py BASELINE.json CURRENT.json [--strict]

The baseline is whatever audit.json you decided to freeze. Keep it outside the repository
when the note carries lecture text (the audit JSON does: quotes and transcript citations).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _digest(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


def identity(finding: dict) -> tuple:
    """A stable key for one finding, from the fields the stage requires to be verbatim."""
    rule = str(finding.get("rule", ""))
    if finding.get("tier") == "tier2":
        return ("tier2", rule, _digest(str(finding.get("quote", ""))))
    return (
        "tier1",
        rule,
        int(finding.get("line", 0)),
        str(finding.get("severity", "")),
    )


def label(finding: dict) -> str:
    confidence = finding.get("confidence") or "-"
    return f"{finding.get('severity', '?'):>4} {finding.get('rule', '?')} [{confidence}]"


def key_label(key: tuple) -> str:
    """A disappeared finding has no current dict; its identity is all this side has."""
    if key[0] == "tier2":
        return f"{key[1]} quote-sha={key[2]}"
    return f"{key[1]}@{key[2]} {key[3]}"


def load(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise SystemExit(f"{path}: no `findings` list — not an audit.json")
    return [f for f in findings if isinstance(f, dict)]


def meta(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "tier2": payload.get("tier2"),
        "tier1_rules_run": payload.get("tier1_rules_run"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path, help="the frozen audit.json")
    parser.add_argument("current", type=Path, help="the re-audit's audit.json")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on any new or disappeared finding (default: report and exit 0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="show at most N examples per category (default: 10)",
    )
    args = parser.parse_args(argv)

    before = load(args.baseline)
    after = load(args.current)
    before_by = Counter(identity(f) for f in before)
    after_by = Counter(identity(f) for f in after)
    before_first = {identity(f): f for f in before}
    after_first = {identity(f): f for f in after}

    new = after_by - before_by
    gone = before_by - after_by
    unchanged = sum((before_by & after_by).values())

    print(f"baseline: {args.baseline}  ({len(before)} findings)")
    print(f"current:  {args.current}  ({len(after)} findings)")
    print(f"unchanged: {unchanged}  new: {sum(new.values())}  disappeared: {sum(gone.values())}")
    if meta(args.baseline).get("tier2") != meta(args.current).get("tier2"):
        print(
            "tier 2 differs: "
            f"{meta(args.baseline).get('tier2')!r} -> {meta(args.current).get('tier2')!r}"
        )

    if new:
        print("\n-- new --")
        for index, (key, count) in enumerate(new.items()):
            if index >= args.limit:
                print(f"... and {len(new) - args.limit} more")
                break
            finding = after_first[key]
            detail = " ".join(str(finding.get("detail", "")).split())
            print(f"  x{count} {label(finding)} {detail[:160]}")

    if gone:
        print("\n-- disappeared --")
        for index, (key, count) in enumerate(gone.items()):
            if index >= args.limit:
                print(f"... and {len(gone) - args.limit} more")
                break
            finding = before_first[key]
            print(f"  x{count} {label(finding)} {key_label(key)}")
        print(
            "  (the baseline carries the text; this side cannot print a disappeared finding's "
            "quote without it)"
        )

    if args.strict and (new or gone):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
