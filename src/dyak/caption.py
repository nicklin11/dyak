"""Stage 6: put each frame next to the words being spoken when it was on screen.

Deterministic, and deliberately so. The useful work here is temporal alignment: every frame
stage 5 passed gets the slice of transcript that was live while it was displayed. That is
arithmetic over timestamps, not inference, and putting a model in the middle of it would
make the one part of this stage that can be verified the one part that cannot be.

Alignment is by *overlap*, not by nearest-timestamp. A frame is on screen from the moment
the previous frame was replaced until it is replaced itself, so it corresponds to the
interval `[previous_pts, pts)` -- for the first frame, `[0, pts)`. Selecting segments that
merely start nearest to the frame would drop everything said while a slide sat there, which
for a 180 s slide gap is most of the lecture.

## What this stage does not do

It does not OCR. Recovering text from the frame is a separate concern from captioning it,
and adding a binary dependency for it would make stage 6 unable to run at all on hosts that
only need the alignment.

## Captioning

Captioning is inference, so it is separated from alignment in three ways: it runs after the
deterministic half has already produced its answer, it never feeds back into the alignment,
and its results are cached by `(frame fingerprint, model id)` so a re-run that changed the
transcript but not the slides does not pay for the same caption twice.

A stage that cannot run must say so rather than write a string that reads like content. If
no vision endpoint is configured, `run()` raises `NotConfiguredError`; it does not produce
`caption: null` in the artefact and call the run clean.

The vision endpoint has its own configuration names (`DYAK_VLM_*`) and falls back to the
text ones, per ADR-0004 D3: separate names because the roles are separate, not because they
have to be separate places.
"""

from __future__ import annotations

import json
import sys
from . import exitcodes as ec, llm, quality, runner, stages
from dataclasses import dataclass, field
from pathlib import Path

REPORT_NAME = "captions.json"
PROVENANCE_NAME = "caption-provenance.json"

#: A frame older than this relative to the transcript end has no words behind it at all.
#: Reported per frame rather than treated as an error: it is information about the source.
UNCOVERED = "no_transcript_coverage"

#: Above this share of uncovered frames the audio and the video are not describing the same
#: lecture, which is a muxing problem, not a captioning problem. ADR-0001 D5 exit 4.
UNCOVERED_SHARE_LIMIT = 0.30

#: What the model returns when it judges the frame to carry nothing the excerpt does not
#: already state. It is a **finding, not content**: 16 of the 57 captions on lecture 1 are
#: this string (#83). It was a bare literal inside the prompt until this constant existed,
#: which is why every consumer had to string-match it and why a refusal was indistinguishable
#: from a caption -- both were `caption_status: OK` with a non-empty string. The stage-7 status
#: that separates them is #83; this is only the name to compare against.
NO_NEW_INFORMATION = "NO NEW INFORMATION."

#: Statuses that mean stage 6 produced a description. `NO_NEW_INFORMATION` belongs here and is
#: deliberately *not* `OK`: a refusal is a finding, not a caption, and collapsing the two is
#: what made 16 of 57 captions on lecture 1 indistinguishable from content (#83). It is still
#: a **success** -- the frame cost a call and came back with an answer -- so it counts towards
#: `captioned` and never towards `failed`. Making it a failure would drop 16 frames into the
#: error count that `cli.py` reports and, at the extreme, turn an all-refusal run into exit 3.
DESCRIBED = ("OK", "NO_NEW_INFORMATION")

#: What the model is asked for. A slide in a technical lecture is mostly equations, and a
#: generic "describe this image" returns "a slide with text on it" -- content-free, and worse
#: than nothing because stage 7 will treat it as material. So the prompt asks for the things
#: the note needs and that the audio does not contain.
CAPTION_PROMPT = (
    """\
You are reading one frame from a university lecture recording. The transcript excerpt below \
is what the lecturer was saying while this frame was on screen.

Describe ONLY what is visible in the frame, in the language the frame itself is written in:

1. Every equation or formula, transcribed exactly as written, in LaTeX. Do not solve, do not \
correct, do not simplify, do not guess a symbol you cannot read. If a symbol is illegible, \
write [illegible] in its place.
2. Any definition, theorem, or named object stated on screen, quoted.
3. The structure of a diagram or graph: what is plotted against what, the axes with their \
labels and ranges, curves and their names, and the direction of any arrows.
4. Anything on screen that the transcript excerpt does not mention.

Do not comment on the lecturer, the conferencing UI, window chrome, or the room. Do not \
describe what the excerpt already says. If the frame carries no content beyond what the \
excerpt states, answer with exactly: """
    + NO_NEW_INFORMATION
)


@dataclass(frozen=True)
class Settings:
    uncovered_share_limit: float = UNCOVERED_SHARE_LIMIT
    #: Characters of transcript text carried per alignment. Enough for stage 7 to place the
    #: frame; the full transcript is a separate artefact and is read from there.
    max_chars: int = 4_000


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Alignment:
    """One frame, and the words that were live while it was up."""

    frame: str
    pts_ms: int
    image: str
    enhanced: str | None
    #: Half-open interval during which this frame was the one on screen.
    on_screen_ms: tuple[int, int]
    #: Transcript segments overlapping that interval.
    segment_indices: list[int] = field(default_factory=list)
    text: str = ""
    first_word_ms: float | None = None
    word_count: int = 0
    #: Always None until a VLM exists. Present as a key so consumers do not have to guess.
    caption: str | None = None
    caption_status: str = "NOT_CONFIGURED"
    #: What produced `caption` -- model, tokens, seconds, attempts, fingerprint, and whether
    #: this run called the endpoint or took the answer from the cache. Stage 6 makes 58 calls
    #: per lecture, the largest consumer in the pipeline, and recorded none of them (#76).
    #:
    #: A summary, not the `messages` array. `audit-llm-transcript.json` averages ~190 KiB per
    #: call because each entry carries the whole section text, so the equivalent here would be
    #: ~11 MiB per run -- more than the rest of the bundle put together. ADR-0004 D4 asks the
    #: artefact to carry enough to attribute a difference, and this does: which model, how many
    #: tokens, how long, how many attempts, and **called vs cached**.
    #:
    #: That last field is not decoration. `caption_frames` caches by `(fingerprint, model)`, so
    #: a warm re-run makes zero calls. Without an explicit marker the bundle after such a run
    #: would show no caption evidence at all, which reads as an omission rather than a hit.
    caption_trace: dict = field(default_factory=dict)
    quality_gate: str = ""
    content_type: str = ""
    crop_state: str = ""
    reason: str = ""

    @property
    def covered(self) -> bool:
        return bool(self.segment_indices)

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "pts_ms": self.pts_ms,
            "image": self.image,
            "enhanced": self.enhanced,
            "on_screen_ms": list(self.on_screen_ms),
            "segment_indices": self.segment_indices,
            "first_word_ms": self.first_word_ms,
            "word_count": self.word_count,
            "text": self.text,
            "caption": self.caption,
            "caption_status": self.caption_status,
            "caption_trace": self.caption_trace,
            "quality_gate": self.quality_gate,
            "content_type": self.content_type,
            "crop_state": self.crop_state,
            "reason": self.reason,
        }


@dataclass
class Report:
    alignments: list[Alignment] = field(default_factory=list)
    #: Carried on the report, not read from `Settings` at property time. `ok` is a property
    #: with no access to the Settings that produced the alignments, and reading the class
    #: constant instead meant a caller who lowered the limit via Settings got it silently
    #: ignored -- the gate would run at the default and say so.
    uncovered_share_limit: float = UNCOVERED_SHARE_LIMIT

    @property
    def considered(self) -> list[Alignment]:
        """Frames stage 6 was allowed to align.

        A frame the quality gate rejected is not a candidate, so it must not count against
        A/V sync. One bad frame of sixteen is not evidence that the audio and the video are
        describing different lectures, and counting it would let a blur failure masquerade
        as a muxing failure.
        """
        return [a for a in self.alignments if a.caption_status != "GATED_OUT"]

    @property
    def ok(self) -> bool:
        """Coverage is a gate: too many frames with no transcript means broken A/V sync."""
        considered = self.considered
        if not considered:
            return False
        uncovered = sum(1 for a in considered if not a.covered)
        return uncovered / len(considered) <= self.uncovered_share_limit

    @property
    def uncovered(self) -> list[Alignment]:
        return [a for a in self.considered if not a.covered]

    @property
    def uncovered_share(self) -> float:
        considered = self.considered
        if not considered:
            return 1.0
        return sum(1 for a in considered if not a.covered) / len(considered)

    def summary(self) -> str:
        if not self.considered:
            return "no frames aligned"
        words = sum(a.word_count for a in self.considered)
        gated = len(self.alignments) - len(self.considered)
        tail = f", {gated} gated out by stage 5" if gated else ""
        return (
            f"{len(self.considered)} frames aligned, {words} words, "
            f"{len(self.uncovered)} without transcript coverage{tail}"
        )

    def explain(self) -> str:
        return "\n".join(
            f"  {a.frame}: on screen {a.on_screen_ms[0]}-{a.on_screen_ms[1]} ms, "
            f"{a.word_count} words ({a.reason})"
            for a in self.uncovered
        )


def read_manifest(manifest: Path) -> list[tuple[int, str]]:
    """`(pts_ms, filename)` from stage 4's manifest. Missing or short manifest -> empty."""
    if not manifest.is_file():
        return []
    rows: list[tuple[int, str]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue
        rows.append((int(parts[0]), parts[2].strip()))
    rows.sort()
    return rows


def read_segments(transcript: Path) -> list[Segment]:
    segments: list[Segment] = []
    if not transcript.is_file():
        return segments
    payload = json.loads(transcript.read_text(encoding="utf-8"))
    for raw in payload.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        text = (raw.get("text") or "").strip()
        try:
            index = int(raw.get("index"))
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            # Reported by stage 3 as an anomaly; silently swapping the pair here would
            # hide a real decoder problem behind a plausible-looking alignment.
            continue
        segments.append(Segment(index=index, start=start, end=end, text=text))
    segments.sort(key=lambda s: (s.start, s.end))
    return segments


def overlapping(segments: list[Segment], start_ms: int, end_ms: int) -> list[Segment]:
    """Segments whose interval meets `[start_ms, end_ms)`.

    `segments` is sorted by start time, so the scan stops at the first segment that begins
    after the window rather than walking the whole transcript. On a 4 500 s lecture that is
    1 500 comparisons per frame instead of 1 500 times the frame count.
    """
    out: list[Segment] = []
    for segment in segments:
        if segment.start * 1000 >= end_ms:
            break
        if segment.end * 1000 > start_ms:
            out.append(segment)
    return out


def _first_word_ms(raw_segments: list[dict], limit_ms: int) -> float | None:
    """Start of the first word at or after `limit_ms`, in seconds.

    Word timings exist because the frame boundary is a hard edge and a segment boundary is
    not: at a slide change the previous sentence is still audible. Without word timings the
    alignment would attribute words to the new frame that were spoken against the old one.

    Every overlapping segment is searched, not just the first. The segment straddling a
    frame boundary begins before it, so all of *its* words precede the limit; looking only
    there returns None for every frame but the first, which is a silent hole in the field
    rather than a missing measurement.

    None means no word started inside the window -- the window fell entirely inside a
    long segment. That is a fact about the transcript, so it stays None rather than being
    rounded off to the segment start.
    """
    best: float | None = None
    for raw in raw_segments:
        for word in raw.get("words") or []:
            try:
                start = float(word.get("start"))
            except (TypeError, ValueError):
                continue
            if start * 1000 >= limit_ms and (best is None or start < best):
                best = start
    return best


def read_quality_entries(path: Path) -> tuple[dict[str, dict] | None, str]:
    """(frame name -> stage 5's entry for it, why the file could not be read).

    `None` with an empty reason means `quality.json` is absent -- legitimate for an
    audio-only source. `None` with a reason means it is present and unusable: truncated, not
    a JSON object, or without a `frames` list. The two are kept apart because the first is a
    state the pipeline allows and the second is a failed write.

    Stage 12 reads the same file through `report.read_quality_gate` and names the same failures
    in the same words for every shape it can read; a payload that is not a JSON object is #202
    and is named by stage 6 alone today. `tests/test_stages.py` pins the two against each other
    for the shapes both handle, so one file is not described two ways. The reader lives here
    rather than in `quality.py` because `caption` must not import `report` (`report` imports
    `caption`, `report.py:48`) and stage 6 is the only reader in this PR's scope.

    The parse is guarded because this is a trust boundary: stage 5 writes the file and stage 6
    reads it moments later, and before this function a truncated file reached the user as a
    `JSONDecodeError` traceback with Python's exit 1 -- indistinguishable from a usage error,
    and before stage 12 could name the problem (#188).
    """
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
    entries: dict[str, dict] = {}
    for entry in frames:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            entries[name] = entry
    return entries, ""


def align(
    manifest: Path,
    transcript: Path,
    quality_report: Path,
    images: Path,
    settings: Settings | None = None,
) -> Report:
    """Align every frame stage 5 passed to the words spoken while it was displayed.

    Raises `runner.DependencyError` (exit 6) when `quality.json` is present and unreadable.
    Falling back to "no gate information" would caption every frame the gate rejected, which
    is the #82 defect; the file names its reason instead.
    """
    settings = settings or Settings()
    report = Report(uncovered_share_limit=settings.uncovered_share_limit)

    frames = read_manifest(manifest)
    if not frames:
        return report

    transcript_payload = (
        json.loads(transcript.read_text(encoding="utf-8")) if transcript.is_file() else {}
    )
    segments = read_segments(transcript)

    gate_entries, gate_error = read_quality_entries(quality_report)
    if gate_error:
        # The run failed, not the file, so `name="dyak"`: runner.report prints
        # "(quality.json wrote nothing to stderr)" for a file name, which would be a false
        # claim about a file. The message is the reader's reason verbatim, so stage 6 and
        # stage 12 say one thing about one file (#188).
        raise runner.DependencyError(
            name="dyak",
            code=ec.ARTEFACT_VERIFICATION_FAILED,
            message=gate_error,
            remediation=(
                "re-run `dyak process` so stage 5 rewrites it. Do not delete it: an absent "
                f"{quality.REPORT_NAME} is read as no gate information and every frame is "
                "captioned, including the ones the gate rejected (#82)"
            ),
        )

    for position, (pts_ms, name) in enumerate(frames):
        # A frame with pts = T was on screen *from* T, until the next frame appeared. The
        # window is [pts_i, pts_{i+1}), not [pts_{i-1}, pts_i): a frame extracted at
        # 304 132 ms is the slide that appeared at 304 132 ms, and everything spoken before
        # that belongs to the previous frame.
        if position + 1 < len(frames):
            on_screen_end = frames[position + 1][0]
        else:
            # The last frame has no successor to bound it. Use the audio duration when the
            # transcript knows one, otherwise the median gap, so it is not left with a
            # zero-length window and reported as uncovered.
            on_screen_end = _tail_end_ms(frames, transcript_payload)
        gate = (gate_entries or {}).get(name, {})
        enhanced = f"{Path(name).stem}_q.jpg"
        alignment = Alignment(
            frame=name,
            pts_ms=pts_ms,
            image=name,
            enhanced=enhanced if (images / enhanced).is_file() else None,
            on_screen_ms=(pts_ms, max(on_screen_end, pts_ms + 1)),
            quality_gate=gate.get("quality_gate", ""),
            content_type=gate.get("content_type", ""),
            crop_state=gate.get("crop_state", ""),
        )

        # Only frames the gate passed carry forward. A frame stage 5 rejected has no
        # business anchoring a section of the note.
        if alignment.quality_gate and alignment.quality_gate != "PASS":
            alignment.reason = f"stage 5 {alignment.quality_gate}"
            alignment.caption_status = "GATED_OUT"
            report.alignments.append(alignment)
            continue

        hits = overlapping(segments, pts_ms, max(on_screen_end, pts_ms + 1))
        alignment.segment_indices = [s.index for s in hits]
        alignment.word_count = sum(len((s.text or "").split()) for s in hits)  # reporting only
        pieces: list[str] = []
        used = 0
        for segment in hits:
            piece = segment.text.strip()
            if not piece:
                continue
            if used + len(piece) > settings.max_chars:
                break
            pieces.append(piece)
            used += len(piece) + 1
        alignment.text = " ".join(pieces)

        alignment.first_word_ms = _first_word_ms(
            _raw_segments(transcript_payload, alignment.segment_indices), pts_ms
        )
        if not alignment.covered:
            alignment.reason = UNCOVERED
        report.alignments.append(alignment)

    return report


def _tail_end_ms(frames: list[tuple[int, str]], payload: dict) -> int:
    """Where the final frame stops being on screen."""
    last = frames[-1][0]
    try:
        audio_ms = int(float(payload.get("audio_duration") or 0) * 1000)
    except (TypeError, ValueError):
        audio_ms = 0
    if audio_ms > last:
        return audio_ms
    gaps = sorted(b - a for (a, _), (b, _) in zip(frames, frames[1:]))
    if not gaps:
        return last
    return last + gaps[len(gaps) // 2]


def _raw_segments(payload: dict, indices: list[int]) -> list[dict]:
    """The raw segment dicts behind `indices`, carrying their word timings."""
    if not indices:
        return []
    wanted = set(indices)
    return [
        raw
        for raw in payload.get("segments") or []
        if isinstance(raw, dict) and raw.get("index") in wanted
    ]


def load_cache(target: Path, model: str) -> dict[str, dict]:
    """Captions from a previous run, keyed by frame name, that this model may reuse.

    An entry survives only if the model id matches. The frame fingerprint is checked later,
    against the bytes on disk, because that is the half that cannot be known until the file
    is opened. A cache from a different model is discarded whole rather than merged: a mixed
    cache would make "which model wrote this caption" unanswerable per frame.
    """
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("captions")
    if not isinstance(entries, dict):
        return {}
    return {
        name: entry
        for name, entry in entries.items()
        if isinstance(entry, dict) and entry.get("model") == model
    }


def caption_frames(
    report: Report,
    images: Path,
    config: llm.Config,
    *,
    cache: dict[str, dict] | None = None,
    stream=None,
) -> dict[str, dict]:
    """Caption every frame stage 5 passed. Returns the cache entries to persist.

    A frame that fails is recorded with its own `caption_status` and does not abort the
    stage: eleven captions and one failure is a useful run, and losing the eleven to report
    the one would not be. A stage where *every* frame failed is different, and
    `CaptionOutcome.ok` is what makes that visible.
    """
    out = stream if stream is not None else sys.stdout
    cache = cache if cache is not None else {}
    produced: dict[str, dict] = {}
    reused = 0
    failed = 0
    refused = 0
    breaker = llm.Breaker()
    stopped = False

    for index, alignment in enumerate(report.considered):
        # First statement of the body, before the breaker and cache checks (#197): every
        # considered frame advances the counter -- cached, missing, stopped or called -- so a
        # unit that leaves by `continue` cannot freeze it. The stage's own result line carries
        # completion; this one is `frames 0/N` before the first call.
        stages.progress_line(out, "frames", index, len(report.considered))
        if stopped:
            # The endpoint is down; the remaining frames cost a full retry ladder each
            # (~6 s fast-refusing, 30 min hung) and buy nothing (#124).
            alignment.caption_status = "STOPPED"
            alignment.reason = (
                f"not attempted: {breaker.limit} consecutive connection failures stopped stage 6"
            )
            alignment.caption_trace = {"source": "breaker"}
            failed += 1
            continue
        source = alignment.enhanced or alignment.image
        path = images / source
        if not path.is_file():
            alignment.caption_status = "FRAME_MISSING"
            alignment.reason = f"{source} is not in the bundle"
            alignment.caption_trace = {"source": "missing", "image": source}
            failed += 1
            continue

        fingerprint = llm.frame_fingerprint(path)
        prior = cache.get(alignment.frame)
        if prior and prior.get("fingerprint") == fingerprint and prior.get("text"):
            alignment.caption = str(prior["text"])
            # The same mapping the cold path applies. A cache hit that hardcoded `OK` would
            # make a warm re-run report all 57 frames described when 16 of them are refusals --
            # a bundle that contradicts the run before it, from the same input.
            is_refusal = alignment.caption == NO_NEW_INFORMATION
            alignment.caption_status = "NO_NEW_INFORMATION" if is_refusal else "OK"
            alignment.reason = (
                "the model found nothing on screen that the excerpt does not already state"
                if is_refusal
                else ""
            )
            if is_refusal:
                refused += 1
            # `cached`, not `called`. A warm re-run makes zero requests, and the bundle has to
            # say so -- an empty trace here would read as "stage 6 did not run".
            alignment.caption_trace = {
                "source": "cached",
                "fingerprint": fingerprint,
                "model": prior.get("model"),
                "served_model": prior.get("served_model"),
            }
            reused += 1
            continue

        try:
            reply = llm.chat(
                [llm.vision_message(CAPTION_PROMPT, path, text=alignment.text)],
                config,
                max_tokens=1024,
            )
        except llm.NotConfiguredError:
            raise
        except llm.EndpointError as exc:
            alignment.caption_status = "ERROR"
            alignment.reason = f"vision endpoint refused: {exc}"
            alignment.caption_trace = {
                "source": "error",
                "fingerprint": fingerprint,
                "model": config.model,
                "detail": str(exc),
            }
            failed += 1
            if exc.connection:
                stopped = breaker.fail()
            else:
                # The endpoint answered, it just said no. It is up, so the run of
                # connection failures is over.
                breaker.ok()
            continue

        text = reply.text.strip()
        breaker.ok()
        if not text:
            alignment.caption_status = "EMPTY"
            alignment.reason = "vision model returned nothing; empty completions are not retried"
            alignment.caption_trace = {
                "source": "empty",
                "fingerprint": fingerprint,
                "model": config.model,
                "served_model": reply.served_model,
                "attempts": reply.attempts,
            }
            failed += 1
            continue

        alignment.caption = text
        is_refusal = text == NO_NEW_INFORMATION
        alignment.caption_status = "NO_NEW_INFORMATION" if is_refusal else "OK"
        alignment.reason = (
            "the model found nothing on screen that the excerpt does not already state"
            if is_refusal
            else ""
        )
        if is_refusal:
            refused += 1
        alignment.caption_trace = {
            "source": "called",
            "fingerprint": fingerprint,
            **reply.as_dict(),
        }
        produced[alignment.frame] = {
            "fingerprint": fingerprint,
            "model": config.model,
            "served_model": reply.served_model,
            "text": text,
        }
        out.write(f"         {alignment.frame}: {len(text)} chars\n")

    if stopped:
        out.write(
            f"         stopped after {breaker.limit} consecutive connection failures; "
            "the remaining frames were not attempted\n"
        )
    out.write(
        f"         {len(produced)} captioned, {reused} from cache, {failed} failed, "
        f"{refused} refused\n"
    )
    return produced


@dataclass(frozen=True)
class CaptionOutcome:
    """What the captioning half actually did."""

    captioned: int
    reused: int
    failed: int
    model: str
    served_model: str | None
    #: Frames the vision model reported as carrying nothing beyond the excerpt. Counted out of
    #: `captioned`, not into `failed`. Read before trusting a note's coverage: a run with a high
    #: refusal share is a run whose screens were not read, whatever the exit code says.
    refused: int = 0

    @property
    def ok(self) -> bool:
        """False when nothing came back -- a run that captioned nothing did not caption.

        Distinct from `Report.ok`, which gates transcript coverage. Both matter and they
        fail for different reasons.
        """
        return self.captioned + self.reused > 0

    def as_dict(self) -> dict:
        return {
            "captioned": self.captioned,
            "reused": self.reused,
            "failed": self.failed,
            "refused": self.refused,
            "model": self.model,
            "served_model": self.served_model,
        }


def run(
    manifest: Path,
    transcript: Path,
    quality_report: Path,
    images: Path,
    destination: Path,
    *,
    settings: Settings | None = None,
    cache_source: Path | None = None,
    stream=None,
) -> tuple[Report, CaptionOutcome]:
    """Align, then caption.

    `cache_source` is a previous run's `captions.json` to reuse from, and it is **not**
    `destination`. The pipeline passes a scratch directory as `destination`, which is empty on
    every run, so reading the cache from it yields nothing and every frame is re-called. Run 10
    reported `0 from cache` immediately after run 9 produced the same 58 frames from the same
    bytes -- the cache exists in the bundle and was never opened. `source: "cached"` in
    `caption_trace` was unreachable in the real pipeline for the same reason `GATED_OUT` was
    (#82), and fixing the wiring is what makes that field mean anything.

    Two failure modes, handled differently on purpose, because the pipeline already has a
    contract about which one stops a run.

    **No vision endpoint at all** degrades, exactly as stage 7 does without a text endpoint.
    The pipeline is required to run end to end with no model configured --
    `TemplateSynthesizer` is what makes that true for the note, and stage 6 must not be the
    stage that breaks it. So every frame is marked `NO_ENDPOINT`, the run continues, and
    `outcome.ok` is False so the CLI reports the degradation and exits non-zero. A note
    written without slide content is a worse note, not no note.

    **An endpoint that is configured and refuses** is a dependency failure. That one is not
    swallowed: `caption_frames` records it per frame and `outcome.ok` stays False, so the
    CLI reports it as exit 3 rather than as a content verdict.

    What this never does is write `caption: null` into the artefact and exit 0, which put a
    string that reads like content where a caption belonged and let the run report itself
    clean.
    """
    settings = settings or Settings()
    out = stream if stream is not None else sys.stdout
    report = align(manifest, transcript, quality_report, images, settings)
    destination.mkdir(parents=True, exist_ok=True)

    target = destination / REPORT_NAME
    try:
        config = llm.Config.vision_from_env()
    except llm.NotConfiguredError as exc:
        config = llm.Config(endpoint="", model="")
        prior: dict[str, dict] = {}
        produced: dict[str, dict] = {}
        for alignment in report.considered:
            alignment.caption_status = "NO_ENDPOINT"
            alignment.reason = str(exc)
        outcome = CaptionOutcome(
            captioned=0,
            reused=0,
            failed=len(report.considered),
            model="",
            served_model=None,
        )
        out.write(f"         no vision endpoint: {exc}\n")
    else:
        prior = load_cache(cache_source or target, config.model)
        produced = caption_frames(report, images, config, cache=prior, stream=out)
        outcome = CaptionOutcome(
            captioned=len(produced),
            reused=sum(1 for a in report.considered if a.caption_status in DESCRIBED)
            - len(produced),
            failed=sum(1 for a in report.considered if a.caption_status not in DESCRIBED),
            refused=sum(1 for a in report.considered if a.caption_status == "NO_NEW_INFORMATION"),
            model=config.model,
            served_model=next(
                (e.get("served_model") for e in produced.values() if e.get("served_model")), None
            ),
        )

    write(report, target, settings, outcome, {**prior, **produced})
    (destination / PROVENANCE_NAME).write_text(
        provenance(settings, config, outcome) + "\n", encoding="utf-8"
    )
    if report.uncovered:
        for line in report.explain().splitlines():
            out.write(f"         {line.strip()}\n")
    if not outcome.ok:
        out.write(f"         no frame was captioned -- see {PROVENANCE_NAME}\n")
    return report, outcome


def write(
    report: Report,
    target: Path,
    settings: Settings,
    outcome: CaptionOutcome | None = None,
    captions: dict[str, dict] | None = None,
) -> None:
    target.write_text(
        json.dumps(
            {
                "provenance": {
                    "stage": 6,
                    "tool": "dyak-caption-v2",
                    "alignment": "segment overlap with word-timed boundary",
                    "caption": outcome.as_dict() if outcome else None,
                    "uncovered_share": round(report.uncovered_share, 4),
                },
                "captions": captions or {},
                "alignments": [a.as_dict() for a in report.alignments],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def provenance(settings: Settings, config: llm.Config, outcome: CaptionOutcome) -> str:
    return json.dumps(
        {
            "stage": 6,
            "tool": "dyak-caption-v2",
            "method": "segment overlap; boundary at first word >= frame display start",
            "requires_model": True,
            "caption": outcome.as_dict(),
            "model": config.redacted() | {"served_model": outcome.served_model},
            "inherited_text_endpoint": config.is_vision_default,
            "cache_key": "sha256(frame bytes) + model id",
            "empty_response_policy": {
                "retry_limit": 0,
                "description": "do not retry HTTP 200 empty completions; record each as EMPTY",
                "measurement": (
                    "42 fresh captions from 36 sampled lecture frames returned 0 empty completions. "
                    "Then 3 frames previously reported EMPTY were re-extracted at their exact PTS "
                    "and each returned NO_NEW_INFORMATION on both of 2 fresh calls. This supports "
                    "the model-refusal hypothesis for those three cases, but does not establish "
                    "how common empty completions are."
                ),
            },
            "ocr": "not attempted (no dependency; the vision endpoint reads the frame)",
            "uncovered_share_limit": settings.uncovered_share_limit,
            "max_chars": settings.max_chars,
        },
        indent=2,
    )
