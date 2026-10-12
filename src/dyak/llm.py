"""An OpenAI-compatible chat client, with nothing else in it.

No SDK. `urllib` and `json` cover the whole surface -- POST to `/chat/completions`, read
`choices[0].message.content` -- and an SDK would add a dependency whose failure modes nobody
here has measured, for one endpoint shape. It is also what makes the provenance chain in
ADR-0004 D4
possible: every call records the exact bytes sent and received, which is not something a
provider library hands you.

## The model id

A reply carries **two** ids and they are not the same question.

`Reply.model` is what was *requested* -- the id in `Config`, which is what a reader needs in
order to reproduce the call. `Reply.served_model` is what the endpoint *reported*, or `None`
when it reported nothing.

They can disagree, and on this host's gateway they always do: the gateway routes by provider
prefix and answers with the upstream's name instead, so `opencode-go/glm-5.3-flash` returns as
`glm-5.3-flash`. With `force-model-prefix: true` the reported id is not routable at all --
replaying it returns `400 unknown provider`. Writing that into the artefact would make the
provenance chain record a call that cannot be repeated, so the reported id is recorded beside
the requested one and the two are left to disagree in the open.

## What it does not do

- **Retries on 4xx.** A 400 means the request is wrong; repeating it identically just burns
  the lecture's time budget. 429 and 5xx are retried, because those are transient by
  definition.
- **Stream.** ADR-0001 D7 forbids it: stage 7's progress cannot be estimated from a token count, and
  a partial note is not a note.
- **Guess at a missing key.** The endpoint may be keyless (a local llama-swap, the tailnet
  gateway) or may require one. Unset means send no `Authorization` header, which both of
  those accept. Guessing a placeholder key would produce a 401 that looks like an auth
  problem when it is a config one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .settings import LLM_ENDPOINT_ENV as ENDPOINT_ENV
from .settings import LLM_KEY_ENV as KEY_ENV
from .settings import LLM_MODEL_ENV as MODEL_ENV
from .settings import VLM_ENDPOINT_ENV as VISION_ENDPOINT_ENV
from .settings import VLM_KEY_ENV as VISION_KEY_ENV
from .settings import VLM_MODEL_ENV as VISION_MODEL_ENV
from .settings import resolve

#: The vision endpoint has its own names, per ADR-0004 D3: the two roles are separate
#: sections that may point at the same URL, and switching one later must cost one line.
#: Longest edge sent to the vision endpoint. Stage 4 extracts at 1600 px; a model that
#: resizes internally sees the same content either way, and the request body is 4/3 the
#: base64 size, so sending more than the frame costs bytes and buys nothing.
VISION_MAX_EDGE = 1600

#: Retries apply to 429 and 5xx only. A 4xx is a request the endpoint has already rejected on
#: its merits.
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RETRY_SLEEP = 2.0
RETRY_MAX = 3

#: Longest `Retry-After` this client will honour. The header is the endpoint's own opinion of
#: when to come back; an hour-long hint would hang the run as surely as no breaker at all.
RETRY_AFTER_CAP = 60.0

#: Consecutive connection-level failures after which one stage stops issuing calls (#124).
BREAKER_MAX = 3


class NotConfiguredError(RuntimeError):
    """No endpoint, or an endpoint with no model id. Raised before any network call."""


class EndpointError(RuntimeError):
    """The endpoint was reached and refused. Carries the status and the body it sent back."""

    def __init__(self, message: str, *, connection: bool = False) -> None:
        super().__init__(message)
        #: True when the call never got a response -- refused, DNS, timeout. A stage breaker
        #: counts these and not HTTP verdicts: a 400 repeated 58 times is a bad request, a
        #: refused connection repeated 58 times is a dead endpoint (#124).
        self.connection = connection


@dataclass(frozen=True)
class Config:
    endpoint: str
    model: str
    api_key: str | None = None
    timeout: float = 600.0
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> Config:
        endpoint = resolve(ENDPOINT_ENV).value or ""
        model = resolve(MODEL_ENV).value or ""
        if not endpoint:
            raise NotConfiguredError(f"set {ENDPOINT_ENV} to enable synthesis, audit and repair")
        if not model:
            raise NotConfiguredError(f"{ENDPOINT_ENV} is set but {MODEL_ENV} is not")
        key = resolve(KEY_ENV).value
        return cls(endpoint=endpoint.rstrip("/"), model=model, api_key=key)

    @property
    def url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    def redacted(self) -> dict:
        """Config safe to write into a bundle. The key never is."""
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key": bool(self.api_key),
            "temperature": self.temperature,
        }

    @classmethod
    def vision_from_env(cls) -> Config:
        """Config for the vision endpoint.

        Falls back to the text endpoint when the vision names are unset. They are separate
        names because they are separate roles, not because they have to be separate places
        -- on this host both are the same gateway, and requiring both sets to be exported
        would make stage 6 unrunnable for anyone who configured only the text side.
        """
        endpoint = resolve(VISION_ENDPOINT_ENV).value or ""
        model = resolve(VISION_MODEL_ENV).value or ""
        key = resolve(VISION_KEY_ENV).value
        if not endpoint and not model:
            return cls.from_env()
        if not endpoint:
            raise NotConfiguredError(f"{VISION_MODEL_ENV} is set but {VISION_ENDPOINT_ENV} is not")
        if not model:
            raise NotConfiguredError(f"{VISION_ENDPOINT_ENV} is set but {VISION_MODEL_ENV} is not")
        return cls(
            endpoint=endpoint.rstrip("/"),
            model=model,
            api_key=key or resolve(KEY_ENV).value,
        )

    @property
    def is_vision_default(self) -> bool:
        """True when this config came from the text names rather than the vision ones.

        Recorded in provenance. A reader comparing two bundles needs to know whether the
        vision stage was pointed somewhere deliberately or inherited.
        """
        return resolve(VISION_ENDPOINT_ENV).value is None


@dataclass(frozen=True)
class Reply:
    text: str
    #: What was requested -- the id that can be replayed against this endpoint.
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    attempts: int
    #: What the endpoint said it served, or None when it said nothing. Recorded beside `model`
    #: rather than substituted for it; see the module docstring.
    served_model: str | None = None
    #: Why the model stopped writing: `stop` (natural), `length` (the output cap), or None when
    #: the endpoint said nothing. Without it a truncated section is indistinguishable from a
    #: finished one in the transcript and in the note (#124).
    finish_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "served_model": self.served_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 3),
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
        }

    @property
    def truncated(self) -> bool:
        """True when the endpoint cut the model off at its output cap."""
        return self.finish_reason == "length"


#: Media types a frame may legitimately arrive as. Anything else is refused rather than
#: guessed at: a mislabelled data URI is a 400 whose message says nothing useful.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def image_data_uri(path: Path) -> str:
    """A frame as a `data:` URI, ready for an OpenAI-compatible `image_url` part.

    Inlined rather than uploaded or linked: the endpoint is on the tailnet, the frames are
    already local, and a URL the endpoint can fetch would be a second failure mode that
    only appears when the two machines disagree about reachability.
    """
    guessed = ("image/" + path.suffix.lstrip(".").lower()).replace("jpg", "jpeg")
    if guessed not in IMAGE_TYPES:
        guessed = "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{guessed};base64,{payload}"


def vision_message(prompt: str, image: Path, *, text: str = "") -> dict:
    """One user message carrying a prompt and one image.

    The transport is unchanged -- `chat()` passes `messages` through without inspecting
    them -- so vision is a message shape, not a second client.
    """
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"{prompt}\n\n{text}".strip() if text else prompt},
            {"type": "image_url", "image_url": {"url": image_data_uri(image)}},
        ],
    }


def frame_fingerprint(path: Path) -> str:
    """Content hash of a frame.

    Part of the stage-6 cache key alongside the model id, per ADR-0001 D4: the same frame
    bytes and the same model must give the same caption, and a changed model must not be
    served from a cache the previous model filled.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def chat(
    messages: list[dict],
    config: Config,
    *,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
) -> Reply:
    """One non-streaming completion. Raises `NotConfiguredError` or `EndpointError`."""
    payload: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if stop:
        payload["stop"] = stop

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    if config.api_key:
        request.add_header("Authorization", f"Bearer {config.api_key}")

    began = time.monotonic()
    last: EndpointError | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _to_reply(data, config.model, time.monotonic() - began, attempt)
        except urllib.error.HTTPError as exc:
            detail = _body(exc)
            last = EndpointError(f"HTTP {exc.code} from {config.url}: {detail}")
            if exc.code not in RETRY_STATUS:
                raise last from exc
            if attempt < RETRY_MAX:
                # The endpoint's own hint beats this client's ladder: a 429 that says "30 s"
                # and is retried after 2 s is a request the endpoint has already refused (#124).
                time.sleep(_retry_after(exc) or RETRY_SLEEP * attempt)
        except urllib.error.URLError as exc:
            # Connection refused, DNS failure, timeout. All transient as far as this code can
            # tell, and all indistinguishable to it -- so they are retried and then reported.
            last = EndpointError(f"{config.url} unreachable: {exc.reason}", connection=True)
            if attempt < RETRY_MAX:
                time.sleep(RETRY_SLEEP * attempt)
        except TimeoutError as exc:
            # **Not** a URLError, and this killed run 10. `urlopen` wraps connection failures
            # in URLError, but a timeout while the endpoint is still sending *headers* is
            # raised from `http.client.getresponse()` -- outside the block `urlopen` guards --
            # so it arrives as a bare `TimeoutError` and escaped every handler above.
            #
            # The symptom was a Python traceback and a dead run, not exit 3. An endpoint that
            # is installed and hanging is exactly what exit 3 means, so the contract the README
            # states was broken by the one failure mode nobody writes a mock for.
            last = EndpointError(
                f"{config.url} timed out after {config.timeout:g}s: {exc}", connection=True
            )
            if attempt < RETRY_MAX:
                time.sleep(RETRY_SLEEP * attempt)
        except json.JSONDecodeError as exc:
            # A body that is not JSON is a proxy or an error page, not a refusal. Retrying
            # cannot help and retrying a 500-shaped HTML page three times is worse.
            raise EndpointError(f"{config.url} returned non-JSON: {exc}") from exc
    raise last if last else EndpointError("no attempt was made")


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """The endpoint's own `Retry-After`, in seconds, or None when it gave none.

    Only the delta-seconds form is honoured. The HTTP-date form needs the endpoint and this
    host to agree on a clock, and a skewed `Date` header would turn a 30 s hint into a
    multi-hour sleep; the static ladder is the safer answer to a form this client cannot
    verify. Capped at `RETRY_AFTER_CAP` for the same reason.
    """
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP)


def _body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:
        return "<unreadable>"


def _to_reply(data: dict, model: str, seconds: float, attempts: int) -> Reply:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise EndpointError(
            f"response has no choices[0].message.content; got keys {sorted(data)}"
        ) from exc
    usage = data.get("usage") or {}
    echoed = data.get("model")
    reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    return Reply(
        text=text.strip(),
        # The requested id, not the reported one. A gateway that routes by provider prefix
        # reports the *upstream's* name, which the endpoint itself cannot route: measured on
        # CLIProxyAPI, `opencode-go/glm-5.3-flash` comes back as `glm-5.3-flash`, and with
        # `force-model-prefix: true` replaying that id is a 400. Substituting it would put an
        # unreplayable id in the artefact that is supposed to record what was asked for, so
        # both are kept and the disagreement stays visible instead of being resolved silently.
        model=model,
        served_model=str(echoed) if echoed else None,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        seconds=seconds,
        attempts=attempts,
        finish_reason=str(reason) if reason else None,
    )


@dataclass
class Transcript:
    """Every call made, for the provenance chain.

    Held in memory and written once. ADR-0004 D4 requires that a note can be traced to the exact
    model output it came from, and the cheapest way to guarantee that is to record it at the
    point of the call rather than reconstructing it later.
    """

    calls: list[dict] = field(default_factory=list)

    def record(self, stage: str, purpose: str, messages: list[dict], reply: Reply) -> None:
        self.calls.append(
            {
                "stage": stage,
                "purpose": purpose,
                "messages": messages,
                **reply.as_dict(),
            }
        )

    def as_dict(self) -> dict:
        return {"calls": self.calls}

    def write(self, target: Path) -> None:
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


class Breaker:
    """Counts consecutive connection-level failures so one stage can stop early (#124).

    The retry ladder is per-call on purpose: a single call should survive a blip. That makes
    58 identical refusals 58 full ladders -- about 5.8 min of sleeping against a fast-refusing
    endpoint before "0 captioned", and 30 min per call against a hung one. So the count is
    per-stage, and only connection-level failures count: a 400 repeated is a bad request, a
    refused connection repeated is a dead endpoint. Any answer at all resets it.
    """

    def __init__(self, limit: int = BREAKER_MAX) -> None:
        self.limit = limit
        self.consecutive = 0

    def ok(self) -> None:
        """A call that came back at all, even a refusal the endpoint had an opinion about."""
        self.consecutive = 0

    def fail(self) -> bool:
        """Record a connection-level failure. True once the stage must stop calling."""
        self.consecutive += 1
        return self.consecutive >= self.limit

    @property
    def tripped(self) -> bool:
        return self.consecutive >= self.limit
