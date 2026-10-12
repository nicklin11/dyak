"""Stage 3 — transcription.

ADR-0001 D2 (amended 2026-10-03): STT is delegated, never reimplemented, and it is delegated to
*an endpoint*, not to a particular utility. `TranscriptionBackend` is the boundary;
whisper.cpp native and an OpenAI-compatible server are the two implementations.

Everything below the backend is unchanged and shared:

- the parser is the same for every backend, because the payload is the same. whisper.cpp
  native `/inference` returns an object carrying `segments`; some servers return the bare
  array instead, and `openai_compat` unwraps either;
- `start`/`end` are **seconds as floats**, not milliseconds, in every one of them;
- per-word `word` fields are **BPE subword pieces** (`" Ин"`, `"ст"`, `"ит"`), carried
  verbatim and never reassembled — that needs whisper.cpp's exact vocabulary, and a wrong
  guess corrupts the transcript while leaving every timestamp plausible;
- the default `response_format=json` returns **only** `{"text": ...}`. Timings exist only
  under `verbose_json`.

**The word-coverage check is the reason this stage can be delegated safely.** Stages 5-7
bind frames to paragraphs by word span, so a backend that returns segments without words
does not degrade — it removes the mechanism the rest of the pipeline is built on. Coverage
is counted from the parsed result, never inferred from an exit code, and a transcript with
zero timed words is exit 3.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .. import exitcodes as ec
from .. import runner
from .backends import ENV_BACKEND, BackendInfo, TranscriptionBackend, resolve
from .core import (
    PROVENANCE_NAME,
    REMEDIATION,
    RATE_RANGE,
    TIMEOUT_MARGIN,
    TIMEOUT_PER_REALTIME,
    WARMUP_SECONDS,
    Segment,
    Transcript,
    Word,
    format_timestamp,
    parse,
    render_text,
    write,
)

__all__ = [
    "BackendInfo",
    "ENV_BACKEND",
    "PROVENANCE_NAME",
    "REMEDIATION",
    "RATE_RANGE",
    "Segment",
    "TIMEOUT_MARGIN",
    "TIMEOUT_PER_REALTIME",
    "TranscriptionBackend",
    "Transcript",
    "WARMUP_SECONDS",
    "Word",
    "format_timestamp",
    "parse",
    "render_text",
    "resolve",
    "transcribe",
    "write",
]


def backend(name: str | None = None) -> TranscriptionBackend:
    """The backend this run will use. Called by `doctor` and by the pipeline."""
    return resolve(name)


def transcribe(
    wav: Path,
    *,
    audio_duration: float,
    backend: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Transcript:
    """Transcribe `wav` through the selected backend and parse what came back.

    `on_progress` is forwarded to the engine unchanged: units the engine itself observed, never
    an estimate (#193). Only the OpenAI-compatible route has a unit today -- the chunk counter --
    and a backend with none simply never calls it.

    Raises runner.DependencyError with code 2 if the backend is not usable, 3 if it failed
    or returned nothing usable, and 1 if `backend` names something unknown.
    """
    engine = resolve(backend)
    engine.check()  # exit 2 before the caller has done any work on this stage's behalf
    timeout = audio_duration * TIMEOUT_PER_REALTIME + TIMEOUT_MARGIN
    raw = engine.run(wav, timeout=timeout, on_progress=on_progress)

    if not raw.strip():
        raise runner.DependencyError(
            name=engine.name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{engine.name} returned an empty body; no transcript was produced",
            remediation="check the backend directly; an empty body means it accepted the request and produced nothing",
        )

    segments, anomalies = parse(raw, source=engine.name)
    if not segments:
        raise runner.DependencyError(
            name=engine.name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{engine.name} returned an empty segments array",
            stdout=raw[:2000],
            remediation=(
                "a whisper.cpp build that ignores response_format=verbose_json returns no "
                "segments; check the server build"
            ),
        )

    transcript = Transcript(
        wav=Path(wav),
        audio_duration=audio_duration,
        segments=segments,
        anomalies=anomalies,
        raw_payload=raw,
        backend=engine.name,
        # Asked of the engine, not reconstructed here: a provenance document that re-derives
        # the parameters reports what the code would say today rather than what was sent.
        parameters=engine.parameters(client_timeout=timeout),
    )
    # ADR-0001 D3, and the reason the interface exists: segments without word timings are not the
    # payload this stage exists to produce.
    if transcript.timed_words == 0:
        raise runner.DependencyError(
            name=engine.name,
            code=ec.DEPENDENCY_FAILED,
            message=(
                f"{engine.name} returned {len(segments)} segments but zero words carrying "
                "timings; frame-to-paragraph binding needs per-word start/end"
            ),
            stdout=raw[:2000],
            remediation=(
                "ask the endpoint for verbose_json and timestamp_granularities[]=word; "
                "many OpenAI-compatible servers ignore the latter"
            ),
        )
    return transcript
