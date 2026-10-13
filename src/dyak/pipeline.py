"""Stage orchestration for `dyak process`.

Stages 1-12 live here. Stage 0 (`doctor`) runs in the CLI before this is called,
because its output is a rendered report rather than a pipeline artefact.

**Progress is reported as stage lines.** ADR-0001 D7's reason for stage-level
progress is that the STT backend issues one blocking HTTP request with no streaming,
and a bar that reads 90% during a 23-minute transcribe and then sits still is a lie
about where the time went. What D7 bans is a *fabricated* bar, and a count of units
this run itself observed is not one: the hosted route's chunk counter is printed as
it advances (#193), while whisper.cpp -- which reports nothing this client can read
-- says so rather than borrowing a rate. The count in every line is
`stages.IMPLEMENTED`, which is 12: every stage ADR-0001 § *The pipeline* lists is
built.

**Stages 2-5 run as two branches, joined by stage 6** (#115). `audio -> stt` and
`frames -> quality` read nothing from each other -- one decodes the audio track and
transcribes it, the other decodes the video track, gate-checking its own frames -- so
they run on one thread each instead of one after the other, and the slower branch is
what the run costs rather than the sum. `DYAK_SERIAL=1` puts them back on one thread
in the old order: a benchmark baseline, and the way out on a host where the two
contend for one resource. Every line a branch writes carries its tag -- `(A)` for
`audio`/`stt`, `(B)` for `frames`/`quality` -- so an interleaved stream still says
which half is talking.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Generic, TypeVar

from . import (
    audio,
    audit,
    caption,
    deps,
    exitcodes as ec,
    frames,
    lint,
    link,
    probe,
    quality,
    repair,
    report,
    runner,
    stages,
    stt,
    synth,
)
from . import llm
from .bundle import Bundle
from .settings import BBOX_SOURCE_ENV, SERIAL_ENV, resolve
from .workspace import WorkDir

# Re-exported, not defined here. `quality` and `caption` write progress lines that need the
# same count, and they cannot import this module to ask -- `pipeline` imports them, so the
# dependency would be a cycle. `stages` holds it instead.
IMPLEMENTED = stages.IMPLEMENTED
FIRST_STAGE = stages.FIRST_STAGE
REMAINING_NOTE = stages.REMAINING_NOTE

# Multiplier on the upper bound of the stage-3 estimate. An interval that can
# collapse to a single number reads as a precision claim nobody has measured.
HEADROOM = 1.5


@dataclass(frozen=True)
class StageReport:
    index: int
    name: str
    seconds: float
    summary: str

    def line(self) -> str:
        return f"  [{self.index}/{IMPLEMENTED}] {self.name.ljust(7)} {self.seconds:7.1f}s  {self.summary}"


@dataclass(frozen=True)
class RunResult:
    source: Path
    info: probe.MediaInfo
    audio: audio.AudioArtefact
    transcript: stt.Transcript
    frames: list[Path]
    artefacts: dict[str, Path]
    reports: tuple[StageReport, ...]
    bundle: Bundle
    #: ADR-0001 D4's verdict. `quality.ok` false means exit 4, reported by the caller.
    quality: quality.Report
    enhanced: list[Path]
    #: Code 4's coverage half, the frame/A-V family (`docs/adr/0001-lecture-pipeline.md:328`) --
    #: *not* ADR-0001 D5 (`:191`), which is the audit. `caption.ok` false means the audio and the
    #: video are not describing the same lecture: exit 4 rather than a note with holes in it. It
    #: is false whenever `caption.considered == []` too, so the gate that reads it is guarded on
    #: `considered` (`src/dyak/cli.py:442`): no frames to align is not a desynced source.
    caption: caption.Report
    #: What the captioning half actually did. Separate from the coverage verdict because they
    #: fail for different reasons: `caption.ok` is about A/V sync, `caption_outcome.ok` is
    #: about the vision endpoint. Only the second one is new since stage 6 gained a model.
    caption_outcome: caption.CaptionOutcome
    #: The note. `synth.degraded` is reported, not hidden: a note written by the template
    #: synthesizer is a skeleton, and a reader has to be able to tell.
    note: synth.Note
    #: Stage 8's mechanical verdict (`docs/adr/0001-lecture-pipeline.md:310`; D6, `:210`); its
    #: findings are stage 10's repair input, not a gate on the note.
    lint: lint.Report
    #: ADR-0001 D5's audit. `audit.ok` false is exit 5: the note is written and annotated, never
    #: discarded. An audit that could not run says so; it never reads as one that passed.
    audit: audit.Report
    #: What stage 10 could fix, and what it declined to.
    repair: repair.Report
    #: Stage 11's link counts. `link.terms == 0` means no terms directory resolved: the note
    #: is written unlinked, and saying "0 links" instead of "no vocabulary" would hide a
    #: missing configuration as a clean result.
    link: link.Report
    #: The note every downstream stage should publish: stage 7's note, repaired by 10 and
    #: linked by 11. Stage 12 writes this file, not `synth.NOTE_NAME` -- publishing the
    #: stage-7 original would undo the two stages that ran after it.
    final_note: Path
    #: ADR-0001 D3's verdict on the filesystem. `report.ok` false is the one failure that must not
    #: reach exit 0: every earlier stage claimed success, and only this one measured it.
    report: report.Report

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.reports)


@dataclass(frozen=True)
class _SttHalf:
    """What branch A produced: the audio, the transcript, and stage 3's published paths.

    A return value rather than a closure over `_stages`'s locals, because the two branch
    bodies run on their own threads and a name they both assign to has no owner: the join is
    where their products meet (#115).
    """

    audio: audio.AudioArtefact
    paths: dict[str, Path]
    transcript: stt.Transcript


@dataclass(frozen=True)
class _FramesHalf:
    """What branch B produced: the frames stage 4 detected and the gate stage 5 applied.

    `published` holds what branch B registered in the bundle, keyed the way `RunResult.artefacts`
    keys them. It is built on the branch rather than handed in because `Bundle.publish` moves
    the file it is given, so the path the branch must record is the one publish returns.
    """

    manifest: Path
    produced: list[Path]
    quality: quality.Report
    enhanced: list[Path]
    published: dict[str, Path]


#: Per-stage wall clock, written after stage 12. Every `StageReport` already carries its own
#: seconds and `RunResult.seconds` already sums them -- all of it in memory, all of it printed
#: to a terminal that closes. Nothing reached the bundle, so the one number the bundle could
#: not answer was "what did this run cost": stage 3 transcribes 4520 s of audio in ~297 s and
#: no artefact said so. Stages 6, 7 and 9 record per-call seconds (`caption_trace`, the two
#: `*-llm-transcript.json`), which made the omission look like a stage-3 gap. It was not --
#: stages 1, 2, 4, 5, 8, 10, 11 and 12 recorded nothing either, and stage 12 is the one stage
#: that does not print its seconds on the terminal: `report.run` writes its own `[12/12]` line
#: without them (`report.py:646-649`), so `_stages` skips `StageReport.line()` for it. The
#: qualifier is load-bearing: stage 12's seconds *do* reach `timings.json` (the note at the
#: stage-12 append says so), and this sidecar is what writes them there.
#:
#: A sidecar, not an artefact: written straight to `bundle.root` and deliberately **not**
#: registered through `Bundle.record`, so `bundle.summary()` and stage 12's REQUIRED/OPTIONAL
#: verification do not move. It is a record of the run, not a product of a stage.
#:
#: #90 recorded what that costs, because the decision was written down without it: an
#: unregistered artefact is not exported, so `timings.json` stays in the bundle and does not
#: travel to the vault, and `bundle.summary()` counts one file fewer than the directory
#: holds. That is the whole of the remaining gap between the bundle and the vault.
TIMINGS_NAME = "timings.json"


def write_timings(bundle: Bundle, reports: list[StageReport]) -> Path:
    """Per-stage seconds into the bundle. Best effort: never fail a run over a log."""
    target = bundle.root / TIMINGS_NAME
    try:
        target.write_text(
            json.dumps(
                {
                    "tool": "dyak-timings-v1",
                    "total_seconds": round(sum(r.seconds for r in reports), 1),
                    "stages": [
                        {
                            "stage": r.index,
                            "name": r.name,
                            "seconds": round(r.seconds, 1),
                            "summary": r.summary,
                        }
                        for r in reports
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        # A run that produced a verified note does not become a failure because a diagnostic
        # file could not be written. The exit code is about the note.
        pass
    return target


#: The run's own state, written in a `finally` so it exists on runs that did *not* finish.
#:
#: Without it an interrupted run leaves a bundle holding `note.md`, `captions.json`,
#: `quality.json` and `lint.json` -- everything stage 7 had published -- and no `report.json`
#: and no `timings.json`, because both were written at the end. To a reader opening the
#: directory that is a successful run whose verification artefacts happen to be missing. The
#: pipeline could not tell its own incomplete bundle from a complete one, and neither could
#: anyone else (#92).
#:
#: `timings.json` is written from the same place, for the same reason: it existed only on runs
#: that completed, which are exactly the runs that were not slow and not broken (#92).
#:
#: A sidecar like `timings.json`, and unregistered for the same reason: it records the run
#: rather than being a product of a stage, and it is written after stage 12 has already
#: verified the artefacts. It is in `bundle.RUN_ARTEFACTS`, so a later run clears it.
RUN_STATE_NAME = "run-state.json"


def run_id_of(source: Path, started_at: str) -> str:
    """A name for one run. Unique per invocation, and readable enough to quote in a bug report."""
    return f"{started_at}-{uuid.uuid4().hex[:8]}-{Path(source).stem[:24]}"


def write_run_state(
    bundle: Bundle,
    *,
    source: Path,
    run_id: str,
    started_at: str,
    reports: list[StageReport],
    outcome: str,
    detail: str,
) -> Path:
    """The run's identity and outcome, into the bundle. Best effort, like `write_timings`.

    `stage_reached` is the stage that was *running* when the run ended, not the last one that
    finished: `reports` holds completed stages, so on an interrupt during stage 9 the last
    entry is stage 8 and the run reached 9. Recording only `stages_completed` would read as
    "the run stopped after 8", which is true but not the useful half.
    """
    target = bundle.root / RUN_STATE_NAME
    completed = [r.index for r in reports]
    try:
        target.write_text(
            json.dumps(
                {
                    "tool": "dyak-run-state-v1",
                    "run_id": run_id,
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "outcome": outcome,
                    "detail": detail,
                    "stages_completed": completed,
                    "stage_reached": min(
                        (completed[-1] + 1) if completed else 1, stages.IMPLEMENTED
                    ),
                    "stages_implemented": stages.IMPLEMENTED,
                    "source": str(source),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return target


def _interval(lo: float, hi: float) -> str:
    """One unit for the whole interval.

    Choosing per endpoint produced "15s-2min", which reads as two unrelated
    numbers. The unit is picked from the upper bound, so both ends agree.
    """
    if hi < 90:
        return f"{lo:.0f}s-{hi:.0f}s"
    # max(1, ...) because a 30 s bound still reads as "0min" on the low end.
    return f"{max(lo / 60, 0.1):.1f}min-{hi / 60:.0f}min"


def _estimate(audio_seconds: float) -> tuple[float, float]:
    """Wall-clock estimate for stage 3, in seconds.

    Short inputs are not faster per second of audio -- they are *slower*,
    because the whisper.cpp warm-up is a fixed cost that a long lecture
    amortises away. Measured here: 25 s of audio took 4.9 s (0.20x), the 4396 s
    webm 257.2 s (0.057x) and the 4520 s mp4 315.4 s (0.070x). So below
    WARMUP_SECONDS the floor is the slow end; using the fast end there would
    print a lower bound 3.9x below the 25 s measurement, which is the ADR-0001 D7
    failure mode exactly.

    HEADROOM widens the upper bound so the interval is never degenerate: with
    the floor already at the slow end, a short clip would otherwise print the
    same number twice ("expect 5s-5s"), which reads as a claim of precision
    nobody has.
    """
    fast, slow = stt.RATE_RANGE
    floor = slow if audio_seconds < stt.WARMUP_SECONDS else fast
    return audio_seconds * floor, audio_seconds * slow * HEADROOM


def _chunk_line(out) -> Callable[[int, int], None]:
    """Stage 3's progress callback, in the one shape every counting stage shares (#197).

    A thin adapter, not a second renderer: `stages.progress_line` owns the text, the flush and
    the swallowed failure, and stages 6, 7 and 9 write through it too. `chunks` is stage 3's
    unit -- a slice of the uploaded wav (`stt/openai_compat.py:240-267`, #125) -- and the only
    unit a backend reports; a backend with none never calls this (#193).
    """
    return lambda done, total: stages.progress_line(out, "chunks", done, total)


def publishes_stage7_transcript(sdir: Path, bundle: Bundle) -> dict[str, Path]:
    """Publish stage 7's artefacts out of its work directory, into the bundle.

    `synth-llm-transcript.json` is conditional on the LLM synthesizer having run -- the
    template one makes no calls and writes no transcript -- so it is published with
    `publish_optional`, which says so in the call rather than in a stat beside it.

    The transcript used to be missing from this list entirely. `synth.py` has written it
    since #38, into `work.dir("synth")`, and the list named three files, so the 8 calls that
    wrote the note were recorded and then deleted with the work directory. Measured on run 6:
    the bundle held `audit-llm-transcript.json` with 8 stage-9 calls and no stage-7
    equivalent. ADR-0004 D4 -- a regression gate that cannot attribute a difference is not a
    gate -- so this returns what it published rather than returning nothing, which lets the
    caller record it in the run's artefact registry.
    """
    artefacts = {extra: bundle.publish(extra, sdir / extra) for extra in synth.STAGE7_ARTEFACTS}
    transcript = bundle.publish_optional(
        synth.LLM_TRANSCRIPT_NAME, sdir / synth.LLM_TRANSCRIPT_NAME
    )
    if transcript is not None:
        artefacts[synth.LLM_TRANSCRIPT_NAME] = transcript
    return artefacts


# --- #115: the pre-caption fork -------------------------------------------------------
#: `DYAK_SERIAL` values that ask for the sequential branch order, and the ones that keep the
#: default -- concurrent branches. Anything else is refused by `_serial_mode` rather than read
#: as "off": a kill-switch that silently ignores a typo is the one thing an escape hatch must
#: not do (`docs/running.md`'s rule for a bad setting value, `openai_compat._threshold_bytes`).
SERIAL_ON = frozenset({"1", "true", "yes", "on"})
SERIAL_OFF = frozenset({"", "0", "false", "no", "off"})


def _serial_mode() -> bool:
    """Whether `DYAK_SERIAL` asks for the sequential order of the #115 branches.

    Off by default: the branches are the point, and the variable is the escape hatch -- a
    benchmark control arm, and the way to run one branch at a time on a host where the two
    contend for the one resource that matters (both are mostly external processes, so the
    contention is on disk and on the network, and nothing here measures it).
    """
    raw = (resolve(SERIAL_ENV, default="0").value or "").strip().lower()
    if raw in SERIAL_ON:
        return True
    if raw in SERIAL_OFF:
        return False
    raise runner.DependencyError(
        name="pipeline",
        code=ec.USAGE,
        message=f"{SERIAL_ENV} is not a yes/no value: {raw!r}",
        remediation=f"set {SERIAL_ENV}=1 for the sequential branch order, or unset it",
    )


_T = TypeVar("_T")


class _Branch(Generic[_T]):
    """One of the two pre-caption branches, running its body on its own thread (#115).

    The body's failure is **recorded**, never raised in the thread. The run raises the first
    branch failure only after both branches have been joined, which is what lets the surviving
    branch finish and publish: with the STT endpoint down, the bundle still holds the frames
    and the quality gate, exactly as it would have if the failure had happened after them. A
    branch that is *not* waited for is the failure mode this type exists to prevent.
    """

    def __init__(self, tag: str, body: Callable[[], _T]) -> None:
        self.tag = tag
        self.value: _T | None = None
        self.error: BaseException | None = None
        #: When the body raised, not when the caller noticed: with both branches in flight,
        #: "the first fatal error" has to mean the first one that actually happened.
        self.failed_at = 0.0
        # Daemon: a branch waiting on an external process must never be what keeps the
        # interpreter alive after the main thread has decided the run is over.
        self._thread = threading.Thread(
            target=self._run, args=(body,), name=f"dyak-branch-{tag}", daemon=True
        )
        self._thread.start()

    def _run(self, body: Callable[[], _T]) -> None:
        try:
            self.value = body()
        except BaseException as exc:  # re-raised by `finish`/the join, in the main thread
            self.error = exc
            self.failed_at = time.monotonic()

    def wait(self) -> None:
        """Block until the body returned, however it ended. Nothing is raised here."""
        self._thread.join()

    def finish(self) -> _T:
        """The branch's value. Only call after `wait()`; a branch that failed re-raises here."""
        if self.error is not None:
            raise self.error
        if self.value is None:
            raise RuntimeError(f"branch {self.tag} recorded neither a value nor a failure")
        return self.value


class _TaggedStream:
    """One branch's writer: a lock so lines cannot tear, and the branch's tag on each line.

    Two branches write to one stream now, and two things follow. The lock makes a line atomic
    -- without it, `[4/12] frames` can land in the middle of `[3/12] stt starting ...`. The tag
    makes the interleaving readable: stage lines keep their `[n/12] name` form, and every line a
    branch writes ends with `(A)` or `(B)`, including the `chunks 0/3` continuation lines that
    carry no stage prefix by design (#197) -- a reader can attribute a line it did not see begin.

    A serial run writes to the bare stream instead, so its output is the pre-#115 rendering
    byte for byte; that is what makes `DYAK_SERIAL=1` a control arm rather than a third format.
    """

    def __init__(self, out, tag: str, lock: threading.Lock) -> None:
        self._out = out
        self._suffix = f"  ({tag})"
        self._lock = lock
        self._pending = ""

    def write(self, text: str) -> None:
        with self._lock:
            self._pending += text
            lines = self._pending.split("\n")
            # Whatever followed the last newline is a partial line: held until the rest of it
            # arrives (or `flush`), so a tag is never written into the middle of a line.
            self._pending = lines.pop()
            for line in lines:
                self._out.write(f"{line}{self._suffix}\n")

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._out.write(f"{self._pending}{self._suffix}\n")
                self._pending = ""
            self._out.flush()


def run(
    source: Path,
    work: WorkDir,
    *,
    bundle: Bundle | None = None,
    stream=None,
    note_title: str | None = None,
    glossary_path: str | None = None,
    terms_dir: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> RunResult:
    """Stages 1-12 on `source`, with the run's own state recorded either way.

    Intermediate artefacts land in `work`, which is scratch and is removed on success.
    Anything worth keeping is published into `bundle` as it is produced, so deleting the
    work dir can never take a deliverable with it -- and so the reverse cannot happen
    either, a deliverable written while the scratch dir is left behind.

    Raises runner.DependencyError for a missing or failed dependency, and
    runner.DependencyError with code 1 for an input the pipeline cannot use.
    Both are reported by the caller, which owns exit codes and rendering.

    The run's identity and outcome go into `RUN_STATE_NAME` from a `finally`, and so do the
    per-stage seconds. Before #92 both were written only on the last line of a successful
    run, which meant a bundle left by an interrupt held everything stage 7 had published and
    nothing saying the run had not finished -- indistinguishable, to a reader, from a
    complete run whose verification artefacts happened to be missing.
    """
    out = stream if stream is not None else sys.stdout
    bundle = bundle if bundle is not None else Bundle.open(source, overwrite=False)
    reports: list[StageReport] = []
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    run_id = run_id_of(source, started_at)
    outcome, detail = "failed", "the run ended without recording an outcome"
    try:
        result = _stages(
            source,
            work,
            out=out,
            bundle=bundle,
            reports=reports,
            run_id=run_id,
            started_at=started_at,
            note_title=note_title,
            glossary_path=glossary_path,
            terms_dir=terms_dir,
            vault_path=vault_path,
        )
    except KeyboardInterrupt as exc:
        # Ctrl-C is not a failure of the pipeline, and the exit code says so (130). The
        # bundle still has to say the run stopped, or the next reader cannot tell.
        outcome, detail = "interrupted", type(exc).__name__
        raise
    except BaseException as exc:
        # Every other exception, recorded and re-raised unchanged: the caller owns exit codes.
        outcome, detail = "failed", type(exc).__name__
        raise
    else:
        outcome, detail = "completed", ""
        return result
    finally:
        write_timings(bundle, reports)
        write_run_state(
            bundle,
            source=Path(source),
            run_id=run_id,
            started_at=started_at,
            reports=reports,
            outcome=outcome,
            detail=detail,
        )


def _stages(
    source: Path,
    work: WorkDir,
    *,
    out,
    bundle: Bundle,
    reports: list[StageReport],
    run_id: str = "",
    started_at: str = "",
    note_title: str | None = None,
    glossary_path: str | None = None,
    terms_dir: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> RunResult:
    """The stages. `run` owns the run state; this owns the work."""
    # Before anything is written. `Bundle.open` only removes the root when `overwrite=True`,
    # so without this a run merges into the previous run's bundle: measured mid-run on
    # lecture 1, 12 files from the last run sat beside the 17 this one had written, two of
    # them on the `REQUIRED` set. Stage 12 would then verify the earlier run's `audit.json`
    # and exit 0. This is the root-level half of the rule `Bundle.clear_images` applies to
    # `images/`; see `Bundle.clear_root`.
    # Stage 6 reuses the previous run's captions, and `clear_root` removes `captions.json`,
    # so take a copy first. A missing cache is fine: stage 6 captions every frame then.
    previous_captions = work.sub("previous-captions.json")
    if (bundle.root / caption.REPORT_NAME).is_file():
        shutil.copyfile(bundle.root / caption.REPORT_NAME, previous_captions)
    superseded = bundle.clear_root()
    if superseded:
        shown = ", ".join(superseded[:4])
        tail = ", ..." if len(superseded) > 4 else ""
        out.write(f"  bundle    cleared {len(superseded)} from an earlier run: {shown}{tail}\n")

    # --- stage 1: probe -------------------------------------------------------
    began = time.monotonic()
    info = probe.probe(Path(source))
    probe.require_supported(info)
    reports.append(StageReport(1, "probe", time.monotonic() - began, info.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stages 2-5: two branches, joined by stage 6 (#115) -------------------
    # Stages 2-3 (`audio`, `stt`) and 4-5 (`frames`, `quality`) share no data: `stt` reads
    # `audio.wav`, `quality` reads the frames stage 4 extracted, and neither reads the other's
    # output. In sequence their seconds add up -- on the user's 4160 s lecture, stage 3 took
    # 206.5 s and stages 4-5 took 654.7 s, so stage 6 could not start until 861.2 s had passed.
    # As branches they overlap, and the join is stage 6, the first stage that needs both
    # (`caption.run` is handed `paths["json"]` and the manifest).
    #
    # Both bodies are the pre-#115 code, unchanged except for the stream they write to and the
    # values they return. They stay inside `_stages` rather than moving out because each is
    # registered in `reports` and in the bundle as its own stage completes -- that is the
    # "published as it completes" rule -- and because `tests/test_claims.py` reads this file's
    # `StageReport` calls out in stage order.
    serial = _serial_mode()
    lock = threading.Lock()

    def stt_branch(branch_out) -> _SttHalf:
        """Stage 2 and stage 3: the audio track, then its transcript."""
        # --- stage 2: audio ---------------------------------------------------
        began = time.monotonic()
        wav = work.sub("audio.wav")
        artefact = audio.extract(Path(source), wav)
        summary = artefact.summary()
        warning = audio.check_coverage(info, artefact)
        if warning:
            summary += f"  [warn] {warning}"
        reports.append(StageReport(2, "audio", time.monotonic() - began, summary))
        branch_out.write(reports[-1].line() + "\n")
        # Stages 1 and 2 record one document between them; see probe.provenance for why it
        # is one and not two. Published here because the bundle exists by now.
        if bundle is not None:
            source_prov = work.sub(probe.PROVENANCE_NAME)
            source_prov.write_text(
                probe.provenance(info, artefact, ffmpeg_version=frames.ffmpeg_version()) + "\n",
                encoding="utf-8",
            )
            bundle.publish(source_prov.name, source_prov)

        # --- stage 3: stt -----------------------------------------------------
        began = time.monotonic()
        # The backend is resolved *before* the line, so the line cannot name one that will not run
        # (#186): `auto` prefers whisper.cpp but falls back to the OpenAI-compatible route
        # (`stt/backends.py:129-134`), and the old literal named whisper.cpp and a local CPU
        # regardless.
        # `name`/`target` is the pair `stt-provenance.json` records, so the log and the provenance
        # now agree by construction. The interval is whisper.cpp's own measurement
        # (`stt/core.py:46-52`); nothing in the tree measures a hosted endpoint's rate, and an
        # invented one is a worse claim than none -- #32 is where that measurement belongs. Note
        # the residual gap: a *remote* whisper.cpp still borrows this host's interval, because
        # nothing classifies the target (loopback vs remote) yet.
        #
        # Named `backend_info`, not `info`: `info` is the probe's media description and is what
        # `RunResult.info` declares (`pipeline.py:89`). #186's fix assigned the backend into that
        # name from inside the stage, so `RunResult.info` had silently been a `BackendInfo`; no
        # consumer read it, and a name one branch overwrites while the other reads is the kind of
        # coupling the fork has to remove rather than add.
        backend_info = stt.backend().info()
        rate = (
            f"expect {_interval(*_estimate(artefact.duration))}"
            if backend_info.name == "whispercpp"
            else "no rate measured for this endpoint"
        )
        progress = (
            "this backend reports no progress"
            if backend_info.name == "whispercpp"
            else "reporting chunk progress"
        )
        branch_out.write(
            f"  [3/{IMPLEMENTED}] {'stt'.ljust(7)} starting  {artefact.duration:.0f}s of audio through "
            f"{backend_info.name} at {backend_info.target}, {rate}; {progress}\n"
        )
        branch_out.flush()
        transcript = stt.transcribe(
            wav,
            audio_duration=artefact.duration,
            # The resolved name, not the setting: the line's claim and the work cannot diverge.
            backend=backend_info.name,
            on_progress=_chunk_line(branch_out),
        )
        paths = stt.write(transcript, work.path)
        if bundle is not None:
            # Every stage that writes to the work dir has to name what it publishes here, or the
            # document dies with the work dir. That is how stage 7's transcript went missing for
            # its whole life (#76) and how stage 3 had no provenance at all (#80).
            for key, path in paths.items():
                paths[key] = bundle.publish(path.name, path)
        reports.append(StageReport(3, "stt", time.monotonic() - began, transcript.summary()))
        branch_out.write(reports[-1].line() + "\n")

        for anomaly in transcript.anomalies[:10]:
            branch_out.write(f"           note: {anomaly}\n")
        if len(transcript.anomalies) > 10:
            branch_out.write(f"           note: ... and {len(transcript.anomalies) - 10} more\n")
        return _SttHalf(audio=artefact, paths=paths, transcript=transcript)

    def frames_branch(branch_out) -> _FramesHalf:
        """Stage 4 and stage 5: detect the frames, then gate what stage 6 will caption."""
        published: dict[str, Path] = {}
        # --- stage 4: frames --------------------------------------------------
        # ADR-0001 D3: frames are read from the source, never from a blurred intermediate. The
        # detect pass decodes the whole container and writes only timestamps, so the
        # blur cannot reach the output; the extract pass then seeks the original.
        began = time.monotonic()
        frames_dir = work.dir("frames")
        manifest, produced = frames.run(Path(source), frames_dir)
        reports.append(
            StageReport(4, "frames", time.monotonic() - began, frames.summary(manifest, produced))
        )
        branch_out.write(reports[-1].line() + "\n")

        published["manifest"] = bundle.publish("manifest", manifest)
        # Frames go to the bundle's images/ so the note can reference them by a stable
        # relative path, and the manifest travels with them.
        #
        # The move happens *after* stage 5. It used to happen before, which handed stage 5 a
        # list of paths that no longer existed: all 16 frames failed with "cannot read the
        # frame size". The unit tests could not see it because the stubbed pipeline never
        # moves anything -- only the end-to-end run did.
        for extra in ("detect.json",):
            extra_published = bundle.publish_optional(extra, frames_dir / extra)
            if extra_published is not None:
                published[extra] = extra_published

        # --- stage 5: quality -------------------------------------------------
        # ADR-0001 D4. Runs on the frames stage 4 produced, not on the source, so the gate judges
        # what stage 6 will actually caption. ADR-0001 D4's exit-4 path is signalled by
        # `quality_ok` rather than raised here: raising would skip the publishing of the
        # enhanced frames, and a failed gate is exactly when those frames are worth
        # looking at.
        began = time.monotonic()
        qs = quality.Settings()
        bbox_source = resolve(BBOX_SOURCE_ENV, default="geometric").value
        quality.resolve_estimator(bbox_source)
        qreport = quality.run(
            produced, work.path, settings=qs, bbox_source=bbox_source, stream=branch_out
        )

        # Publish the gated frames only now, so stage 5 read real files. `images/` is emptied
        # here rather than by `clear_root` at the top of the run, because it must be empty at the
        # moment the frames land, and by then the root's own artefacts are wanted. Same rule as
        # the root's, one level down: the bundle is opened with overwrite=False, so without this
        # the directory is the union of every run that has ever touched it. See
        # `Bundle.clear_images` and `Bundle.clear_root`.
        bundle.clear_images()
        for index, path in enumerate(produced):
            target = bundle.images / path.name
            shutil.move(str(path), str(target))
            bundle.record(f"frame{index}", target)
            published[f"frame{index}"] = target
        reports.append(StageReport(5, "quality", time.monotonic() - began, qreport.summary()))
        branch_out.write(reports[-1].line() + "\n")

        qdir = work.dir("quality")
        quality.write(qreport, qdir / quality.REPORT_NAME, qs)
        published[quality.REPORT_NAME] = bundle.publish(
            quality.REPORT_NAME, qdir / quality.REPORT_NAME
        )
        (qdir / quality.PROVENANCE_NAME).write_text(
            quality.provenance(qs, bbox_source) + "\n", encoding="utf-8"
        )
        published[quality.PROVENANCE_NAME] = bundle.publish(
            quality.PROVENANCE_NAME, qdir / quality.PROVENANCE_NAME
        )

        enhanced: list[Path] = []
        for entry in qreport.passed:
            candidate = work.path / "enhanced" / quality.enhanced_name(entry.name)
            if not candidate.is_file():
                continue
            target = bundle.images / candidate.name
            shutil.move(str(candidate), str(target))
            bundle.record(f"quality:{entry.name}", target)
            published[f"quality:{entry.name}"] = target
            enhanced.append(target)
        return _FramesHalf(
            manifest=manifest,
            produced=produced,
            quality=qreport,
            enhanced=enhanced,
            published=published,
        )

    if serial:
        # One thread, stage order, and the pre-#115 rendering: nothing writes through a
        # `_TaggedStream`. A failure ends the run where it happened, as it always did -- the
        # failure policy below is about two branches, not about a run that never had two.
        stt_half = stt_branch(out)
        frames_half = frames_branch(out)
    else:
        stt_running = _Branch("A", lambda: stt_branch(_TaggedStream(out, "A", lock)))
        frames_running = _Branch("B", lambda: frames_branch(_TaggedStream(out, "B", lock)))
        # Both are joined before anything is raised: the policy is "fail on the first fatal
        # error, but let the surviving branch finish and publish" (#115), and publishing happens
        # inside the branch. Raising at the first failure would strand stage 4's frames in the
        # work directory, which is removed when the run ends.
        stt_running.wait()
        frames_running.wait()
        # Each branch appends its own `StageReport` as its stage completes, so the list order is
        # whichever branch finished first. `timings.json` and `run-state.json` are read in this
        # order, so it is sorted back into stage order here rather than left to the race.
        reports.sort(key=lambda report: report.index)
        failures = sorted(
            (branch for branch in (stt_running, frames_running) if branch.error is not None),
            key=lambda branch: branch.failed_at,
        )
        if failures:
            raise failures[0].error
        stt_half = stt_running.finish()
        frames_half = frames_running.finish()

    artefact = stt_half.audio
    paths = stt_half.paths
    transcript = stt_half.transcript
    produced = frames_half.produced
    qreport = frames_half.quality
    enhanced = frames_half.enhanced
    # Stage 3's keys first, then branch B's, which is the order stages 4 and 5 registered them
    # in before #115: `report.run` walks this dict, and `RunResult.artefacts` is the registry
    # the vault export copies by.
    artefacts = dict(paths)
    artefacts.update(frames_half.published)

    # --- stage 6: caption -----------------------------------------------------
    # Deterministic. Runs on the frames stage 5 passed and the transcript stage 3 wrote;
    # there is no model in the alignment half, by design, so that half can be checked against
    # the artefact rather than believed. The captioning half does call one.
    #
    # The quality report comes from `artefacts`, not `qdir`, and that is the whole fix:
    # `Bundle.publish` *moves*, so the work-dir path stage 5 wrote is dangling by the time
    # stage 6 reads it. `align` treats a missing file as "no gate information", which makes
    # the guard `if alignment.quality_gate and ...` vacuously false -- stage 5's gate became a
    # no-op in the real pipeline, and all 58 frames were captioned including the one stage 5
    # rejected. `publish` used to return the source path unchanged when it did not exist
    # rather than raising, so no run ever reported it. It raises now (#89), which is why the
    # bundle's copies are the only ones this stage is handed. The signature in the artefact
    # was `quality_gate: ""` on all 58 frames where stage 5 had recorded one FAIL. #82.
    began = time.monotonic()
    capdir = work.dir("caption")
    creport, coutcome = caption.run(
        # The bundle's copies, not the work dir's. `publish` moved both, so the work-dir
        # paths are dangling -- the same move-then-read ordering bug stage 5 had.
        artefacts["manifest"],
        paths["json"],
        artefacts[quality.REPORT_NAME],
        bundle.images,
        capdir,
        # The bundle's copy from the previous run, not `capdir`: `capdir` is scratch and
        # empty on every run, so caching from it re-called all 58 frames on run 10 (#83).
        cache_source=previous_captions if previous_captions.is_file() else None,
        stream=out,
    )
    for extra in (caption.REPORT_NAME, caption.PROVENANCE_NAME):
        artefacts[extra] = bundle.publish(extra, capdir / extra)
    reports.append(StageReport(6, "caption", time.monotonic() - began, creport.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stage 7: synth ------------------------------------------------------
    # The first stage that may need a model. Two implementations of one Protocol; the
    # template one runs with nothing configured, so an unavailable endpoint costs the note's
    # quality rather than the note itself. Which one ran is in provenance, not in prose.
    began = time.monotonic()
    sdir = work.dir("synth")
    try:
        llm_config = llm.Config.from_env()
    except llm.NotConfiguredError:
        llm_config = None
    note = synth.run(
        artefacts["captions.json"],
        artefacts["txt"],
        Path(source),
        sdir,
        config=llm_config,
        title=note_title,
        stream=out,
    )
    # The transcript is conditional: it exists only when the LLM synthesizer ran, and the
    # template one makes no calls to record. Stage 9 guards its equivalent the same way.
    artefacts.update(publishes_stage7_transcript(sdir, bundle))
    reports.append(
        StageReport(
            7,
            "synth",
            time.monotonic() - began,
            f"{note.synthesizer}, {note.markdown.count(chr(10))} lines, "
            f"{note.filled}/{len(synth.SECTIONS)} sections filled"
            + ("  DEGRADED" if note.degraded else ""),
        )
    )
    out.write(reports[-1].line() + "\n")

    # --- stage 8: lint --------------------------------------------------------
    # Deterministic, and every rule is a delimiter count or a string match. A model asked
    # whether `$...$` is balanced is strictly worse than a counter, so nothing here is one.
    began = time.monotonic()
    ldir = work.dir("lint")
    lreport = lint.run(artefacts[synth.NOTE_NAME], bundle.images, ldir, stream=out)
    for extra in (lint.REPORT_NAME, lint.PROVENANCE_NAME):
        artefacts[extra] = bundle.publish(extra, ldir / extra)
    reports.append(StageReport(8, "lint", time.monotonic() - began, lreport.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stage 9: audit --------------------------------------------------------
    # Tier 1 is deterministic and carries the load. Tier 2 is a zero-shot critic that sees
    # the note and nothing else -- a model reviewing its own output in the same context
    # ratifies the error it is shown.
    began = time.monotonic()
    adir = work.dir("audit")
    areport = audit.run(
        artefacts[synth.NOTE_NAME],
        artefacts["txt"],
        adir,
        glossary_path=Path(glossary_path) if glossary_path else None,
        config=llm_config,
        stream=out,
    )
    for extra in audit.ALL_ARTEFACTS:
        published = bundle.publish_optional(f"audit/{extra}", adir / extra)
        if published is not None:
            artefacts[f"audit/{extra}"] = published
    reports.append(StageReport(9, "audit", time.monotonic() - began, areport.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stage 10: repair -----------------------------------------------------
    began = time.monotonic()
    rdir = work.dir("repair")
    # The published report, not `adir / audit.REPORT_NAME`. Stage 9 publishes (MOVES) the
    # report into the bundle -- `Bundle.publish` is a `shutil.move` (bundle.py:271) -- so the
    # work-dir path was empty by the time this ran, `repair.run` found no report at all, and
    # only the lint findings were declined. Measured on the 2026-10-12 run: `audit.json` held
    # 14 findings and `repair.json` declined one, the lint rule `math/display-unbalanced`
    # (#253). The same mechanism as #82/#89: a reader handed the pre-publish path.
    rreport = repair.run(
        artefacts[synth.NOTE_NAME],
        artefacts[f"audit/{audit.REPORT_NAME}"],
        rdir,
        glossary_path=Path(glossary_path) if glossary_path else None,
        lint_report_path=artefacts[lint.REPORT_NAME],
        stream=out,
        # The Config the run already holds (#257): stage 10 sends no prompt, but its provenance
        # records the endpoint and the seed, so `repair-provenance.json` is the fourth stage
        # provenance the #77 decision names. Same `llm_config` as stages 6, 7 and 9 above.
        config=llm_config,
    )
    for extra in (repair.REPAIRED_NOTE, repair.REPORT_NAME, repair.PROVENANCE_NAME):
        artefacts[f"repair/{extra}"] = bundle.publish(f"repair/{extra}", rdir / extra)
    reports.append(StageReport(10, "repair", time.monotonic() - began, rreport.summary()))
    out.write(reports[-1].line() + "\n")

    # --- stage 11: link --------------------------------------------------------
    # Links are injected into the REPAIRED note, not the stage-7 original: the repair stage
    # exists because stage 7 writes a note that can be wrong, and linking the wrong text
    # would give a wikilink to a defect that stage 10 just declined to fix.
    began = time.monotonic()
    ldir2 = work.dir("link")
    # The vault is resolved the same way `doctor` resolves it, not only when `--vault` was
    # passed. Otherwise stage 11 looks for `mscs/_terms` under a `None` vault and reports
    # "not configured" on a machine where `dyak doctor` just named the directory that
    # holds them.
    tdir = link.resolve_terms_dir(terms_dir, deps.resolve_vault(vault_path))
    lreport2 = link.run(
        rdir / repair.REPAIRED_NOTE
        if (rdir / repair.REPAIRED_NOTE).is_file()
        else artefacts[synth.NOTE_NAME],
        ldir2,
        terms_dir=tdir,
        stream=out,
    )
    for extra in link.ALL_ARTEFACTS:
        published = bundle.publish_optional(f"link/{extra}", ldir2 / extra)
        if published is not None:
            artefacts[f"link/{extra}"] = published
    reports.append(StageReport(11, "link", time.monotonic() - began, lreport2.summary()))
    out.write(reports[-1].line() + "\n")
    note_artefact = ldir2 / link.LINKED_NOTE

    # --- stage 12: report --------------------------------------------------------
    # The last stage, and the only one that verifies instead of writes. Everything before it
    # makes claims; this one stats the filesystem and compares. Exit 0 depends on this.
    began = time.monotonic()
    rreport2 = report.run(
        bundle.root,
        final_note=note_artefact,
        synth_note=Path(artefacts[synth.NOTE_NAME]),
        artefacts=artefacts,
        superseded=superseded,
        # #88: the verdict names the run it belongs to. `Bundle.open` merges into an existing
        # directory, so two runs of one lecture leave overlapping file sets, and without this
        # nothing said which run wrote which file.
        run_id=run_id,
        started_at=started_at,
        stream=out,
    )
    artefacts["note"] = bundle.root / synth.NOTE_NAME
    artefacts[report.REPORT_NAME] = bundle.root / report.REPORT_NAME
    # #90: stage 12's two artefacts were written straight into `bundle.root` and never
    # registered, and `export_to_vault` copies by registry. The vault therefore received the
    # note and every input to it, and not the verdict that approved it. `timings.json` is the
    # one file that stays out, and it stays out on purpose: see `TIMINGS_NAME` above.
    for stage12_artefact in report.ALL_ARTEFACTS:
        bundle.record(stage12_artefact, bundle.root / stage12_artefact)
    reports.append(StageReport(12, "report", time.monotonic() - began, rreport2.summary()))
    # No `out.write(reports[-1].line())` here, and that is deliberate: `report.run` has already
    # printed its own `[12/12] report OK: ...` (`report.py`), immediately followed by the MISSING
    # lines it exists to justify. A second line would repeat the stage and carry the same
    # seconds. Stage 12 is therefore the one stage whose seconds reach `timings.json` and not
    # the terminal -- it takes under 0.1 s, and `report.json` carries its verdict. #97.

    return RunResult(
        source=Path(source),
        info=info,
        audio=artefact,
        transcript=transcript,
        frames=[artefacts[f"frame{i}"] for i in range(len(produced))],
        artefacts=artefacts,
        reports=tuple(reports),
        bundle=bundle,
        quality=qreport,
        enhanced=enhanced,
        caption=creport,
        caption_outcome=coutcome,
        note=note,
        lint=lreport,
        audit=areport,
        repair=rreport,
        link=lreport2,
        final_note=note_artefact,
        report=rreport2,
    )
