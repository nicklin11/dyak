"""Stage 12: report. Assemble the deliverable set and verify it on disk.

This stage is the reason `process` can exit 0 at all. Exit 0 in ADR-0001's table means
"all artefacts written and verified", and the second half of that is the whole point of
this module: **verification is a read of the filesystem, never an inference from an exit
code.** ADR-0001 D3 records the run that produced zero frames and exited successfully — ffmpeg wrote
an empty file, ffmpeg exited 0, and the pipeline had nothing to check. So stage 12 stats
every artefact it expects and counts what is actually there.

## What it does, in order

1. **Promotes the final note.** Stages 10 and 11 each write their own file
   (`note.repaired.md`, `note.linked.md`). The deliverable is the last one. It is published
   as `note.md` so that both the bundle and any vault export carry the finished note under
   the one name a reader expects — and stage 7's original is preserved as `note.synth.md`,
   because "the note as stage 7 wrote it" is evidence about what the synthesizer did, and
   throwing it away to save a filename would destroy the only record of it.
2. **Verifies the required set.** Each entry states why it is required, so a failure names
   the reason instead of a filename.
3. **Counts frames against the manifest** when the source had video. A manifest listing 18
   timestamps and 17 files on disk is a failure, not a rounding difference.
4. **Records what is absent and why.** Optional artefacts — the tier-2 LLM transcript, the
   link reports — are reported as absent when they are legitimately absent. A verification
   that lists everything missing looks identical whether the thing is optional or broken.

## The vault is not touched here

ADR-0001 D9, as amended 2026-10-03: the vault is an export target, not the artefact root. This
stage writes into the bundle and nowhere else. `Bundle.export_to_vault` performs the copy
when `--vault-path` was given, and verifies each destination against its source.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import audit, frames, lint, link, quality, repair, stages, synth
from .bundle import IMAGES

REPORT_NAME = "report.json"
PROVENANCE_NAME = "report-provenance.json"
ALL_ARTEFACTS = (REPORT_NAME, PROVENANCE_NAME)

#: The stage-7 note, kept under this name once stage 12 publishes the final one as
#: `note.md`. Losing it would leave no record of what the synthesizer actually produced.
SYNTH_NOTE_NAME = "note.synth.md"

#: Artefacts without which there is no deliverable, and why each one is load-bearing.
REQUIRED: dict[str, str] = {
    "note": "the deliverable itself",
    "transcript.json": "the normalised transcript the note was built from",
    "transcript.txt": "the human-readable transcript stage 9 audits numbers against",
    "transcript.raw.json": "the endpoint's unmodified payload; the only way to re-derive "
    "the transcript if the normaliser is wrong",
    lint.REPORT_NAME: "the mechanical gate that ran before the audit",
    audit.REPORT_NAME: "the audit; without it the note is unverified",
    repair.REPORT_NAME: "the fix log; ADR-0001 D5 requires 'wrong' and 'changed' stay separable",
    frames.MANIFEST: "the record of which frames were extracted and when",
}

#: Artefacts that are legitimately absent. Recorded as absent, not as failures.
OPTIONAL: dict[str, str] = {
    quality.REPORT_NAME: "the quality gate runs on frames; an audio-only source has none",
    audit.LLM_TRANSCRIPT_NAME: "tier 2 did not run, so no model was called",
    link.REPORT_NAME: "the link stage writes no report when no terms were configured",
}


@dataclass
class Report:
    verified: list[dict] = field(default_factory=list)
    absent: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    frames_expected: int = 0
    frames_found: int = 0
    promoted: bool = False
    #: Files under the bundle root other than the two this stage writes below. Recorded
    #: because `ok` is otherwise a statement about eight filenames while the directory holds
    #: 143, and nothing in the artefact said so. See `unchecked`.
    bundle_files: int = 0
    #: Files that no check touched, by name. `len(unchecked) + checked` is everything the run
    #: left, and a reader can compare that against `bundle_files` without trusting this stage.
    unchecked: list[str] = field(default_factory=list)
    #: Artefacts removed from the root before this run started, by name. They were an earlier
    #: run's, and left in place they would have satisfied `REQUIRED` on their behalf -- two of
    #: them, `audit.json` and `repair.json`, did exactly that for eight runs. Recorded because
    #: a non-empty list is the evidence that what was verified belonged to this run.
    superseded: list[str] = field(default_factory=list)
    #: Stage 7's own measurement of how much of the transcript reached the model, read back
    #: from `synth.json` rather than recomputed. `None` when the figure is not there to read
    #: (a bundle from before the metric existed): unknown is not the same as low.
    coverage: float | None = None
    coverage_threshold: float = synth.COVERAGE_THRESHOLD
    #: How many of the eight sections stage 7 recorded, and how many it left as
    #: `в лекции не затрагивается`. Reported, never gated: a lecture that does not have the
    #: skill's eight-section shape legitimately leaves some empty, and a threshold here would
    #: fail a correct note. The share is in `report.json` so a reader can judge it (#128).
    sections_total: int = 0
    sections_not_covered: int = 0
    #: Section numbers the model wrote and the endpoint then cut off at its output cap
    #: (`finish_reason == "length"`). Gated, because the text is real and its ending is not:
    #: a truncated derivation passes lint and audit as finished content (#124, #128).
    sections_truncated: tuple[int, ...] = ()

    @property
    def checked(self) -> int:
        """Files a check actually looked at. REQUIRED entries plus the counted frames."""
        return len(self.verified) + self.frames_found

    @property
    def not_covered_share(self) -> float:
        return self.sections_not_covered / self.sections_total if self.sections_total else 0.0

    @property
    def truncated(self) -> bool:
        """A section was cut off at the model's output limit (#128).

        Separate from `low_coverage`: that one is about what reached the model, this one is
        about what came back. Separate from `ok` for the same reason -- `ok` is a statement
        about files on disk, and both of these are statements about what went into them.
        """
        return bool(self.sections_truncated)

    @property
    def ok(self) -> bool:
        """Nothing required is missing, and the frame count agrees with the manifest."""
        return not self.missing and self.frames_found == self.frames_expected

    @property
    def low_coverage(self) -> bool:
        """Stage 7 sent the model less of the lecture than the threshold allows (#117).

        Deliberately *not* part of `ok`: `ok` is a statement about files on disk, and this is a
        statement about what went into them. Merging the two is how a run that omitted a third
        of the course came to exit 0, so they get separate exit codes.
        """
        return self.coverage is not None and self.coverage < self.coverage_threshold

    def summary(self) -> str:
        if not self.ok:
            return f"FAILED: {len(self.missing)} missing, frames {self.frames_found}/{self.frames_expected}"
        return (
            f"{len(self.verified)} verified, {len(self.absent)} optional absent, "
            f"frames {self.frames_found}/{self.frames_expected}"
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "absent": self.absent,
            "missing": self.missing,
            "frames_expected": self.frames_expected,
            "frames_found": self.frames_found,
            "note_promoted": self.promoted,
            "files_checked": self.checked,
            "files_in_bundle_excluding_this_stage": self.bundle_files,
            "unchecked": self.unchecked,
            "superseded_by_this_run": self.superseded,
            "transcript_coverage": None if self.coverage is None else round(self.coverage, 4),
            "transcript_coverage_threshold": self.coverage_threshold,
            "sections_total": self.sections_total,
            "sections_not_covered": self.sections_not_covered,
            "sections_not_covered_share": round(self.not_covered_share, 4),
            "sections_truncated": len(self.sections_truncated),
            "sections_truncated_numbers": list(self.sections_truncated),
        }


def promote_note(bundle_root: Path, final_note: Path, synth_note: Path) -> tuple[Path, bool]:
    """Publish the final note as `note.md`, keeping stage 7's original as `note.synth.md`.

    Returns (path, moved_original). The original is only moved when it is actually
    different, so a run where nothing was repaired or linked does not gain a pointless
    duplicate of itself.
    """
    target = bundle_root / synth.NOTE_NAME
    if not final_note.is_file() or final_note.resolve() == target.resolve():
        return target, False
    # Compare content, not identity. After one promotion note.synth.md necessarily differs
    # from the final note -- keeping it is the whole point -- so a path comparison never
    # matches here, and every later call would move the original a second time.
    if target.is_file() and target.read_bytes() == final_note.read_bytes():
        return target, False
    if synth_note.is_file():
        synth_note.replace(bundle_root / SYNTH_NOTE_NAME)
    elif target.is_file():
        target.replace(bundle_root / SYNTH_NOTE_NAME)
    shutil.copy2(str(final_note), str(target))
    return target, True


def _check(path: Path) -> tuple[bool, str]:
    """Present and non-empty. A zero-byte artefact is a failed write, not a small one."""
    if not path.is_file():
        return False, "missing"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"cannot stat: {exc}"
    if size == 0:
        return False, "zero bytes"
    return True, f"{size} B"


def count_frames(bundle_root: Path) -> tuple[int, int]:
    """(expected from the manifest, found on disk).

    The manifest is the source of truth for *which* files must exist, so each row is stat'd
    by name rather than counted by globbing. The first version of this function globbed
    `*.jpg` in the bundle root and was wrong twice over: frames live in `images/`, and they
    are `.png` -- the `.jpg` files beside them are the quality gate's enhanced derivatives,
    named `<frame>_q.jpg`, one per frame that *passed*. A glob counted 0 on a bundle that
    held 16 correct frames, and a glob that had matched would have counted the derivatives
    too.

    Expected counts data rows, not lines: a header-only manifest is the ADR-0001 D3 failure this
    whole stage exists to catch, and counting lines would call that a success.

    **Expected subtracts the frames stage 5 rejected**, because stage 5 only moves the frames
    that passed into `images/` (`pipeline.run` calls `clear_images` then moves `qreport.passed`).
    Without the subtraction a correctly gated bundle reads `frames 57/58` and the run exits 1
    for having done the right thing -- ADR-0005 D2's "N states, M uncaptioned is visible"
    turned into "M uncaptioned is a failure".

    This could not have been reached before #82: the gate never fired, so `images/` always held
    every manifest row and the subtraction was a no-op. A check that cannot fail is not a
    check, in the same way a gate that never fires is not a gate.
    """
    expected, rejected = expected_frames(bundle_root)
    images = bundle_root / IMAGES
    found = 0
    for name in expected:
        ok, _ = _check(images / name)
        if ok:
            found += 1
    return len(expected), found


def expected_frames(bundle_root: Path) -> tuple[set[str], set[str]]:
    """(manifest rows the gate did not reject, manifest rows it did)."""
    manifest = bundle_root / frames.MANIFEST
    if not manifest.is_file():
        return set(), set()
    rows = [line.split("\t") for line in manifest.read_text(encoding="utf-8").splitlines()[1:]]
    names = {row[2].strip() for row in rows if len(row) >= 3 and row[2].strip()}
    rejected = _rejected_by_gate(bundle_root)
    return names - rejected, rejected


def inventory(root: Path, checked: set[Path], frames: set[str]) -> tuple[int, list[str]]:
    """(files under `root`, the names of those no check touched).

    `checked` is the resolved paths the REQUIRED loop stat'd and passed; `frames` are the
    manifest rows `count_frames` looked for, which live in `images/`. Everything else the run
    wrote -- `captions.json`, `quality.json`, every `*-provenance.json`, the two intermediate
    notes, this stage's own two files -- comes back by name.

    The point is not to fail on them. It is that `report.json` said `"ok": true` after looking
    at 65 of 143 files and nothing in the artefact said so. Measured on run 12: `captions.json`
    and `quality.json` were never stat'd, so a corrupted quality gate still exits 0. #87.
    """
    on_disk = {p for p in root.rglob("*") if p.is_file()}
    frame_paths = {(root / IMAGES / name).resolve() for name in frames}
    unchecked = sorted(
        p.relative_to(root).as_posix()
        for p in on_disk
        if p.resolve() not in checked and p.resolve() not in frame_paths
    )
    return len(on_disk), unchecked


def _rejected_by_gate(bundle_root: Path) -> set[str]:
    """Frame names stage 5 refused. Empty when there is no quality report to ask."""
    path = bundle_root / quality.REPORT_NAME
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return {
        entry.get("name", "")
        for entry in payload.get("frames", [])
        if entry.get("quality_gate") and entry.get("quality_gate") != "PASS"
    }


def run(
    bundle_root: Path,
    *,
    final_note: Path,
    synth_note: Path,
    artefacts: dict[str, Path],
    superseded: list[str] | None = None,
    stream=None,
) -> Report:
    out = stream if stream is not None else sys.stdout
    root = Path(bundle_root)
    report = Report(superseded=list(superseded or []))
    checked_paths: set[Path] = set()

    published, promoted = promote_note(root, final_note, synth_note)
    report.promoted = promoted
    artefacts = dict(artefacts)
    artefacts["note"] = published

    for name, reason in REQUIRED.items():
        path = artefacts.get(name) or (root / name)
        ok, detail = _check(Path(path))
        entry = {"artefact": name, "why": reason, "detail": detail}
        if ok:
            report.verified.append(entry)
            checked_paths.add(Path(path).resolve())
        else:
            report.missing.append(entry)

    for name, reason in OPTIONAL.items():
        path = artefacts.get(name) or (root / name)
        if not Path(path).is_file():
            report.absent.append({"artefact": name, "why_absent": reason})

    report.frames_expected, report.frames_found = count_frames(root)
    # Read back, never recomputed: the coverage figure is stage 7's measurement of what it
    # actually sent, and a second implementation here would be a second thing to keep true.
    report.coverage = synth.coverage_of(root / synth.REPORT_NAME)
    # The per-section half of the same read-back (#128). `None` when stage 7's record is not
    # there to read: unknown is not the same as truncated.
    gates = synth.gates_of(root / synth.REPORT_NAME)
    if gates is not None:
        report.sections_total = gates.total
        report.sections_not_covered = gates.not_covered
        report.sections_truncated = gates.truncated
    expected, _ = expected_frames(root)
    # Measured before this stage writes its own two files, so `bundle_files` is everything
    # under the root *except* `report.json` and `report-provenance.json`. Stated that way
    # because a number that needs a footnote to be right is a number nobody checks.
    report.bundle_files, report.unchecked = inventory(root, checked_paths, expected)

    (root / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / PROVENANCE_NAME).write_text(provenance(report) + "\n", encoding="utf-8")

    out.write(
        f"  [{stages.IMPLEMENTED}/{stages.IMPLEMENTED}] report "
        f"{'OK' if report.ok else 'FAILED'}: {report.summary()}\n"
    )
    for entry in report.missing:
        out.write(f"           MISSING {entry['artefact']}: {entry['detail']} -- {entry['why']}\n")
    if report.unchecked:
        # The exit code does not depend on this line and must not. It exists so that `ok: true`
        # is never read as "everything in the bundle was verified": it is a statement about the
        # required set and the frame count, and those are 65 files out of 143. #87.
        out.write(
            f"           verified {report.checked} of {report.bundle_files + 2} files; "
            f"{len(report.unchecked)} unexamined, listed in {REPORT_NAME} under 'unchecked'\n"
        )
    if report.frames_expected and report.frames_found != report.frames_expected:
        out.write(
            f"           frame count disagrees with {frames.MANIFEST}: "
            f"{report.frames_found} on disk, {report.frames_expected} listed\n"
        )
    if report.low_coverage:
        # Not a missing file, so not a `MISSING` line: the bundle is complete and the note is
        # not. Printed here so the run says it before the exit code has to.
        out.write(
            f"           coverage {report.coverage:.0%} of the transcript reached the model, "
            f"below the {report.coverage_threshold:.0%} threshold: the note is missing part of "
            f"the lecture\n"
        )
    if report.truncated:
        # Also not a `MISSING` line: every file is there and one section ends mid-sentence.
        out.write(
            f"           {len(report.sections_truncated)} of {report.sections_total} sections "
            f"hit the model's output limit and are incomplete: "
            f"{', '.join(str(n) for n in report.sections_truncated)}\n"
        )
    return report


def provenance(report: Report) -> str:
    return json.dumps(
        {
            "stage": 12,
            "tool": "dyak-report-v1",
            "requires_model": False,
            "verified_by": "filesystem stat, not the exit code of any earlier stage",
            "why": (
                "ADR-0001 D3: a run produced zero frames and exited successfully, because ffmpeg "
                "wrote an empty file and exited 0. An exit code is a claim by the stage "
                "that failed; a stat is a measurement."
            ),
            "vault": "not written by this stage; export_to_vault copies and verifies (ADR-0001 D9)",
            "ok": report.ok,
            "frames_expected": report.frames_expected,
            "frames_found": report.frames_found,
            "note_promoted": report.promoted,
            "files_checked": report.checked,
            "files_in_bundle_excluding_this_stage": report.bundle_files,
            "superseded_by_this_run": report.superseded,
            "not_checked": report.unchecked,
            "transcript_coverage": (None if report.coverage is None else round(report.coverage, 4)),
            "transcript_coverage_threshold": report.coverage_threshold,
            "transcript_coverage_source": (
                f"read from {synth.REPORT_NAME}, which stage 7 wrote: the fraction of the "
                "transcript that reached the model. Below the threshold the run does not exit "
                "0 (#117), because every file can be present while the note is missing part "
                "of the lecture."
            ),
            "sections_total": report.sections_total,
            "sections_not_covered": report.sections_not_covered,
            "sections_not_covered_share": round(report.not_covered_share, 4),
            "sections_truncated": list(report.sections_truncated),
            "sections_source": (
                f"read from {synth.REPORT_NAME} too: per-section `present` and `finish_reason`. "
                "A section the model left as 'в лекции не затрагивается' is reported and not "
                "gated, because a lecture without the skill's eight-section shape leaves some "
                "legitimately empty. A section with `finish_reason == 'length'` is gated: the "
                "text is real and its ending is missing, and a run with one does not exit 0 "
                "(#124, #128)."
            ),
            "coverage": (
                f"{report.checked} of {report.bundle_files + 2} files were examined; the "
                "remainder are listed in report.json under 'unchecked'. `ok` is a statement "
                "about the required set and the frame count, not about every file present."
            ),
        },
        indent=2,
        ensure_ascii=False,
    )
