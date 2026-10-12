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

**A hosted endpoint has a per-upload ceiling; this machine does not.** 16 kHz mono 16-bit is
32 KiB/s, so a 4520 s lecture is ~140 MiB against the documented 25 MB limit (#125). The
route therefore slices the wav into chunks below a configured threshold
(`DYAK_OPENAI_MAX_UPLOAD_BYTES`, default 25 MiB) and splices their word timings.

What the splice does, exactly:

- the **sample bytes are copied verbatim** — no re-encode, no ffmpeg, no codec change. Each
  chunk is given a rebuilt RIFF header carrying that chunk's sizes, because a raw slice of a
  wav is not a wav; that is a container rewrite, not a re-encode, and #49 (whisper.cpp
  disagreeing with itself) is the reason a second encoder is not an option.
- chunks overlap, and each word is kept by exactly one chunk: the one whose
  `[start, next_start)` half-open range holds the word's start. A word near a chunk's end is
  therefore never taken from the decoder's truncated tail, and no word is duplicated or lost
  at the seam. Segment `text` can still repeat a few words across a seam — detokenised text
  cannot be cut without the tokenizer's vocabulary, which `core.py` refuses to guess at — but
  the timed-word stream stages 5-7 bind on cannot.
- the splice works on the raw dicts from `core.segment_dicts`, offsetting each segment and
  word by its chunk's start and re-serialising, so `transcript.raw.json` holds the merged
  payload with every other field the server sent (`t_dtw`, `probability`, `avg_logprob`).

This is also what removes the 140 MiB `bytes` object: each request body is one chunk's
multipart body, built from a bounded read of the wav. There is deliberately no second,
streaming request implementation on top — `_multipart` takes the chunk range instead of
reading the whole file.
"""

from __future__ import annotations

import json
import struct
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import exitcodes as ec
from .. import runner
from ..deps import PROBE_TIMEOUT
from ..settings import OPENAI_KEY_ENV as ENV_KEY
from ..settings import OPENAI_MAX_UPLOAD_BYTES_ENV as ENV_MAX_UPLOAD
from ..settings import OPENAI_MODEL_ENV as ENV_MODEL
from ..settings import OPENAI_URL_ENV, resolve
from . import core
from .backends import BackendInfo

REMEDIATION = (
    "set DYAK_OPENAI_URL (or the openai_url settings key) to an OpenAI-compatible "
    "/v1/audio/transcriptions endpoint"
)

CHUNK = 1 << 20

#: The documented hosted ceiling (OpenAI's 25 MB per audio upload), in bytes. A **setting**
#: rather than a literal at the call site, because the limit belongs to the endpoint: another
#: provider documents another number, and this repo cannot measure it.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: How far each chunk reaches back into the previous one. Overlap is what makes the seam
#: recoverable: without it the last word of a chunk is cut by the decoder and there is no
#: second reading of it. Ten seconds is several times a word and well under a decode window,
#: so a chunk start is never inside the previous chunk's keep range.
CHUNK_OVERLAP_SECONDS = 10.0

#: A chunk request's timeout is its share of the whole-audio budget, but a short chunk still
#: pays the fixed warm-up the budget's margin covers; this floor is that margin.
CHUNK_TIMEOUT_FLOOR = 120.0


@dataclass(frozen=True)
class _Pcm:
    """The PCM data range of a wav, plus what is needed to rebuild a per-chunk header."""

    fmt: bytes
    data_offset: int
    data_length: int
    block_align: int
    bytes_per_second: int

    @property
    def header_bytes(self) -> int:
        """Length of the RIFF header `_wav_header` writes for this `fmt` payload."""
        return 28 + len(self.fmt)


@dataclass(frozen=True)
class _Chunk:
    """One chunk's PCM range: `length` bytes starting at `offset` in the source wav."""

    offset: int
    length: int


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
        # Resolved, not parsed, here: a bad value must fail the run with a message naming
        # the setting, and `available()` treats any DependencyError as "unreachable", so
        # parsing in the constructor would turn a typo into autodetection's exit 2.
        self.max_upload_setting = resolve(ENV_MAX_UPLOAD, default=str(DEFAULT_MAX_UPLOAD_BYTES))
        #: Requests the last `run` made: 0 before one, 1 when the wav fitted, N when it was
        #: sliced. Provenance records it, so a chunked upload is legible after the fact.
        self.chunks = 0

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
            # The stage-0 probe budget, not a bare literal (#189): `deps.py:48`'s
            # `PROBE_TIMEOUT` is what `check_llm`/`check_vlm` spend, and a hosted endpoint
            # that is cold or slow to first byte is not "unreachable" inside that budget.
            # This is the probe only -- `run()` keeps the caller's transcription budget.
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
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
        wav = Path(wav)
        boundary = f"dyak-{uuid.uuid4().hex}"
        envelope, closing = self._envelope(boundary, wav.name)
        overhead = sum(len(part) for part in envelope) + len(closing)
        threshold = _threshold_bytes(self.max_upload_setting.value)

        if wav.stat().st_size <= threshold - overhead:
            # Under the limit: one request, the file verbatim — the body this route has
            # always sent, extra RIFF chunks and all. The layout is not even parsed, so a
            # wav this code cannot slice still uploads as long as it fits.
            self.chunks = 1
            return self._post(boundary, self._multipart(boundary, wav), timeout=timeout)

        layout = _wav_layout(wav)
        capacity = _align_down(threshold - overhead - layout.header_bytes, layout.block_align)
        if capacity <= 0:
            raise runner.DependencyError(
                name=self.name,
                code=ec.USAGE,
                message=(
                    f"{ENV_MAX_UPLOAD}={threshold} leaves no room for audio: the multipart "
                    f"envelope and a wav header need {overhead + layout.header_bytes} bytes"
                ),
                remediation="raise the limit to the endpoint's real per-upload ceiling",
            )
        overlap = _align_down(
            min(int(CHUNK_OVERLAP_SECONDS * layout.bytes_per_second), capacity // 2),
            layout.block_align,
        )
        chunks = _plan_chunks(layout.data_length, capacity, overlap)
        self.chunks = len(chunks)
        payloads = [
            self._post(
                boundary,
                self._multipart(
                    boundary,
                    wav,
                    # `chunk.offset` is data-relative -- that is what makes the time
                    # `offset / bytes_per_second` -- so the read seeks past the header.
                    offset=layout.data_offset + chunk.offset,
                    length=chunk.length,
                    header=_wav_header(layout.fmt, chunk.length),
                ),
                timeout=_chunk_timeout(timeout, chunk.length, layout.data_length),
            )
            for chunk in chunks
        ]
        return self._splice(payloads, chunks, layout.bytes_per_second)

    def _post(self, boundary: str, body: bytes, *, timeout: float) -> bytes:
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
        """What this backend sends, for `stt-provenance.json`.

        The model id is the field that matters: two runs through two models produce two
        different transcripts from identical audio, and without it the bundle cannot say so.
        `client_timeout` is accepted for interface symmetry with whisper.cpp and is not part
        of the request -- the OpenAI route takes the timeout from the client.

        The `chunking` block is what makes a sliced upload distinguishable from a single one
        after the fact (#125): `chunks` is 1 when the wav fitted in one request, N when it
        was sliced, and 0 before any run. `threshold_bytes` and `overlap_seconds` are the
        geometry the run used.
        """
        return {
            "endpoint": f"{self.base_url}/v1/audio/transcriptions",
            "backend": self.name,
            "detail": f"OpenAI-compatible /v1/audio/transcriptions, model={self.model}",
            "fields": dict(self._fields()),
            "api_key": bool(self.key),
            "chunking": {
                "threshold_bytes": _threshold_bytes(self.max_upload_setting.value),
                "overlap_seconds": CHUNK_OVERLAP_SECONDS,
                "chunks": self.chunks,
            },
        }

    def _fields(self) -> tuple[tuple[str, str], ...]:
        return (
            ("model", self.model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
        )

    def _envelope(self, boundary: str, filename: str) -> tuple[list[bytes], bytes]:
        """The parts that do not depend on the audio, and the closing delimiter.

        Factored out so `run` can measure the envelope before choosing a chunk size: the body
        bound is a claim about bytes, and an estimate that leaves out the boundary lines is
        not the bound it says it is.
        """
        parts: list[bytes] = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in self._fields()
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        )
        return parts, f"\r\n--{boundary}--\r\n".encode()

    def _multipart(
        self,
        boundary: str,
        wav: Path,
        *,
        offset: int = 0,
        length: int | None = None,
        header: bytes = b"",
    ) -> bytes:
        """One request body: the fields, then `header` and `length` PCM bytes from `offset`.

        With the defaults this reads the whole file verbatim. `length` is what keeps the body
        bounded: the 140 MiB single `bytes` object #125 measured is gone because a chunked
        call reads only its own range, in `CHUNK`-sized pieces. There is deliberately no
        second, streaming request implementation.
        """
        wav = Path(wav)
        parts, closing = self._envelope(boundary, wav.name)
        if header:
            parts.append(header)
        with wav.open("rb") as handle:
            handle.seek(offset)
            remaining = length
            while True:
                want = CHUNK if remaining is None else min(CHUNK, remaining)
                if want <= 0:
                    break
                piece = handle.read(want)
                if not piece:
                    break
                parts.append(piece)
                if remaining is not None:
                    remaining -= len(piece)
        parts.append(closing)
        return b"".join(parts)

    def _splice(self, payloads: list[bytes], chunks: list[_Chunk], bytes_per_second: int) -> bytes:
        """Merge per-chunk payloads into one, offsetting timings by each chunk's start.

        A word is kept by the chunk whose `[start, next_start)` range holds its start, so the
        overlapping regions are decoded twice but emitted once. Raw dicts are copied field by
        field rather than rebuilt from `Segment`s: `t_dtw` and the rest are part of
        `transcript.raw.json`, and a splice that dropped them would make a chunked run's raw
        artefact a different kind of document from a single-request one.
        """
        merged: list[dict] = []
        for index, (raw, chunk) in enumerate(zip(payloads, chunks)):
            items, _ = core.segment_dicts(raw, source=self.name)
            if items and not any(_timed(item) for item in items):
                # A server that ignored timestamp_granularities answers this way for one
                # chunk. The whole-result check in transcribe() would not catch it while
                # another chunk has words, so the loss would be silent.
                raise runner.DependencyError(
                    name=self.name,
                    code=ec.DEPENDENCY_FAILED,
                    message=(
                        f"chunk {index + 1} of {len(chunks)} returned {len(items)} segments but "
                        "zero words carrying timings; frame-to-paragraph binding needs "
                        "per-word start/end"
                    ),
                    stdout=raw[:2000],
                    remediation=(
                        "ask the endpoint for verbose_json and "
                        "timestamp_granularities[]=word; many OpenAI-compatible servers ignore "
                        "the latter"
                    ),
                )
            start = chunk.offset / bytes_per_second
            limit = chunks[index + 1].offset / bytes_per_second if index + 1 < len(chunks) else None
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                segment_start = _shifted(item.get("start"), start)
                if limit is not None and segment_start is not None and segment_start >= limit:
                    continue  # the next chunk decodes this segment too, and it owns it
                segment = dict(item)
                # A segment with no numeric start still belongs to this chunk: the parser's
                # 0.0 fallback would otherwise place it at the start of the lecture.
                segment["start"] = start if segment_start is None else segment_start
                segment["end"] = _coerced(item.get("end"), _shifted(item.get("end"), start))
                words: list[dict] = []
                for word in item.get("words") or []:
                    if not isinstance(word, dict):
                        continue
                    word_start = _shifted(word.get("start"), start)
                    if limit is not None and word_start is not None and word_start >= limit:
                        continue
                    shifted = dict(word)
                    shifted["start"] = word.get("start") if word_start is None else word_start
                    shifted["end"] = _coerced(word.get("end"), _shifted(word.get("end"), start))
                    words.append(shifted)
                if item.get("words") and not words:
                    continue  # every word of this segment lies past the keep range
                segment["words"] = words
                segment["index"] = len(merged)
                merged.append(segment)
        text = " ".join(str(item.get("text", "")).strip() for item in merged).strip()
        return json.dumps({"text": text, "segments": merged}, ensure_ascii=False).encode()


def _threshold_bytes(raw: object) -> int:
    """The configured per-upload ceiling in bytes; exit 1 if it is not a positive count.

    Bytes, not a unit-suffixed string: `25M` would have to decide between 25,000,000 and
    26,214,400, and a wrong guess is a request that exceeds the endpoint's limit -- the exact
    failure this setting exists to prevent.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise runner.DependencyError(
            name="openai",
            code=ec.USAGE,
            message=f"{ENV_MAX_UPLOAD} is not a positive byte count: {raw!r}",
            remediation=(
                f"set it to the endpoint's per-upload limit in bytes "
                f"(default {DEFAULT_MAX_UPLOAD_BYTES} = 25 MiB)"
            ),
        )
    return value


def _align_down(value: int, alignment: int) -> int:
    """`value` rounded down to a whole number of `alignment`-byte units."""
    return value - (value % alignment)


def _plan_chunks(data_length: int, capacity: int, overlap: int) -> list[_Chunk]:
    """Chunk ranges covering `data_length` bytes, each at most `capacity`, overlapping.

    `overlap <= capacity // 2`, so the stride is positive and the loop terminates; the last
    chunk is short and the first starts at 0.
    """
    stride = capacity - overlap
    chunks: list[_Chunk] = []
    offset = 0
    while True:
        length = min(capacity, data_length - offset)
        chunks.append(_Chunk(offset, length))
        if offset + length >= data_length:
            return chunks
        offset += stride


def _chunk_timeout(timeout: float, length: int, total: int) -> float:
    """A chunk's share of the whole-audio budget, never below the warm-up margin."""
    if total <= 0:
        return timeout
    return max(CHUNK_TIMEOUT_FLOOR, timeout * length / total)


def _wav_header(fmt: bytes, data_size: int) -> bytes:
    """A canonical RIFF/WAVE header for `data_size` bytes of PCM described by `fmt`.

    The sample bytes are untouched; only the two size fields change, so each chunk is a
    self-contained wav the endpoint can decode.
    """
    return (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + len(fmt) + 8 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", data_size)
    )


def _wav_layout(path: Path) -> _Pcm:
    """Locate the `fmt ` and `data` chunks of a PCM wav.

    Walks the RIFF chunk list rather than assuming the 44-byte canonical header: a wav from
    another tool can carry a `LIST` chunk before `data`, and slicing at a fixed offset would
    then feed the endpoint metadata bytes as audio. Only reached when the file must be
    sliced; a wav under the threshold is sent verbatim, header and all.
    """
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise _not_sliceable(f"{path.name} is not a RIFF/WAVE file")
        fmt: bytes | None = None
        offset = 12
        while offset + 8 <= file_size:
            handle.seek(offset)
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
            body = offset + 8
            if chunk_id == b"fmt ":
                fmt = handle.read(min(chunk_size, 64))
            elif chunk_id == b"data":
                declared = chunk_size
                if declared in (0, 0xFFFFFFFF) or body + declared > file_size:
                    declared = file_size - body
                if fmt is None or len(fmt) < 16:
                    raise _not_sliceable(f"{path.name} has a data chunk before its fmt chunk")
                audio_format, _channels, sample_rate, byte_rate, block_align, _bits = struct.unpack(
                    "<HHIIHH", fmt[:16]
                )
                if audio_format != 1 or block_align <= 0:
                    raise _not_sliceable(
                        f"{path.name} is not PCM (format {audio_format}, block align {block_align})"
                    )
                return _Pcm(
                    fmt=fmt,
                    data_offset=body,
                    data_length=declared,
                    block_align=block_align,
                    bytes_per_second=byte_rate or (sample_rate * block_align),
                )
            offset = body + chunk_size + (chunk_size & 1)
    raise _not_sliceable(f"{path.name} has no PCM data chunk")


def _not_sliceable(what: str) -> runner.DependencyError:
    """The error for a wav above the threshold that this route cannot slice."""
    return runner.DependencyError(
        name="openai",
        code=ec.DEPENDENCY_FAILED,
        message=(
            f"{what}; the wav is above the upload threshold and cannot be sliced. The hosted "
            "route copies the sample bytes as-is and only rebuilds the RIFF header, so it "
            "needs a PCM wav"
        ),
        remediation="re-extract the audio (stage 2 writes a 16 kHz mono PCM wav)",
    )


def _shifted(value: object, delta: float) -> float | None:
    """`value + delta` when `value` is a number, else None so the caller leaves it alone."""
    try:
        return float(value) + delta  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerced(original: object, shifted: float | None) -> object:
    """The shifted value, or the original when it was not a number to shift."""
    return original if shifted is None else shifted


def _timed(item: object) -> bool:
    """Whether one raw segment carries at least one word with usable timings.

    The raw-dict form of `Segment.timed_words`, for the per-chunk check that must run before
    the dicts become `Segment`s.
    """
    if not isinstance(item, dict):
        return False
    for word in item.get("words") or []:
        if not isinstance(word, dict):
            continue
        start = _shifted(word.get("start"), 0.0)
        end = _shifted(word.get("end"), 0.0)
        if start is not None and end is not None and start >= 0 and end >= start:
            return True
    return False


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "reason", None) or exc)
