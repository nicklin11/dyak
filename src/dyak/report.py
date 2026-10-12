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
   the reason instead of a filename. Membership is not unconditional: `quality.json` is
   required only when the bundle's own source record says the input had video, because the
   gate runs on frames and an audio-only source has none (#87).
3. **Cross-checks what two artefacts claim about each other.** Presence is not agreement:
   `captions.json` and `quality.json` describe the same frames from opposite ends of the
   pipeline, and every alignment's `quality_gate` must match the gate report's verdict for
   that frame. Two internally consistent files contradicting each other is how stage 5's gate
   stayed a no-op for eight runs (#82), and no `is_file()` can see it (#87).
4. **Counts frames against the manifest** when the source had video. A manifest listing 18
   timestamps and 17 files on disk is a failure, not a rounding difference.
5. **Records what is absent and why.** Optional artefacts — the tier-2 LLM transcript, the
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

from . import audit, caption, frames, lint, link, probe, quality, repair, stages, synth
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
    caption.REPORT_NAME: "stage 6's whole product: which frame was on screen when, and the "
    "words it was captioned against",
    synth.REPORT_NAME: "stage 7's own record; stage 12 reads the coverage and section gates "
    "back from it, so losing it makes the note's completeness unfalsifiable",
}

#: Required only when the bundle's own source record says the source had video (#87).
#:
#: `quality.json` was optional for a real reason -- the gate runs on frames and an audio-only
#: source has none -- but "optional" made a corrupt or missing gate report indistinguishable
#: from a legitimate absence, and left `captions.json` with nothing to be checked against. The
#: promotion is therefore conditional rather than unconditional: a wrong unconditional
#: `REQUIRED` would fail every bundle whose source had no frames.
#:
#: Whether the source had video is read back from `source-provenance.json` (see
#: `source_had_video`), which stages 1 and 2 publish for every run. A bundle whose record is
#: absent or unreadable keeps the optional behaviour: unknown is not False, and a missing
#: record must not fail a run that may legitimately have no frames.
REQUIRED_WHEN_VIDEO: dict[str, str] = {
    quality.REPORT_NAME: "the gate's verdict on every frame; without it stage 5's claim is "
    "unfalsifiable and captions.json has nothing to agree with",
}

#: Artefacts that are legitimately absent. Recorded as absent, not as failures.
#:
#: `quality.json` is listed here so an audio-only (or source-unknown) bundle reports it as
#: legitimately absent. When `source_had_video` is True it is removed from this set and
#: required instead; `_required` is the one place that decides which.
OPTIONAL: dict[str, str] = {
    quality.REPORT_NAME: "the quality gate runs on frames; an audio-only source has none",
    audit.LLM_TRANSCRIPT_NAME: "tier 2 did not run, so no model was called",
    link.REPORT_NAME: "the link stage writes no report when no terms were configured",
}


#: Which registry key holds each artefact this stage verifies is *not* fixed: stage 3 registers
#: `json` / `txt` / `raw`, stage 4 `manifest`, stage 9 `audit/audit.json`, stage 10
#: `repair/repair.json`, while this stage verifies by bundle-root name.
#:
#: Resolution used to be `artefacts.get(name) or (root / name)`. For every entry whose key
#: differs, the lookup missed and the check fell through to "a file with this name is sitting
#: in the bundle root" -- which a *previous* run's file satisfies just as well as this one's.
#: `audit.json` and `repair.json` did exactly that for eight runs. The fallback was
#: load-bearing, not defensive: it was the only thing resolving six of the eight (#88).
#:
#: So the lookup is over the registry twice -- by key, then by the name the registered path
#: has in the bundle -- and never over the filesystem. A name that matches nothing a stage
#: registered is a failure with a reason, which is the whole of the change.
def registered_by_root_name(artefacts: dict[str, Path]) -> dict[str, Path]:
    """Registered artefacts keyed by the name they carry in the bundle root.

    `Bundle.publish` names the destination after the source basename, so a registered path's
    name *is* its bundle-root name. This is what lets `audit/audit.json` answer for
    `audit.json` without anyone guessing from the directory listing.
    """
    return {Path(path).name: Path(path) for path in artefacts.values()}


def source_had_video(bundle_root: Path) -> bool | None:
    """Whether the bundle's own record says the source had video (#87).

    Read from `source-provenance.json`, which stages 1 and 2 publish for every run. It is the
    bundle's own measurement of its input, which is the right evidence for a stage whose job
    is to verify the bundle rather than trust the pipeline that filled it.

    `None` means the record is absent or unreadable -- unknown, not False. The two are kept
    apart because an audio-only source legitimately has no `quality.json`, while a bundle
    whose source record went missing must not be read as one. Unknown therefore keeps
    `quality.json` optional rather than inventing a requirement the bundle cannot justify.
    """
    path = Path(bundle_root) / probe.PROVENANCE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    value = source.get("has_video")
    return value if isinstance(value, bool) else None


def _required(had_video: bool | None) -> dict[str, str]:
    """The REQUIRED set for a bundle whose source did or did not have video."""
    required = dict(REQUIRED)
    if had_video is True:
        required.update(REQUIRED_WHEN_VIDEO)
    return required


def required_for(bundle_root: Path) -> dict[str, str]:
    """The REQUIRED set for this bundle, conditional entries included (#87).

    Exposed rather than inlined into `run` so a test can ask the same question of the same
    bundle the stage verified, without re-implementing the condition and drifting from it.
    """
    return _required(source_had_video(bundle_root))


def _optional(required: dict[str, str]) -> dict[str, str]:
    """OPTIONAL minus anything `required` has taken over.

    A name that is required must not also be reported as legitimately absent: the two records
    would disagree about the same file, and `absent` is the one a reader skips.
    """
    return {name: reason for name, reason in OPTIONAL.items() if name not in required}


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
    #: Where two artefacts contradict each other, or where one of the pair cannot be read.
    #: Empty means `captions.json` and `quality.json` agree about every frame. This is the
    #: cross-reference #87 asked for and the check that would have caught #82 from the bundle
    #: alone: the published `captions.json` carried `quality_gate: ""` on all 58 frames while
    #: `quality.json` recorded one FAIL, and neither file was wrong on its own (#87).
    inconsistent: list[dict] = field(default_factory=list)
    #: What the bundle's own source record says about the input: `True`, `False`, or `None`
    #: when the record is absent or unreadable. Recorded because it decides whether
    #: `quality.json` is required, and a reader must be able to see which way that went.
    source_had_video: bool | None = None
    #: Whether `quality.json` was required for this bundle. Equal to
    #: `source_had_video is True`, stated separately so the verdict is not re-derived from a
    #: tri-state by every reader.
    quality_required: bool = False
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
    #: Which run this verification belongs to (#88). Carried from `pipeline.run`, so the
    #: verdict can be attributed to a run rather than to "a bundle": `Bundle.open` merges into
    #: an existing directory, so two runs of one lecture leave overlapping file sets and,
    #: before this, nothing said which run wrote which file.
    run_id: str = ""
    started_at: str = ""

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
        """Nothing required is missing, nothing contradicts, and the frame count agrees."""
        return (
            not self.missing and not self.inconsistent and self.frames_found == self.frames_expected
        )

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
            return (
                f"FAILED: {len(self.missing)} missing, {len(self.inconsistent)} inconsistent, "
                f"frames {self.frames_found}/{self.frames_expected}"
            )
        return (
            f"{len(self.verified)} verified, {len(self.absent)} optional absent, "
            f"frames {self.frames_found}/{self.frames_expected}"
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "verified": self.verified,
            "absent": self.absent,
            "missing": self.missing,
            "inconsistent": self.inconsistent,
            "source_had_video": self.source_had_video,
            "quality_gate_required": self.quality_required,
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

    **Expected subtracts the frames stage 5 rejected.** Stage 5 moves *every* frame stage 4
    produced into `images/` -- `pipeline.run` calls `clear_images` then moves `produced`; the
    gate's verdict only decides which of them get an enhanced `_q.jpg`. A rejected frame is
    therefore on disk and would count as found, so the subtraction is not what keeps `ok` true
    (both counts agree either way): it keeps the rejection out of `frames_expected` and in
    `unchecked`, so ADR-0005 D2's "N states, M uncaptioned is visible" is a number rather than
    a silently verified frame.

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
    and `quality.json` were never stat'd, so a corrupted quality gate still exits 0. #87 closed
    that: both are verified now (`captions.json` is required, `quality.json` when the source had
    video), and this list is what remains outside the check.
    """
    on_disk = {p for p in root.rglob("*") if p.is_file()}
    frame_paths = {(root / IMAGES / name).resolve() for name in frames}
    unchecked = sorted(
        p.relative_to(root).as_posix()
        for p in on_disk
        if p.resolve() not in checked and p.resolve() not in frame_paths
    )
    return len(on_disk), unchecked


def read_quality_gate(bundle_root: Path) -> tuple[dict[str, str] | None, str]:
    """(frame name -> gate verdict, why the file could not be read).

    `None` with an empty reason means `quality.json` is absent. `None` with a reason means it
    is present and unusable -- truncated, corrupt, or shaped wrongly. The two are kept apart
    because the first is legitimate for an audio-only source and the second is a failed write.

    The second case used to be swallowed: `_rejected_by_gate` caught `JSONDecodeError` and
    returned an empty set, so a corrupt gate report still exited 0 with
    `frames_found == frames_expected` (#87). `run` now records the reason as an
    inconsistency; this function only reports it.
    """
    path = Path(bundle_root) / quality.REPORT_NAME
    if not path.is_file():
        return None, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read {quality.REPORT_NAME}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{quality.REPORT_NAME} is not a JSON object"
    frames = payload.get("frames")
    if not isinstance(frames, list):
        return None, f"{quality.REPORT_NAME} has no 'frames' list"
    verdicts: dict[str, str] = {}
    for entry in frames:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            verdicts[name] = str(entry.get("quality_gate") or "")
    return verdicts, ""


def _rejected_by_gate(bundle_root: Path) -> set[str]:
    """Frame names stage 5 refused. Empty when there is no readable quality report.

    A corrupt report is not silently ignored by the run -- `gate_disagreements` records it --
    but this function still has to answer for the frame count, and "no gate information" is
    the safe answer: it subtracts nothing rather than guessing which frames failed.
    """
    verdicts, _ = read_quality_gate(bundle_root)
    if not verdicts:
        return set()
    return {name for name, gate in verdicts.items() if gate and gate != "PASS"}


def gate_disagreements(
    bundle_root: Path,
    verdicts: dict[str, str] | None,
    quality_error: str,
) -> list[dict]:
    """Where `captions.json` and `quality.json` contradict each other (#87).

    The cross-reference the issue asked for, and the check that would have caught #82 from the
    bundle alone: the published `captions.json` carried `quality_gate: ""` on all 58 frames
    while `quality.json` recorded one FAIL. Each file was internally consistent; only a
    comparison between them shows the contradiction.

    One direction, as specified: every alignment in `captions.json` is compared against the
    gate's verdict for the same frame. A frame the gate report does not mention is itself a
    disagreement -- `captions.json` cannot be cross-checked against a verdict that is not
    there. The reverse (a frame in `quality.json` with no alignment) is not checked here: the
    issue named this direction, and stage 6 records every manifest frame, rejected ones
    included, so a missing alignment is a different defect with a different owner.
    """
    findings: list[dict] = []
    if quality_error:
        findings.append(
            {
                "artefact": quality.REPORT_NAME,
                "why": "the gate's own verdict must be readable before it can be believed",
                "detail": quality_error,
            }
        )
        return findings
    if verdicts is None:
        # No gate report at all. Whether that is legitimate is decided by
        # `REQUIRED_WHEN_VIDEO`; here there is simply nothing to cross-check against.
        return findings

    path = Path(bundle_root) / caption.REPORT_NAME
    alignments, captions_error = caption.read_alignments(path)
    if captions_error:
        # The reader's reasons fall into two families: the file could not be read, and it was
        # read but its shape is wrong. The `why` says which; `detail` is the reader's reason
        # verbatim, so stage 7 and stage 12 describe one file in one vocabulary (#201).
        findings.append(
            {
                "artefact": caption.REPORT_NAME,
                "why": (
                    "the frame-to-gate agreement cannot be checked against an unreadable file"
                    if captions_error.startswith("cannot read ")
                    else "stage 6's product must list its alignments to be verifiable"
                ),
                "detail": captions_error,
            }
        )
        return findings

    for alignment in alignments:
        if not isinstance(alignment, dict):
            continue
        frame = alignment.get("frame")
        if not isinstance(frame, str) or not frame:
            continue
        claimed = str(alignment.get("quality_gate") or "")
        if frame not in verdicts:
            findings.append(
                {
                    "artefact": caption.REPORT_NAME,
                    "frame": frame,
                    "why": "every captioned frame must carry the gate's verdict for it",
                    "detail": f"{quality.REPORT_NAME} records no verdict for {frame}",
                }
            )
            continue
        measured = verdicts[frame]
        if claimed != measured:
            findings.append(
                {
                    "artefact": caption.REPORT_NAME,
                    "frame": frame,
                    "why": "captions.json must not contradict the gate report it was aligned against",
                    "detail": (
                        f"{caption.REPORT_NAME} says quality_gate={claimed!r} for {frame}, "
                        f"{quality.REPORT_NAME} says {measured!r}"
                    ),
                }
            )
    return findings


def run(
    bundle_root: Path,
    *,
    final_note: Path,
    synth_note: Path,
    artefacts: dict[str, Path],
    superseded: list[str] | None = None,
    run_id: str = "",
    started_at: str = "",
    stream=None,
) -> Report:
    out = stream if stream is not None else sys.stdout
    root = Path(bundle_root)
    report = Report(
        superseded=list(superseded or []),
        run_id=run_id,
        started_at=started_at,
    )
    checked_paths: set[Path] = set()

    published, promoted = promote_note(root, final_note, synth_note)
    report.promoted = promoted
    artefacts = dict(artefacts)
    artefacts["note"] = published
    by_root_name = registered_by_root_name(artefacts)

    # Whether `quality.json` is required, read from the bundle's own source record (#87).
    # Unknown (`None`) keeps it optional: a bundle whose record is missing may legitimately
    # have no frames, and failing it would invent a requirement the bundle cannot justify.
    report.source_had_video = source_had_video(root)
    required = _required(report.source_had_video)
    optional = _optional(required)
    report.quality_required = quality.REPORT_NAME in required

    for name, reason in required.items():
        path = artefacts.get(name) or by_root_name.get(name)
        if path is None:
            # No stage registered it. Deliberately *not* a fallback to `root / name`: that is
            # how a previous run's `audit.json` came to satisfy this run's verification, which
            # is the failure #88 exists to name. A name this stage cannot resolve is a failure
            # with a reason, not a stat of whatever is lying around.
            report.missing.append(
                {
                    "artefact": name,
                    "why": reason,
                    "detail": "no stage registered an artefact with this name",
                }
            )
            continue
        ok, detail = _check(Path(path))
        entry = {"artefact": name, "why": reason, "detail": detail}
        if ok:
            report.verified.append(entry)
            checked_paths.add(Path(path).resolve())
        else:
            report.missing.append(entry)

    for name, reason in optional.items():
        # Absence is the expected outcome here, so an unregistered name is simply absent --
        # no reason to fail a run over an artefact that is allowed not to exist.
        path = artefacts.get(name) or by_root_name.get(name)
        if path is None or not Path(path).is_file():
            report.absent.append({"artefact": name, "why_absent": reason})

    # The cross-artefact half of the verification (#87). A presence check cannot see two
    # internally consistent files contradicting each other, which is exactly how #82 shipped:
    # `captions.json` said every frame passed, `quality.json` said one failed, and stage 12
    # never compared them. Read the gate report once and hand it to the comparison.
    verdicts, quality_error = read_quality_gate(root)
    report.inconsistent = gate_disagreements(root, verdicts, quality_error)
    # #211: the manifest stops being the only witness of *how many* frames the run produced.
    # `count_frames` below stats the manifest's own rows, so it is its own denominator: a
    # manifest cut from three rows to one reports `1/1`, the note covers one of three frames,
    # and the run exits 0 with `uncovered 0.00` because the coverage denominator is that same
    # short file. Stage 5 judged the frames stage 4 returned in memory (`pipeline.py:511` ->
    # `:541`) and `quality.json` records one verdict per judged frame, so it is the witness for
    # the count while the manifest stays the witness for *which* files must exist --
    # `count_frames`' contract is untouched, and stage 6's own reader is the right one for the
    # left side because `caption.align` takes its frames from the manifest too (`caption.py:373`).
    #
    # A trim is not this case, and that is answered rather than deferred: re-running stage 4 on
    # a trimmed manifest reuses those times, deletes the old frames and rewrites the manifest
    # (`frames.py:295-318`) before stage 5 judges the same set, so a self-consistent trim agrees
    # here and stays exit 0.
    #
    # The honest limit: a manifest already short *before* stage 5 runs cannot be told apart from
    # a trim, and it is also the case where every artefact agrees -- the bundle really does hold
    # only those frames, and nothing in it can say it was ever longer.
    #
    # Absence is not this check's case: the manifest is in REQUIRED, so a missing one is a
    # `MISSING` entry and exit 6 already, and listing it here too would put one artefact in both
    # lists. `verdicts is None` is the audio-only (or unreadable-gate) case, where there is no
    # count to compare against -- the same skip `gate_disagreements` makes.
    manifest_path = root / frames.MANIFEST
    if verdicts is not None and manifest_path.is_file():
        listed = len(caption.read_manifest(manifest_path))
        if listed != len(verdicts):
            report.inconsistent.append(
                {
                    "artefact": frames.MANIFEST,
                    "detail": f"{listed} of {len(verdicts)} frames listed",
                    "why": (
                        "stage 4 wrote it with the frames stage 5 then judged; a shorter one "
                        "was changed afterwards"
                    ),
                }
            )

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
    for entry in report.inconsistent:
        # Its own line and its own reason, because it is not a missing file: both files are
        # there and one of them is wrong about a frame. #87.
        where = entry["artefact"] + (f":{entry['frame']}" if entry.get("frame") else "")
        out.write(f"           INCONSISTENT {where}: {entry['detail']} -- {entry['why']}\n")
    if report.unchecked:
        # The exit code does not depend on this line and must not. It exists so that `ok: true`
        # is never read as "everything in the bundle was verified": it is a statement about the
        # required set and the frame count, not about every file present. #87.
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
            "inconsistent": report.inconsistent,
            "source_had_video": report.source_had_video,
            "quality_gate_required": report.quality_required,
            "quality_gate_conditional_source": (
                f"read from {probe.PROVENANCE_NAME}, which stages 1 and 2 publish: when the "
                f"source had video, {quality.REPORT_NAME} is required, not optional, and every "
                f"alignment in {caption.REPORT_NAME} must agree with its verdict for that frame "
                "(#87). Unknown keeps it optional, because a bundle whose source record is "
                "missing may legitimately have no frames."
            ),
            "inconsistency_check": (
                f"every alignment in {caption.REPORT_NAME} is compared with "
                f"{quality.REPORT_NAME}'s verdict for the same frame. A disagreement, or an "
                f"unreadable {quality.REPORT_NAME}, is named in 'inconsistent' and fails the "
                "run. This is the check that would have caught #82 from the bundle alone: the "
                "two files were each internally consistent and contradicted each other. "
                f"{frames.MANIFEST} is compared with the same verdicts for the frame *count*: "
                "stage 4 wrote it with the frames stage 5 judged, so a manifest listing fewer "
                "of them was changed afterwards, and it is named here too (#211)."
            ),
            "run_id": report.run_id,
            "started_at": report.started_at,
            "run_id_source": (
                "from pipeline.run (#88). The bundle root is merged into, not replaced, so "
                "without this two runs of one lecture are indistinguishable in the directory."
            ),
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
                "about the required set, the frame count, and the "
                f"{caption.REPORT_NAME}/{quality.REPORT_NAME} agreement -- not about every "
                "file present."
            ),
        },
        indent=2,
        ensure_ascii=False,
    )
