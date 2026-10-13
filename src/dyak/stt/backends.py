"""Stage 3 backends: one interface, several wire formats.

`TranscriptionBackend` returns **bytes**, not a `Transcript`. The bytes are parsed by
`core.parse`, which is shared, because the payload is the same in every backend:

- whisper.cpp native `POST /inference` returns a **top-level object** (`verbose_json`) with a
  `segments` array, each segment carrying a `words` list;
- an OpenAI-compatible server returns the same shape, *if* the server honours
  `timestamp_granularities`.

Keeping the interface at the byte level means a backend cannot quietly reshape timings on
the way in. Normalisation happens once, in one place, and is testable on its own.

Both remaining backends are HTTP endpoints. There is no subprocess backend: the one there was
added nothing this code did not already do (`core.parse` unwraps `segments` either way) and
imposed a 300 s ceiling it could not lift. See issue #49, which
also corrects an earlier claim that the two payloads were byte-identical. They are not:
whisper.cpp disagrees with **itself** run to run.

**Word timings are the reason the interface exists.** Stages 5-7 bind frames to paragraphs
by word span. A backend that returns segments without words does not degrade gracefully —
it removes the mechanism the rest of the pipeline is built on, while every segment still
looks plausible. So coverage is measured after parsing and reported, never inferred from
an exit code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .. import runner, settings
from ..settings import OPENAI_URL_ENV, WHISPERCPP_URL_ENV
from ..settings import STT_BACKEND_ENV as ENV_BACKEND


@dataclass(frozen=True)
class BackendInfo:
    """What `doctor` prints and what the stage report records."""

    name: str
    target: str
    detail: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Transcribe one 16 kHz mono wav. Return the raw payload; do not parse it."""

    name: str

    def info(self) -> BackendInfo:
        """Where this backend points, for `doctor` and the stage report."""
        ...

    def check(self) -> None:
        """Raise runner.DependencyError unless this backend is usable *now*.

        Must be able to distinguish "not configured" from "configured and unreachable".
        `shutil.which` cannot: it says a binary exists and nothing about its backend.
        """
        ...

    def run(
        self, wav, *, timeout: float, on_progress: Callable[[int, int], None] | None = None
    ) -> bytes:
        """Transcribe and return the raw payload.

        `on_progress` receives `(done, total)` in units this client has *observed*, never an
        estimate (#193): the only route with a unit today is the OpenAI-compatible one, where
        it is the count of wav slices uploaded (`openai_compat.py:240-267`). A backend with no
        unit never calls it, and the stage-3 start line says so instead of inventing a rate.
        """
        ...

    def parameters(self, *, client_timeout: float) -> dict:
        """What this backend will send, for the stage-3 provenance document.

        Required rather than optional: a backend that cannot describe itself produces a
        transcript whose provenance says nothing about how it was made, which is what stage 3
        did for its whole life (#80). Implementing it is one dict per backend.
        """
        ...


#: The remediation for every "no usable STT endpoint" failure (#259): one sentence, in one
#: place, so the two `auto` failures and every whisper.cpp failure cannot drift apart. It names
#: the variables to set and nothing about this host -- 100 chars.
ENDPOINT_REMEDIATION = (
    "set DYAK_WHISPERCPP_URL (or whispercpp_url) to a whisper.cpp /inference endpoint, "
    "or DYAK_OPENAI_URL"
)


def _endpoint_configured() -> bool:
    """Whether a flag/env/file layer supplied an STT URL at all (#95).

    `auto` has two failures that are both exit 2 but send the operator to different places:
    *nothing is configured*, and *something is configured but down*. The backend factories
    resolve the URL themselves; this asks the same resolver the same question the `auto`
    path can ask, so the two answers cannot diverge. Neither call passes a flag because
    `auto` constructs the backends with no argument -- there is no flag layer to reach.
    """
    return bool(
        settings.resolve(WHISPERCPP_URL_ENV).value or settings.resolve(OPENAI_URL_ENV).value
    )


def resolve(name: str | None = None) -> TranscriptionBackend:
    """Pick a backend. `DYAK_STT` wins; otherwise whisper.cpp native, then OpenAI.

    The selection now also honours the flat `stt` key in the settings file: the explicit
    argument is the flag layer, then the environment, then the file, then `auto`. Both
    candidates are HTTP endpoints. `auto` probes them in that order and takes the first
    that answers, because a local whisper.cpp keeps the audio on the machine and a configured
    OpenAI-compatible endpoint does not -- so the local one is preferred when both are up.

    **There is no built-in endpoint (#95).** An unset URL means "not configured", not
    "probe `127.0.0.1:10302`": that was a raw container port with no auto-start, and the
    idle timer reclaimed it after 300 s -- exactly the coincidence #62 measured. An
    unconfigured backend fails its own `check()` before any socket is opened, so `auto`
    never touches a port nobody started.
    """
    from . import openai_compat, whispercpp

    chosen = settings.resolve(ENV_BACKEND, flag=name, default="auto").value
    if chosen == "auto":
        for factory in (whispercpp.HttpWhisperCpp, openai_compat.OpenAICompat):
            backend = factory()
            if backend.available():
                return backend
        if not _endpoint_configured():
            raise runner.DependencyError(
                name="stt",
                code=2,
                message="the STT endpoint is not configured: no whisper.cpp or "
                "OpenAI-compatible URL is set in any layer",
                remediation=ENDPOINT_REMEDIATION,
            )
        raise runner.DependencyError(
            name="stt",
            code=2,
            message="no transcription backend is reachable; set DYAK_STT to one of "
            "whispercpp, openai",
            remediation=f"start whisper.cpp; {ENDPOINT_REMEDIATION}",
        )

    table: dict[str, type] = {
        "whispercpp": whispercpp.HttpWhisperCpp,
        "whisper.cpp": whispercpp.HttpWhisperCpp,
        "openai": openai_compat.OpenAICompat,
        "openai-compatible": openai_compat.OpenAICompat,
    }
    if chosen not in table:
        raise runner.DependencyError(
            name="stt",
            code=1,
            message=f"unknown STT backend {chosen!r}",
            remediation=f"DYAK_STT must be one of: {', '.join(sorted(table))}",
        )
    return table[chosen]()


__all__ = [
    "BackendInfo",
    "ENV_BACKEND",
    "TranscriptionBackend",
    "resolve",
]
