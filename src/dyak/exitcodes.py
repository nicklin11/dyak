"""Exit codes are a contract, not documentation (ADR-0001).

Every stage imports its code from here rather than hardcoding an integer, so
that the table in the ADR and the behaviour of the binary cannot drift apart.
If a stage invents its own exit path, the table becomes fiction.
"""

from __future__ import annotations

# 0 — success; all artefacts written and verified on disk.
OK = 0

# 1 — usage error. Also used for a subcommand that is not implemented yet:
# invoking something this build cannot do is a usage error.
USAGE = 1

# 2 — missing dependency. The check names it and prints a remediation line.
# `process` refuses to start rather than silently degrading.
MISSING_DEPENDENCY = 2

# 3 — dependency present but failed. Its stderr is reproduced VERBATIM (ADR-0001 D2):
# a tool that swallows its dependency's errors is worse than no tool, because
# the failure looks like success.
DEPENDENCY_FAILED = 3

# 4 — quality gate failed. The note is still written; the report says why (ADR-0001 D4).
QUALITY_GATE_FAILED = 4

# 5 — the audit found errors above threshold. The note is written and FLAGGED.
AUDIT_FINDINGS = 5

# 6 — the pipeline ran, but required artefacts could not be verified on disk (ADR-0001 D3).
# Also produced by a run that stops at the stage which needed the artefact, not only by stage
# 12's end-of-run check: stage 6 exits 6 when the gate report it must read is unreadable
# (caption.py:388-391, #188), so the code means "the artefacts are not trustworthy", whether
# the run reached stage 12 or stopped at the stage that needed the artefact.
ARTEFACT_VERIFICATION_FAILED = 6

# 7 — a model ran, but its note is degraded/incomplete and needs user attention.
INCOMPLETE_NOTE = 7

# 8 — the note was written from less of the lecture than the coverage threshold allows: stage 7
# handed the model a shortened transcript, so the note cannot be complete however clean it looks
# (#117). Separate from 6, which is about files that are not on disk at all: here every file is
# present and verified, and the deliverable is still missing a third of the course.
LOW_COVERAGE = 8

# 9 — a section the model wrote was cut off at the endpoint's output limit (`finish_reason ==
# "length"`), so its ending is missing while its text reads as finished (#124, #128). Separate
# from 8, which is about what reached the model: here the whole transcript got there and the
# answer came back short. Separate from 7, which is the general degraded path and would report
# the symptom rather than the cause.
TRUNCATED_SECTIONS = 9

# 130 — interrupted (Ctrl-C), the shell convention of 128 + SIGINT(2). Not a
# pipeline failure: the work dir is retained and its path printed, because an
# aborted 73-minute transcribe is exactly the run worth inspecting. Declared
# here so the table below stays the single source of truth.
INTERRUPTED = 130

# An *uncaught* internal error (a bug in dyak itself) deliberately has no code
# here: it propagates as a traceback, which is louder than any summary line, and
# Python exits it with 1. Recorded so the absence is a decision rather than a gap.

DESCRIPTIONS: dict[int, str] = {
    OK: "success",
    USAGE: "usage error",
    MISSING_DEPENDENCY: "missing dependency (named, with remediation)",
    DEPENDENCY_FAILED: "dependency failed (stderr reproduced verbatim)",
    QUALITY_GATE_FAILED: "quality gate failed; note written, report says why",
    AUDIT_FINDINGS: "audit found errors above threshold; note written and flagged",
    ARTEFACT_VERIFICATION_FAILED: (
        "required artefacts could not be verified on disk, whether the run reached stage 12 "
        "or stopped at the stage that needed the artefact"
    ),
    INCOMPLETE_NOTE: "model-written note is incomplete or degraded",
    LOW_COVERAGE: "less of the lecture reached the model than the coverage threshold allows",
    TRUNCATED_SECTIONS: "a section was cut off at the model's output limit; its ending is missing",
    INTERRUPTED: "interrupted by the user; work dir kept",
}
