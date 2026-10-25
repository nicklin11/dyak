"""Stage 3 core: the normalised transcript types, the parser, and the writers.

ADR-0001 D2: STT is delegated, never reimplemented. The wav is posted to a whisper.cpp endpoint
and the raw `segments` payload comes back over HTTP. Two properties of that payload
drove the design, both measured on this host rather than assumed:

  * the default `response_format=json` returns **only** `{"text": ...}`. Segments
    exist solely under `verbose_json`, so there is nothing to read off the plain
    transcript. Timestamps are read from the `--timestamps json` form, never
    reparsed out of stdout text;
  * `start`/`end` are **seconds as floats**, not milliseconds. A parser that
    assumed ms would place every segment past the end of a 73-minute lecture.

**Word timings are the useful payload, and they are kept unassembled.** Per-word
`start`/`end` is what stages 5-7 need to bind a frame to a paragraph; segment
edges are decoder artefacts and will not line up with paragraph structure. But
the `word` field holds **BPE subword pieces**, not words -- a 25 s probe yields
`" Ин"`, `"ст"`, `"ит"`, `"ут"` for one word. Re-joining them needs the
tokenizer's vocabulary to be exactly the one whisper.cpp was built against, and a
wrong guess silently corrupts the transcript while leaving every timestamp
plausible. So the pieces are carried verbatim, and the human-readable transcript
is built from segment `text`, which whisper.cpp already detokenised.

Three artefacts are written, because three different consumers want different
things and collapsing them loses data we do not yet understand:

  * `transcript.raw.json` -- the payload exactly as the endpoint returned it
    (`tokens`, `t_dtw`, `avg_logprob` included), so nothing is discarded;
  * `transcript.json`     -- normalised: text stripped, timings coerced, plus
    run-level metadata. This is what later stages read;
  * `transcript.txt`      -- `[hh:mm:ss] text` per segment, for a human or an
    agent reading the lecture linearly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import exitcodes as ec
from .. import runner

PROVENANCE_NAME = "stt-provenance.json"

# Measured on this host, CPU whisper.cpp. The 257.2 s figure was long recorded as
# the 4520 s lecture's; ADR-0001's 2026-10-04 amendment shows it came from the
# shorter 4396 s .webm, and the 4520 s run measured 315.4 s:
#   25 s clip   ->   4.9 s wall  (0.20x realtime; server warm-up dominates here)
#   4396 s webm -> 257.2 s wall  (0.057x realtime)
#   4520 s mp4  -> 315.4 s wall  (0.070x realtime)
# The 25 s figure is warm-up, not throughput -- the two lecture-length runs measured
# 0.057x and 0.070x, so the estimate below is an interval wide enough to cover both.
#
# The timeout ceiling is 2x realtime plus a fixed margin: generous, because a
# timeout here discards a lecture-length run that has already been paid for,
# while an over-tight one would abort it.
TIMEOUT_PER_REALTIME = 2.0
TIMEOUT_MARGIN = 120.0

# Realtime multipliers behind the estimate the pipeline prints before stage 3
# blocks (ADR-0001 D7). A range, not a point estimate: the rate depends on model size and
# machine, and a precise-looking prediction that is wrong by 2x is worse than an
# honest interval.
RATE_RANGE = (0.05, 0.2)

# Below this much audio the whisper.cpp warm-up is a material share of the run:
# 25 s measured 0.20x, the 4396 s webm 0.057x and the 4520 s mp4 0.070x. Short
# inputs therefore get the slow end of RATE_RANGE as their floor, not the fast end.
WARMUP_SECONDS = 300.0


@dataclass(frozen=True)
class Word:
    """One BPE piece with its timing. `text` is verbatim from whisper.cpp."""

    text: str
    start: float
    end: float
    probability: float | None = None

    def to_dict(self) -> dict:
        out: dict = {"text": self.text, "start": self.start, "end": self.end}
        if self.probability is not None:
            out["probability"] = self.probability
        return out


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()
    avg_logprob: float | None = None
    no_speech_prob: float | None = None

    @property
    def timed_words(self) -> int:
        return sum(1 for w in self.words if w.start >= 0 and w.end >= w.start)

    def to_dict(self) -> dict:
        out: dict = {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }
        if self.avg_logprob is not None:
            out["avg_logprob"] = self.avg_logprob
        if self.no_speech_prob is not None:
            out["no_speech_prob"] = self.no_speech_prob
        return out


@dataclass(frozen=True)
class Transcript:
    wav: Path
    audio_duration: float
    segments: tuple[Segment, ...]
    #: Parser observations about this payload (a segment skipped for a missing text field, an
    #: end before a start, and so on). Recorded in `stt-provenance.json` and printed -- the
    #: first ten -- by stage 3, but deliberately never gated (#93). No measurement ties the
    #: count to note quality; a threshold would be a number without a denominator. #49 and #32
    #: are the measurements that could change this decision.
    anomalies: tuple[str, ...] = field(default_factory=tuple)
    raw_payload: bytes = b""
    backend: str = "unknown"
    #: What the backend was asked for, as resolved -- endpoint and every request field.
    #: Stage 3 published no provenance at all before this (#80): `transcript.json` recorded the
    #: results and nothing about what produced them, so the backend existed only in the run log
    #: and, incidentally, inside an anomaly string.
    parameters: dict = field(default_factory=dict)

    @property
    def speech_end(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    @property
    def coverage(self) -> float:
        if self.audio_duration <= 0:
            return 0.0
        return self.speech_end / self.audio_duration

    @property
    def timed_words(self) -> int:
        return sum(s.timed_words for s in self.segments)

    def summary(self) -> str:
        return (
            f"{len(self.segments)} segments, {self.timed_words} timed words, "
            f"speech to {self.speech_end:.1f}s of {self.audio_duration:.1f}s audio "
            f"[{self.backend}]"
        )


def segment_dicts(raw: bytes, *, source: str) -> tuple[list, tuple[str, ...]]:
    """The raw segment entries from either envelope, **unmodified**.

    The envelope is the only thing that differs between backends, and this is the one
    place that knows its shape -- shared by `parse` (which normalises the entries into
    `Segment`s) and the OpenAI-compatible chunk splice (which must offset the entries
    without discarding the raw fields `parse` does not keep, `t_dtw` among them).

    Two envelopes exist and the difference is not cosmetic. Measured against the live
    server: whisper.cpp `POST /inference` returns an **object**
    ({task, language, duration, text, segments, ...}); a bare **array** is what some
    servers and proxies return instead. The segment contents are the same
    (`words` with word/start/end/t_dtw/probability, float seconds); only the wrapper
    differs. Guessing one shape would have failed against the other.
    """
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise runner.DependencyError(
            name=source,
            code=ec.DEPENDENCY_FAILED,
            message=f"{source} returned output that is not JSON ({exc})",
            stdout=_truncated(raw),
            remediation="run the backend by hand to see what it prints",
        ) from exc

    anomalies: list[str] = []
    if isinstance(payload, dict):
        anomalies.append(f"payload arrived as an object with a 'segments' key ({source})")
        segments_raw = payload.get("segments")
        if segments_raw is None:
            # Timings were never returned. One segment carrying the whole text is honest
            # about what arrived; the word-coverage check in transcribe() then rejects it
            # with a specific message instead of stages 5-7 losing their binding silently.
            text = payload.get("text")
            segments_raw = [{"id": 0, "start": 0.0, "end": 0.0, "text": text or "", "words": []}]
        payload = segments_raw
    if not isinstance(payload, list):
        raise runner.DependencyError(
            name=source,
            code=ec.DEPENDENCY_FAILED,
            message=f"expected segments as a JSON array or an object, got {type(payload).__name__}",
            stdout=_truncated(raw),
            remediation=(
                "whisper.cpp verbose_json returns {..., 'segments': [...]}, or a bare "
                "[...]; check the backend build"
            ),
        )
    return payload, tuple(anomalies)


def parse(raw: bytes, *, source: str) -> tuple[tuple[Segment, ...], tuple[str, ...]]:
    payload, envelope_anomalies = segment_dicts(raw, source=source)

    segments: list[Segment] = []
    anomalies: list[str] = list(envelope_anomalies)
    previous_end = -1.0

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            anomalies.append(f"segment {index} is {type(item).__name__}, not an object; skipped")
            continue
        text = item.get("text")
        if not isinstance(text, str):
            anomalies.append(f"segment {index} has no text field; skipped")
            continue
        text = text.strip()
        if not text:
            continue

        start = _number(item.get("start"), 0.0)
        raw_end = item.get("end")
        end = _finite(raw_end)
        if end is None:
            # Clamping is necessary -- int(inf) in format_timestamp would raise
            # OverflowError after the whole transcription had been paid for --
            # but it is a silent edit to the transcript unless it is recorded.
            if raw_end is not None:
                anomalies.append(
                    f"segment {index} has a non-finite end ({raw_end!r}); clamped to its start"
                )
            end = start
        elif end < start:
            anomalies.append(f"segment {index} ends ({end:.2f}) before it starts ({start:.2f})")
            end = start
        if start < previous_end:
            anomalies.append(
                f"segment {index} starts at {start:.2f}s, before the previous segment "
                f"ended at {previous_end:.2f}s; decoder artefacts, kept as reported"
            )
        previous_end = end

        segments.append(
            Segment(
                index=len(segments),
                start=start,
                end=end,
                text=text,
                words=_words(item.get("words")),
                avg_logprob=_optional_float(item.get("avg_logprob")),
                no_speech_prob=_optional_float(item.get("no_speech_prob")),
            )
        )

    return tuple(segments), tuple(anomalies)


def _words(raw: object) -> tuple[Word, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[Word] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("word", item.get("text"))
        if not isinstance(text, str):
            continue
        out.append(
            Word(
                text=text,
                start=_number(item.get("start"), -1.0),
                end=_number(item.get("end"), -1.0),
                probability=_optional_float(item.get("probability")),
            )
        )
    return tuple(out)


def write(transcript: Transcript, workdir: Path) -> dict[str, Path]:
    """Write the three transcript artefacts into `workdir`."""
    raw_path = workdir / "transcript.raw.json"
    json_path = workdir / "transcript.json"
    txt_path = workdir / "transcript.txt"

    raw_path.write_bytes(transcript.raw_payload or b"[]")
    json_path.write_text(
        json.dumps(
            {
                "wav": str(transcript.wav),
                "audio_duration": transcript.audio_duration,
                "speech_end": transcript.speech_end,
                "coverage": round(transcript.coverage, 4),
                "segment_count": len(transcript.segments),
                "timed_words": transcript.timed_words,
                "anomalies": list(transcript.anomalies),
                "segments": [s.to_dict() for s in transcript.segments],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    txt_path.write_text(render_text(transcript), encoding="utf-8")
    prov_path = workdir / PROVENANCE_NAME
    prov_path.write_text(provenance(transcript, workdir) + "\n", encoding="utf-8")
    # Returned, not just written: `pipeline.run` publishes whatever this returns, so a file
    # written here and not named here never reaches the bundle.
    return {"raw": raw_path, "json": json_path, "txt": txt_path, "provenance": prov_path}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(transcript: Transcript, workdir: Path) -> str:
    """What produced this transcript, and a hash of what it produced.

    The hashes are the point. #49 measures whisper.cpp disagreeing **with itself** on the same
    audio, so two runs of one lecture can differ at the source. With a digest of `transcript.txt`
    in the bundle, that is a one-line check -- equal digests mean any difference downstream is
    the model's, unequal digests mean stage 3's, and the two files are sitting there to diff.
    Without one, the honest answer to "why did this run produce a different note" is a guess.
    """
    return json.dumps(
        {
            "stage": 3,
            "tool": "dyak-stt-v1",
            "requires_model": True,
            "backend": transcript.backend,
            "parameters": transcript.parameters,
            "results": {
                "segment_count": len(transcript.segments),
                "timed_words": transcript.timed_words,
                "audio_duration": round(transcript.audio_duration, 1),
                "speech_end": round(transcript.speech_end, 1),
                "coverage": round(transcript.coverage, 4),
                "anomalies": len(transcript.anomalies),
            },
            "digests": {
                name: _digest(workdir / filename)
                for name, filename in (
                    ("transcript.txt", "transcript.txt"),
                    ("raw", "transcript.raw.json"),
                )
            },
        },
        indent=2,
        ensure_ascii=False,
    )


def render_text(transcript: Transcript) -> str:
    lines = [f"# Transcript — {transcript.wav.name}", ""]
    for seg in transcript.segments:
        lines.append(f"[{format_timestamp(seg.start)}] {seg.text}")
    return "\n".join(lines) + "\n"


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _truncated(raw: bytes, limit: int = 2000) -> bytes:
    """First `limit` bytes, marked when anything was cut.

    An unmarked truncation reads as a complete message, which is the swallowed
    -error outcome ADR-0001 D2 exists to prevent -- and worse here, because these bytes
    came from stdout and `report()` would otherwise present them as the
    dependency's stderr.
    """
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"\n... [truncated, {len(raw)} bytes total]".encode()


def _finite(value: object) -> float | None:
    """Coerce to a finite float, or None.

    Both NaN and +/-inf must be rejected, not just NaN: Python's json accepts
    the bare literals `Infinity`/`-Infinity`, so `"end": 1e999` parses to inf.
    An inf `end` then reaches `format_timestamp` -> `int(inf)` -> OverflowError,
    *after* the whole transcription has already been paid for, and lands in
    transcript.json as a bare `Infinity` token, which is not valid JSON.
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _number(value: object, fallback: float) -> float:
    out = _finite(value)
    return fallback if out is None else out


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _finite(value)
