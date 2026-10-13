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

## Concurrency

`DYAK_LLM_CONCURRENCY` (default **1**) is how many section requests stage 7 and stage 9 may
have in flight at once (#153). Both make one request per note section and both are blocking
`urllib` calls, so the number is a thread count: it bounds what this client has outstanding,
and says nothing about what the endpoint does with them.

The default is 1 because the endpoint this is built against is a local gateway. A gateway with
one model behind it serves concurrent requests by queueing them, so the knob buys wall clock
only where the provider does, and it stays opt-in.

Two rules hold at any value:

* **The calls are recorded in section order, not completion order.** ADR-0004 D4 makes the
  recorded order part of the provenance contract, so `Transcript.record` takes the section a
  call belongs to and `as_dict` -- the artefact -- writes the calls in that order.
* **Every shared write stays on the calling thread.** `map_bounded` runs the request in a
  worker and nothing else: the loop that owns the transcript, the findings and the progress
  line consumes the replies in the order it submitted the jobs, so no lock is needed and the
  interleaving the counter shows is the section order (#197).

The two stages stop differently, and that difference lives in their own loops rather than
here: stage 9 stops at the first failed call, stage 7 after `BREAKER_MAX` consecutive
connection failures. `map_bounded` takes the rule as a predicate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from . import exitcodes as ec
from . import runner
from .settings import LLM_CONCURRENCY_ENV as CONCURRENCY_ENV
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

#: The sampling seed every `chat()` call sends (#77). A constant and not a setting: a
#: `DYAK_LLM_SEED` would be a documented key whose promise this pipeline cannot keep, and it
#: would add a failure mode -- someone sets it and expects two runs to agree. The owner asked
#: for the note to come out the same, not for a difference to be re-rollable, and a random
#: per-run seed would let a difference be re-rolled without making it smaller.
#:
#: The value is arbitrary; what carries the meaning is that one constant reaches every call, so
#: two runs of the same lecture send the same request. Whether this gateway honours `seed` is a
#: property of the gateway and is not verifiable from this tree, so the claim a bundle can make
#: is that this seed *was sent* -- recorded by `Config.redacted()` for exactly that claim and
#: nothing stronger. Bit-identical notes stay out of reach: the model version is a variable
#: this code does not control either (`served_model` is what records it).
SEED = 20261012


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


#: What stands in for a credential in the redacted form: the userinfo of an endpoint URL (#273) and
#: the value of a query or fragment parameter whose name looks like a credential (#279). A constant
#: and not an empty string, so two runs that differ only in whether credentials were configured stay
#: distinguishable in a bundle -- which is the whole point of recording an endpoint -- while no part
#: of the credential survives. The password this replaces is not a configuration detail worth
#: comparing: the live request does not use a URL userinfo either. `chat()` authenticates with
#: `Authorization: Bearer <api_key>` below, and `urllib` writes a `user:password@` netloc into the
#: `Host` header as-is, so an endpoint with userinfo is a credential that leaks and does nothing.
#: Named for the credential rather than the userinfo since #279, when the query string turned out to
#: be the same gap under a different name; it was `REDACTED_USERINFO` before.
REDACTED_CREDENTIAL = "***"

#: Parameter names whose *value* is masked in the redacted form (#279), matched as substrings of the
#: lowercased name so that `api_key`, `X-Amz-Signature` and `x-goog-api-key` are covered without
#: listing every spelling. A closed list, because no URL marks which of its parameters is a
#: credential and guessing from the value's shape is not possible -- a secret has no shape. This is
#: a heuristic and claims no completeness. Its two errors are asymmetric, and the asymmetry is the
#: justification: a false positive (`?monkey=1`) masks a value whose NAME survives, so two runs stay
#: comparable, which is what `redact_endpoint` exists to protect, while a false negative writes a
#: live key into a bundle that gets archived. The price is named rather than hidden: `?monkey=1`
#: loses its value. The stated misses are a name containing none of these words, a secret in the
#: path, and a secret in the host. The mechanism that reaches no artefact is `DYAK_LLM_API_KEY`:
#: it goes into the `Authorization` header and is recorded only as a boolean.
CREDENTIAL_NAMES = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "sig",
    "signature",
    "auth",
    "credential",
    "bearer",
)


def _redact_params(raw: str) -> str:
    """`k=v&k2=v2` with the value of every credential-named pair replaced (#279).

    Names, order and pair count are kept exactly: an ordinary query (`?api-version=2026-01-01`) is
    provenance and survives byte for byte, and a masked one still reads as the same parameter asking
    for the same thing. A pair with no `=` has no value to mask and is returned as it came.
    """
    pairs = []
    for pair in raw.split("&"):
        name, sep, _value = pair.partition("=")
        if sep and any(word in name.lower() for word in CREDENTIAL_NAMES):
            pairs.append(f"{name}={REDACTED_CREDENTIAL}")
        else:
            pairs.append(pair)
    return "&".join(pairs)


def redact_endpoint(endpoint: str) -> str:
    """An endpoint URL with its credentials replaced by `***` (#273, #279).

    `Config.redacted()` promises a config safe to write into a bundle, and the key was never the
    only credential a config can carry: `endpoint` went into the record verbatim, so an endpoint
    like `http://user:password@host:8317/v1` wrote its password into every artefact holding that
    dict -- `endpoint` in `synth`, `audit` and `repair` provenance, inside `model` in `caption`'s.
    Redacting here rather than at those four call sites covers all of them and any consumer added
    later, and leaves `Config.endpoint` itself alone: the string that goes over the network is not
    the string that goes into a bundle.

    Two steps, one for each way an endpoint URL can carry a credential: the userinfo becomes
    `***`, and then every query and fragment parameter whose name is in `CREDENTIAL_NAMES` has its
    value masked, names and order untouched. Scheme, host, port, path, and the names, order and
    non-credential values of the query and fragment all survive. They are what names an endpoint and
    what makes two runs comparable -- and what the second step is deliberately conservative about,
    since a query is provenance until it is a credential. See `CREDENTIAL_NAMES` for what the rule
    cannot see; this is a heuristic and no completeness is claimed for it.
    """
    parts = urllib.parse.urlsplit(endpoint)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED_CREDENTIAL}@{netloc.rpartition('@')[2]}"
    return urllib.parse.urlunsplit(
        parts._replace(
            netloc=netloc,
            query=_redact_params(parts.query),
            fragment=_redact_params(parts.fragment),
        )
    )


def append_path(url: str, segment: str) -> str:
    """`url` with `segment` appended to its path, and its query and fragment left alone (#281).

    The obvious `f"{url}{segment}"` appends to the whole string, so an endpoint that already
    carries a query -- the shape #279 made safe to record, and the reason this matters, because
    such an endpoint could not work until the segment landed in the path -- produced
    `POST /v1?api_key=<value>&api-version=2026-01-01/chat/completions`: the path stayed `/v1` and
    the segment became part of the last parameter's value, so both the path and the parameter were
    wrong. Splitting the URL is what keeps the segment in the path, and stripping the trailing
    slash from the path alone is what keeps a trailing slash from becoming a double slash
    (`http://h:8317/` gains one segment, not two).

    The same join is behind both `deps.py` probes, which is why this is a shared helper rather
    than a property: they append a different segment (`/models`), and nothing else about them
    differs.

    Malformed input is passed through rather than refused wherever `urlsplit` can split it,
    exactly as the concatenation was. That is most of it, and not all of it: no scheme, an empty
    string, `%zz`, `not a url` and a relative path all still yield a URL, so `Config.url` stays
    safe to call from an error path -- but a bracketed netloc that is not an IPv4/IPv6/IPvFuture
    literal (`http://[myhost]:8317/v1`, `http://[not-an-ipv6]/v1`) and an unterminated bracket
    (`http://[::1/v1`) raise `ValueError`, and `Config.url` therefore raises for those where the
    old concatenation returned a string. Measured, the impact is that the failure moves one call
    earlier and not that it appears: `urllib.request.Request(<that string>)` raised the same
    `ValueError` with the same message at the base. The raise is kept rather than guarded,
    because it names the join as the problem instead of the transport.

    `urlunsplit` normalises twice more, and both are stated rather than fixed: the scheme is
    lowercased (`HTTP://H:8317/V1` -> `http://H:8317/V1/chat/completions`; host and path keep
    their case), and an empty query or fragment loses its delimiter, so a bare `?` or `#` is
    dropped.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=parts.path.rstrip("/") + segment))


@dataclass(frozen=True)
class Config:
    endpoint: str
    model: str
    api_key: str | None = None
    timeout: float = 600.0
    temperature: float = 0.2
    #: Sent with every call. Last field on purpose: every existing construction is positional
    #: (`Config(endpoint, model, key)`), and a new field in the middle would silently shift
    #: them. Not read by `from_env()` -- see `SEED`.
    seed: int = SEED

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
        return append_path(self.endpoint, "/chat/completions")

    def redacted(self) -> dict:
        """Config safe to write into a bundle. The key never is."""
        return {
            # `redact_endpoint`, not `self.endpoint` (#273): the docstring above is about the whole
            # dict, and the key was never the only credential a config can carry -- a URL with
            # userinfo put its password in here verbatim while `api_key` was correctly a boolean.
            "endpoint": redact_endpoint(self.endpoint),
            "model": self.model,
            "api_key": bool(self.api_key),
            "temperature": self.temperature,
            # Recorded because the seed is an input to the note: without it a bundle says which
            # model answered and not which sample was asked for (#77). It is the one sampling
            # input the four `chat()` call sites share -- stage 7's synthesis, stage 9's two
            # critic loops (sequential and concurrent) and stage 6's captioner. Stage 10's
            # repair makes no model call, so it shares nothing: the sentence here said "every
            # stage" until #256, and that was false for the one stage that sends nothing.
            "seed": self.seed,
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
        """The call's metadata, and the model's output in full.

        `text` is last so every key that was here keeps its position in the artefact's JSON,
        and it is not truncated (#252). ADR-0004 D4 asks that a note be traceable to the exact
        model output; this dict is the only place that output is recorded, and the transcript
        that omitted it recorded the question and not the answer -- which is why #240's
        near-match measurement could only be finished by replaying the recorded requests
        against the live endpoint, and why a finding cut at 300 characters (`audit.py:504`)
        could not be recovered from the bundle at all. The cost is the reply's 1-3 KB against
        a request of ~124 KB per audit call, so the cheap half is the one worth keeping whole.
        """
        return {
            "model": self.model,
            "served_model": self.served_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 3),
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
            "text": self.text,
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
        # #77: the same constant for every call that reaches a model -- the four call sites in
        # stages 6, 7 and 9 -- so the note is a function of (source, transcript digest, model,
        # prompt, seed) rather than of whatever the endpoint happened to sample. Sent here, at
        # the one choke point, rather than by each stage. Stage 10 sends no prompt at all, so it
        # is outside the claim rather than an exception to it.
        "seed": config.seed,
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
            # `redact_endpoint` here and in the three messages below, not `config.url` (#279):
            # these strings leave the process -- into logs (`runner.py:101` prints the message to
            # stderr) and into `run-state.json`'s `error` field, which keeps the reason verbatim
            # (`pipeline.py:280`, #271/#286) -- and an endpoint URL can carry a credential in its
            # userinfo or its query string. The request built above keeps the raw `config.url`,
            # because that string goes to the endpoint and authentication may live in it.
            last = EndpointError(f"HTTP {exc.code} from {redact_endpoint(config.url)}: {detail}")
            if exc.code not in RETRY_STATUS:
                raise last from exc
            if attempt < RETRY_MAX:
                # The endpoint's own hint beats this client's ladder: a 429 that says "30 s"
                # and is retried after 2 s is a request the endpoint has already refused (#124).
                time.sleep(_retry_after(exc) or RETRY_SLEEP * attempt)
        except urllib.error.URLError as exc:
            # Connection refused, DNS failure, timeout. All transient as far as this code can
            # tell, and all indistinguishable to it -- so they are retried and then reported.
            last = EndpointError(
                f"{redact_endpoint(config.url)} unreachable: {exc.reason}", connection=True
            )
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
                f"{redact_endpoint(config.url)} timed out after {config.timeout:g}s: {exc}",
                connection=True,
            )
            if attempt < RETRY_MAX:
                time.sleep(RETRY_SLEEP * attempt)
        except json.JSONDecodeError as exc:
            # A body that is not JSON is a proxy or an error page, not a refusal. Retrying
            # cannot help and retrying a 500-shaped HTML page three times is worse.
            raise EndpointError(f"{redact_endpoint(config.url)} returned non-JSON: {exc}") from exc
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


#: The key `record` carries a call's section position in until `as_dict` writes the artefact.
#: Not a fact about the call, so it is stripped: `purpose` already names the section, and the
#: sequential transcript must not gain a field the concurrent one needs.
_SECTION_KEY = "section"


@dataclass
class Transcript:
    """Every call made, for the provenance chain.

    Held in memory and written once. ADR-0004 D4 requires that a note can be traced to the exact
    model output it came from, and the cheapest way to guarantee that is to record it at the
    point of the call rather than reconstructing it later. That output is `Reply.as_dict`'s
    `text`, in full: before #252 an entry carried the request and the call's metadata and no
    answer, so the artefact could not say what any model had said.

    `calls` is the order the calls *finished* in, which under `DYAK_LLM_CONCURRENCY > 1` is not
    the order of the note. The artefact is `as_dict`, which writes them in section order; a
    reader who needs the provenance contract reads that and not this list.
    """

    calls: list[dict] = field(default_factory=list)

    def record(
        self,
        stage: str,
        purpose: str,
        messages: list[dict],
        reply: Reply,
        *,
        section: int | None = None,
    ) -> None:
        """Record one call. `section` is its position in the note, for the artefact's order.

        A call with no section keeps the order it was recorded in -- today every caller is a
        per-section loop and passes one, but a stage that records outside a loop must not have
        to invent a number to stay where it is.
        """
        entry = {
            "stage": stage,
            "purpose": purpose,
            "messages": messages,
            **reply.as_dict(),
        }
        if section is not None:
            entry[_SECTION_KEY] = section
        self.calls.append(entry)

    def as_dict(self) -> dict:
        """The artefact: `calls` in section order (ADR-0004 D4).

        Concurrent calls come back in completion order, and the note they document is read in
        section order, so the order is restored here rather than at each call site -- one place
        owns the contract, and a stage that records calls gets it without asking.

        `sorted` is stable, so unkeyed calls keep their relative order and follow the keyed
        ones. At `DYAK_LLM_CONCURRENCY=1` the sections are already ascending and this is the
        identity, which is what keeps the sequential transcript byte-identical.
        """
        if not any(_SECTION_KEY in call for call in self.calls):
            return {"calls": self.calls}
        ordered = sorted(
            self.calls,
            key=lambda call: (call.get(_SECTION_KEY) is None, call.get(_SECTION_KEY) or 0),
        )
        return {
            "calls": [
                {key: value for key, value in call.items() if key != _SECTION_KEY}
                for call in ordered
            ]
        }

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


#: Section requests stage 7 and stage 9 may have in flight at once when `DYAK_LLM_CONCURRENCY`
#: is unset. 1 is the sequential run this client shipped with, and it is the right default for
#: the local gateway the stages are pointed at (#153).
DEFAULT_CONCURRENCY = 1


def concurrency() -> int:
    """How many section requests one stage may have in flight at once.

    Read where it is used rather than at import, so an unset variable leaves the run exactly as
    it was and a test can set it per call.

    A value that is not a positive whole number is refused, not clamped. Clamping would make
    `DYAK_LLM_CONCURRENCY=0` -- what someone writes to ask for sequential -- look like it
    worked; exit 1 names the variable instead, the same answer
    `DYAK_OPENAI_MAX_UPLOAD_BYTES` gives a bad count (`stt/openai_compat.py:470`).
    """
    raw = resolve(CONCURRENCY_ENV, default=str(DEFAULT_CONCURRENCY)).value
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        raise runner.DependencyError(
            name="llm",
            code=ec.USAGE,
            message=f"{CONCURRENCY_ENV} is not a positive request count: {raw!r}",
            remediation=(
                "set it to the number of section requests stage 7 and stage 9 may have in "
                f"flight at once (default {DEFAULT_CONCURRENCY} = one at a time)"
            ),
        )
    return value


T = TypeVar("T")


def map_bounded(
    jobs: list[Callable[[], T]],
    workers: int,
    *,
    stop_when: Callable[[object], bool] | None = None,
) -> list[T | EndpointError | NotConfiguredError | None]:
    """Call every job with at most `workers` in flight; results in the order they were given.

    Only the request runs in a worker. The caller keeps every shared write -- the transcript,
    the findings, the progress line -- on its own thread, and consumes this list in order, so
    the recorded section order is a property of the loop and not of a lock (#153).

    `stop_when` is asked after each reply, in that same order, whether the calls after it
    should be left alone; an unattempted job's slot is `None`. The predicate belongs to the
    caller because the two stages stop on different things: stage 9 stops at the first failed
    call, where its sequential loop `break`s, and stage 7 stops after `BREAKER_MAX` consecutive
    connection failures. Calls already in flight when the predicate trips still finish -- at
    most `workers` of them -- and their replies are kept, so a dead endpoint costs a bounded
    number of ladders instead of one per section (#124).

    `EndpointError` and `NotConfiguredError` come back as values, because both stages catch
    both around their call and a raised one would lose the section it belongs to. Anything else
    is a bug in this program and propagates.
    """
    results: list[T | EndpointError | NotConfiguredError | None] = [None] * len(jobs)
    if not jobs:
        return results
    window = max(1, min(workers, len(jobs)))
    with futures.ThreadPoolExecutor(max_workers=window) as pool:
        pending: deque[tuple[int, futures.Future]] = deque()

        def start(index: int) -> None:
            pending.append((index, pool.submit(jobs[index])))

        for index in range(window):
            start(index)
        following = window
        stopped = False
        while pending:
            index, future = pending.popleft()
            results[index] = _outcome(future)
            if stop_when is not None and not stopped and stop_when(results[index]):
                # Not `break`: the calls already in flight are paid for either way, and each
                # reply belongs to the section that asked for it.
                stopped = True
            if not stopped and following < len(jobs):
                start(following)
                following += 1
    return results


def _outcome(future: futures.Future) -> object:
    """A job's reply, or the failure its caller would have caught. Anything else propagates."""
    try:
        return future.result()
    except (EndpointError, NotConfiguredError) as exc:
        return exc
