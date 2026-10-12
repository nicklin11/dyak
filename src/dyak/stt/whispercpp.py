"""HTTP backend for whisper.cpp's native `POST /inference`.

This is the protocol the server on this host actually speaks. Measured:

    GET  /health                    -> 200
    GET  /v1/models                 -> 404
    POST /v1/audio/transcriptions   -> 404
    POST /inference                 -> 200

The OpenAI-compatible route is not merely discouraged here, it does not exist — so a
backend written only against `/v1/audio/transcriptions` cannot run on this machine at all.

`response_format=verbose_json` is what makes timings exist. The default `json` returns
only `{"text": ...}`; there is nothing to read timings off it.

**This endpoint is not reproducible run to run.** Measured, 25 s clip, three runs each:

| request fields | identical 3x | segment counts |
|---|---|---|
| `language` + `verbose_json` | no | 11, 11, 11 |
| `temperature` + `no_timestamps` | no | 11, 11, 11 |
| plus `threads=1` | no | 10, 11, 11 |
| `threads=2` / `threads=4` | no | 11, 11, 11 / 11, 11, 10 |
| after a fresh container restart | no | 11, 10, 11 |

Differences are punctuation and a segment that appears or does not
("то есть не профессор каприбальский состав" / "кабрибательский"), on identical timings.
Ruled out: thread count down to 1, language on or off, temperature on or off, container
state. Cause not identified. Consequence: `transcript.json` is **not** a reproducible
artefact, and a regression check cannot compare transcripts byte-wise — see #20.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .. import exitcodes as ec
from .. import runner
from ..deps import PROBE_TIMEOUT
from ..settings import WHISPERCPP_LANGUAGE_ENV as ENV_LANGUAGE
from ..settings import WHISPERCPP_URL_ENV, resolve
from .backends import BackendInfo

# The trap this names is the one #62 measured and #95 decided: `127.0.0.1:10302` is the
# container directly, with no `_Heartbeat` to refresh the idle marker, so
# `whisper-idle-stop.timer` reclaims it after 300 s -- mid-request. The supported target is
# the shipboard proxy, which does refresh the marker. This is a remediation string, not an
# essay: the reader is mid-failure and needs the address and the reason in one line.
REMEDIATION = (
    "set DYAK_WHISPERCPP_URL (or the whispercpp_url settings key) to a whisper.cpp "
    "POST /inference endpoint. The supported target on this host is the shipboard proxy on "
    "http://100.64.0.1:10301, which refreshes the idle marker; a raw container port such as "
    "http://127.0.0.1:10302 has no auto-start and is reclaimed after 300 s by "
    "whisper-idle-stop.timer"
)

# The server-side guard must outlast the client's, because a client timeout discards a
# lecture-length run that has already been paid for. The pipeline computes the client
# budget from the audio duration; the backend only needs it to be later than that.
SERVER_TIMEOUT_FACTOR = 1.5
SERVER_TIMEOUT_MARGIN = 120.0

CHUNK = 1 << 20


class HttpWhisperCpp:
    """Transcribes over `POST /inference`."""

    name = "whispercpp"

    def __init__(self, base_url: str | None = None) -> None:
        # `base_url` is the flag layer; then `$DYAK_WHISPERCPP_URL`, then the flat
        # `whispercpp_url` key in the settings file. There is deliberately no fourth,
        # built-in layer (#95): an unset URL is "not configured", not a raw container port.
        resolved = resolve(WHISPERCPP_URL_ENV, flag=base_url)
        self.configured = resolved.value is not None
        self.base_url = (resolved.value or "").rstrip("/")

    # -- introspection --------------------------------------------------------

    def available(self) -> bool:
        """Whether /inference answers. Never raises; used for autodetection."""
        try:
            self.check()
        except runner.DependencyError:
            return False
        return True

    def info(self) -> BackendInfo:
        return BackendInfo(
            name=self.name,
            target=self.base_url or "(not configured)",
            detail="whisper.cpp native /inference",
            notes=("this build does not serve the OpenAI-compatible /v1/* routes",),
        )

    def check(self) -> None:
        if not self.configured:
            # Distinct from "configured and unreachable" on purpose: the two send the
            # operator to different places, and this one must not read as a refused
            # connection to a port nobody started (#95).
            raise runner.DependencyError(
                name=self.name,
                code=ec.MISSING_DEPENDENCY,
                message="the whisper.cpp endpoint is not configured",
                remediation=REMEDIATION,
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise runner.DependencyError(
                name=self.name,
                code=ec.USAGE,
                message=f"DYAK_WHISPERCPP_URL is not an http(s) URL: {self.base_url!r}",
                remediation=REMEDIATION,
            )
        try:
            request = urllib.request.Request(f"{self.base_url}/health", method="GET")
            # The stage-0 probe budget, not a bare literal (#189). The endpoint #95 names as
            # supported -- the shipboard proxy -- is wake-on-first-request, so the first
            # `GET /health` against a reclaimed container is spent waiting for the wake: a
            # 5.0 s literal here reported a working endpoint as unreachable and `_preflight`
            # turned that into a refusal. `PROBE_TIMEOUT` is the budget the sibling `llm`
            # (`deps.py:360`) and `vlm` probes already spend, so the three cannot disagree.
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
                response.read(1)
        except urllib.error.HTTPError:
            # Some builds serve /inference without /health. Not fatal.
            return
        except (urllib.error.URLError, OSError) as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"whisper.cpp is not reachable at {self.base_url}: {_reason(exc)}",
                remediation=REMEDIATION,
            ) from exc

    # -- transcription ---------------------------------------------------------

    def run(self, wav: Path, *, timeout: float) -> bytes:
        boundary = f"dyak-{uuid.uuid4().hex}"
        body = self._multipart(boundary, Path(wav), timeout)
        request = urllib.request.Request(
            f"{self.base_url}/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"whisper.cpp returned HTTP {exc.code} for POST /inference",
                stderr=exc.read()[:2000],
                remediation=REMEDIATION,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"POST /inference failed: {_reason(exc)}",
                remediation=REMEDIATION,
            ) from exc

    def _fields(self, client_timeout: float) -> list[tuple[str, str]]:
        """The request fields, exactly as they will be sent.

        Factored out of `_multipart` so `parameters()` and the request cannot disagree. A
        provenance document that reconstructs the parameters instead of asking for them is
        worth less than none: it reports what the code *would* say today, not what was sent.
        """
        server_timeout = client_timeout * SERVER_TIMEOUT_FACTOR + SERVER_TIMEOUT_MARGIN
        fields = [
            # Greedy decoding. Not a determinism fix -- measured above, nothing is -- but
            # it is the right request regardless: the default sampling path is a worse
            # transcript at the same cost.
            ("temperature", "0.0"),
            # verbose_json is what makes timings exist at all; the default json format
            # returns only {"text": ...}.
            ("response_format", "verbose_json"),
            # Server-side ceiling, so a slow client is not cut off mid-lecture while the
            # server keeps working.
            ("timeout", str(int(server_timeout))),
        ]
        # Language is optional and unset by default: whisper.cpp auto-detects, and pinning
        # it would silently mis-transcribe any lecture in another language.
        language = resolve(ENV_LANGUAGE).value or ""
        if language:
            fields.append(("language", language))
        return fields

    def parameters(self, *, client_timeout: float) -> dict:
        """What this backend is about to send, for `stt-provenance.json`.

        The language pin matters and is invisible elsewhere: unset means whisper.cpp
        auto-detects, and pinning it changes the transcript without changing anything a user
        would see in the run log. #49 measures whisper.cpp disagreeing with itself on the same
        audio, which is the situation these parameters exist to make diagnosable.
        """
        return {
            "endpoint": f"{self.base_url}/inference",
            "backend": self.name,
            "detail": "whisper.cpp native /inference",
            "fields": dict(self._fields(client_timeout)),
        }

    def _multipart(self, boundary: str, wav: Path, client_timeout: float) -> bytes:
        """Build the body by hand: the stdlib has no multipart encoder, and a wav is 137 MiB."""
        fields = self._fields(client_timeout)
        parts: list[bytes] = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{wav.name}"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        )
        with wav.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                parts.append(chunk)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return b"".join(parts)


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "reason", None) or exc)
