"""Backend for OpenAI-compatible `/v1/audio/transcriptions` endpoints.

For hosted and third-party servers. The wire format is similar to whisper.cpp's
`verbose_json` but **not identical**, and the differences are the reason this is a separate
adapter rather than a flag:

- the payload is an **object** with a `segments` array, not a bare array;
- with `response_format=json` there are **no timings at all** — only `{"text": ...}`;
- per-word timings come from `timestamp_granularities[]=word`, which the OpenAI API
  supports and many OpenAI-*compatible* servers silently ignore.

So this backend normalises the envelope and then hands the same segment list to the same
parser. It does not invent timings. If the server ignored the granularity request, the
segment count will be non-zero and the word count zero, and `transcribe()` fails loudly
rather than letting stages 5-7 fall back to guesswork.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .. import exitcodes as ec
from .. import runner
from ..settings import OPENAI_KEY_ENV as ENV_KEY
from ..settings import OPENAI_MODEL_ENV as ENV_MODEL
from ..settings import OPENAI_URL_ENV, resolve
from .backends import BackendInfo

REMEDIATION = (
    "set DYAK_OPENAI_URL (or the openai_url settings key) to an OpenAI-compatible "
    "/v1/audio/transcriptions endpoint"
)

CHUNK = 1 << 20


class OpenAICompat:
    """Transcribes over `POST /v1/audio/transcriptions`."""

    name = "openai"

    def __init__(self, base_url: str | None = None) -> None:
        # `base_url` is the flag layer; then `$DYAK_OPENAI_URL`, then the flat `openai_url`
        # key in the settings file. No built-in layer (#95): an unset URL is "not
        # configured", not a probe of the whisper.cpp container port.
        resolved = resolve(OPENAI_URL_ENV, flag=base_url)
        self.configured = resolved.value is not None
        self.base_url = (resolved.value or "").rstrip("/")
        self.model = resolve(ENV_MODEL, default="whisper-1").value
        self.key = resolve(ENV_KEY).value

    def available(self) -> bool:
        try:
            self.check()
        except runner.DependencyError:
            return False
        return True

    def info(self) -> BackendInfo:
        return BackendInfo(
            name=self.name,
            target=self.base_url or "(not configured)",
            detail=f"OpenAI-compatible /v1/audio/transcriptions, model={self.model}",
            notes=(
                "no API key configured" if not self.key else "API key present",
                "requesting timestamp_granularities[]=word; some compatible servers ignore it",
            ),
        )

    def check(self) -> None:
        if not self.configured:
            raise runner.DependencyError(
                name=self.name,
                code=ec.MISSING_DEPENDENCY,
                message="the OpenAI-compatible STT endpoint is not configured",
                remediation=REMEDIATION,
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise runner.DependencyError(
                name=self.name,
                code=ec.USAGE,
                message=f"DYAK_OPENAI_URL is not an http(s) URL: {self.base_url!r}",
                remediation=REMEDIATION,
            )
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            request = urllib.request.Request(
                f"{self.base_url}/v1/models", headers=headers, method="GET"
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                response.read(1)
        except urllib.error.HTTPError as exc:
            # 401/404 mean the server is there. A wrong key or a build without /v1/models
            # is not the same failure as "nothing is listening", and conflating them sends
            # the reader to debug the wrong thing.
            if exc.code in (401, 403, 404, 405):
                return
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"the endpoint answered GET /v1/models with HTTP {exc.code}",
                stderr=exc.read()[:2000],
                remediation=REMEDIATION,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"the endpoint is not reachable at {self.base_url}: {_reason(exc)}",
                remediation=REMEDIATION,
            ) from exc

    def run(self, wav: Path, *, timeout: float) -> bytes:
        boundary = f"dyak-{uuid.uuid4().hex}"
        body = self._multipart(boundary, Path(wav))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/audio/transcriptions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._check_json(response.read())
        except urllib.error.HTTPError as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"the endpoint returned HTTP {exc.code} for POST /v1/audio/transcriptions",
                stderr=exc.read()[:2000],
                remediation=REMEDIATION,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"POST /v1/audio/transcriptions failed: {_reason(exc)}",
                remediation=REMEDIATION,
            ) from exc

    def _check_json(self, raw: bytes) -> bytes:
        """Fail early on a non-JSON body; the envelope itself is handled by core.parse."""
        try:
            json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise runner.DependencyError(
                name=self.name,
                code=ec.DEPENDENCY_FAILED,
                message=f"the endpoint returned output that is not JSON ({exc})",
                stdout=raw[:2000],
                remediation="check that the URL points at an API, not an HTML page",
            ) from exc
        return raw

    def parameters(self, *, client_timeout: float) -> dict:
        """What this backend will send, for `stt-provenance.json`.

        The model id is the field that matters: two runs through two models produce two
        different transcripts from identical audio, and without it the bundle cannot say so.
        `client_timeout` is accepted for interface symmetry with whisper.cpp and is not part
        of the request -- the OpenAI route takes the timeout from the client.
        """
        return {
            "endpoint": f"{self.base_url}/v1/audio/transcriptions",
            "backend": self.name,
            "detail": f"OpenAI-compatible /v1/audio/transcriptions, model={self.model}",
            "fields": dict(self._fields()),
            "api_key": bool(self.key),
        }

    def _fields(self) -> tuple[tuple[str, str], ...]:
        return (
            ("model", self.model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
        )

    def _multipart(self, boundary: str, wav: Path) -> bytes:
        fields = self._fields()
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
