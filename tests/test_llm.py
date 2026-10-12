#!/usr/bin/env python3
"""A reply must be able to say which model it came from, and the bundle must not
contradict itself about it (#61).

Stage 7 writes two artefacts from one call: `synth.json` records `config.model`
and `synth-llm-transcript.json` records `Reply.model`. Both are the id that was
*requested*. What the endpoint *reported* is a third value, `Reply.served_model`,
recorded beside them rather than allowed to overwrite them.

The gateway this repo is wired to does rewrite the id. Measured against
CLIProxyAPI at 100.64.0.2:8317, a request for `opencode-go/glm-5.3-flash` comes
back as `"model": "glm-5.3-flash"`. The prefix is stripped, and because that
gateway runs `force-model-prefix: true` the reported id is not routable -- a
reader who tries to replay it gets `400 unknown provider for model
glm-5.3-flash`. So the value written into the transcript cannot reproduce the
call it claims to record.

Which of the two ids wins was a separate decision (#61), now resolved: the requested
id is canonical, because it is the only one of the two that can be replayed against this
endpoint. What is not optional is that the bundle can state the answer, and never asserts
two answers to one question without marking which is which. That is what this script holds
fixed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env

SAVED_ENV = _env.isolate()


from dyak import llm  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


#: What was asked for. Prefixed, because this gateway only routes prefixed ids.
REQUESTED = "opencode-go/glm-5.3-flash"
#: What came back. The gateway strips the prefix.
ECHOED = "glm-5.3-flash"

BODY_ECHOED = {
    "choices": [{"message": {"content": "ok"}}],
    "model": ECHOED,
    "usage": {"prompt_tokens": 19, "completion_tokens": 3},
}
BODY_NO_MODEL = {
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 19, "completion_tokens": 3},
}

reply_echoed = llm._to_reply(BODY_ECHOED, REQUESTED, 0.1, 1)
reply_silent = llm._to_reply(BODY_NO_MODEL, REQUESTED, 0.1, 1)

# --- the requested id survives the round trip -------------------------------------

check(
    "a response with no model field falls back to what was requested",
    reply_silent.model == REQUESTED,
    f"{reply_silent.model}",
)

# --- the disagreement must be visible, not silent ---------------------------------
#
# `synth.json` gets `config.model` (synth.py:424 -> provenance, synth.py:457).
# The transcript gets `Reply.model` (via Reply.as_dict). Both are the requested id, so
# they agree by construction; `served_model` is what carries the endpoint's answer, and a
# bundle that omitted it would have no way to say the two ever differed.

config = llm.Config(endpoint="http://x/v1", model=REQUESTED)
provenance_model = config.model  # what synth.json will carry
transcript_model = reply_echoed.model  # what synth-llm-transcript.json will carry

check(
    "the reply states which model served it, not only which model was asked for",
    hasattr(reply_echoed, "served_model"),
    f"served_model={getattr(reply_echoed, 'served_model', '<absent>')}",
)

check(
    "the id written to the transcript is the one that was requested",
    transcript_model == REQUESTED,
    f"transcript={transcript_model} provenance={provenance_model}",
)

check(
    "synth.json and the transcript cannot name different models",
    provenance_model == transcript_model,
    f"synth.json={provenance_model} transcript={transcript_model}",
)

check(
    "a rewritten id is reported rather than absorbed",
    getattr(reply_echoed, "served_model", None) == ECHOED,
    f"requested={REQUESTED} served={getattr(reply_echoed, 'served_model', '<absent>')}",
)

# --- the common case must not regress ---------------------------------------------
#
# Every gateway that echoes the id it was given is the boring case, and the fix
# must not turn it into a warning. Same id in, same id out, no noise.

BODY_MATCHES = {
    "choices": [{"message": {"content": "ok"}}],
    "model": REQUESTED,
}
reply_matching = llm._to_reply(BODY_MATCHES, REQUESTED, 0.1, 1)
check(
    "an echo that matches the request raises nothing and records one id",
    reply_matching.model == REQUESTED,
    f"{reply_matching.model}",
)

# --- a timeout while the endpoint is sending headers -------------------------------
#
# `urlopen` wraps *connection* failures in URLError, but a timeout raised from
# `http.client.getresponse()` -- which is outside the block `urlopen` guards -- arrives as a
# bare `TimeoutError`. Run 10 died there: a traceback and a dead run where the contract says
# exit 3, "a dependency is installed and failing".
#
# A real socket, not a mock, because the whole point is that the mock would not have raised
# the same exception at the same place. The server accepts and then never writes a status line.
import socket  # noqa: E402
import threading  # noqa: E402
import time as _time  # noqa: E402

_server = socket.socket()
_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_server.bind(("127.0.0.1", 0))
_server.listen(4)
_stall_port = _server.getsockname()[1]


def _stall_forever():
    while True:
        try:
            conn, _ = _server.accept()
        except OSError:
            return
        threading.Thread(
            target=lambda c=conn: (c.recv(65536), _time.sleep(600)), daemon=True
        ).start()


threading.Thread(target=_stall_forever, daemon=True).start()

_saved_retry, _saved_sleep = llm.RETRY_MAX, llm.RETRY_SLEEP
llm.RETRY_MAX, llm.RETRY_SLEEP = 1, 0.0
try:
    _probe = llm.Config(endpoint=f"http://127.0.0.1:{_stall_port}", model="m", timeout=0.5)
    _raised = None
    _began = _time.monotonic()
    try:
        llm.chat([{"role": "user", "content": "hi"}], _probe)
    except llm.EndpointError as _exc:
        _raised = _exc
    except BaseException as _exc:
        _raised = _exc
    check(
        "a hung endpoint raises EndpointError, not a bare TimeoutError",
        isinstance(_raised, llm.EndpointError),
        f"{type(_raised).__name__}: {_raised}",
    )
    check(
        "and the message names the endpoint and the timeout it waited",
        isinstance(_raised, llm.EndpointError)
        and str(_stall_port) in str(_raised)
        and "timed out" in str(_raised),
        str(_raised),
    )
    check(
        # Retry exhaustion must still end in EndpointError. Before the fix this was the path
        # that killed run 10, so the two must not be conflated with a retryable 5xx.
        "it did not hang: the probe returned promptly",
        _time.monotonic() - _began < 30.0,
        f"{_time.monotonic() - _began:.1f}s",
    )
finally:
    llm.RETRY_MAX, llm.RETRY_SLEEP = _saved_retry, _saved_sleep
    _server.close()

# --- #124: a truncated reply must be distinguishable from a finished one -----------
#
# `finish_reason` is the endpoint saying *why* the model stopped. Without it a section cut
# off at the output cap is byte-identical, in the transcript and in the note, to one the
# model finished -- so a truncated derivation passes lint and audit as complete content.

BODY_TRUNCATED = {
    "choices": [{"message": {"content": "раздел обрывается на"}, "finish_reason": "length"}],
    "model": REQUESTED,
    "usage": {"prompt_tokens": 19, "completion_tokens": 1024},
}
reply_truncated = llm._to_reply(BODY_TRUNCATED, REQUESTED, 0.1, 1)
reply_finished = llm._to_reply(
    {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}, REQUESTED, 0.1, 1
)
reply_silent_reason = llm._to_reply(
    {"choices": [{"message": {"content": "ok"}}]}, REQUESTED, 0.1, 1
)

check(
    "a reply records why the model stopped",
    reply_truncated.finish_reason == "length",
    f"{reply_truncated.finish_reason!r}",
)
check(
    "the transcript entry carries finish_reason, not just the object",
    reply_truncated.as_dict().get("finish_reason") == "length",
    str(sorted(reply_truncated.as_dict())),
)
check(
    "a truncated reply is marked truncated",
    reply_truncated.truncated and not reply_finished.truncated,
    f"length={reply_truncated.truncated} stop={reply_finished.truncated}",
)
check(
    "an endpoint that says nothing is not assumed truncated",
    reply_silent_reason.finish_reason is None and not reply_silent_reason.truncated,
    f"{reply_silent_reason.finish_reason!r}",
)
_tr = llm.Transcript()
_tr.record(7, "section 1", [{"role": "user", "content": "x"}], reply_truncated)
check(
    "the written transcript artefact includes finish_reason per call",
    _tr.as_dict()["calls"][0].get("finish_reason") == "length",
    str(_tr.as_dict()["calls"][0]),
)

# --- #124: a 429 must be retried when the endpoint says, not when this client guesses --
import urllib.error  # noqa: E402

_real_urlopen = llm.urllib.request.urlopen
_real_sleep = llm.time.sleep
_saved_max = llm.RETRY_MAX


def _sleeping_429(retry_after):
    """urlopen that answers 429 with the given Retry-After header."""

    def _urlopen(_request, timeout=None):
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        raise urllib.error.HTTPError(
            "http://e/v1/chat/completions", 429, "Too Many Requests", headers, None
        )

    return _urlopen


_slept: list[float] = []
try:
    llm.time.sleep = _slept.append
    llm.RETRY_MAX = 3
    llm.urllib.request.urlopen = _sleeping_429("7")
    try:
        llm.chat([{"role": "user", "content": "hi"}], llm.Config(endpoint="http://e/v1", model="m"))
    except llm.EndpointError:
        pass
    check(
        "a 429 with Retry-After sleeps the interval it named, not the static ladder",
        _slept == [7.0, 7.0],
        f"slept={_slept}",
    )

    _slept.clear()
    llm.urllib.request.urlopen = _sleeping_429("9999")
    try:
        llm.chat([{"role": "user", "content": "hi"}], llm.Config(endpoint="http://e/v1", model="m"))
    except llm.EndpointError:
        pass
    check(
        "an absurd Retry-After is capped, not obeyed for hours",
        _slept == [llm.RETRY_AFTER_CAP, llm.RETRY_AFTER_CAP],
        f"slept={_slept} cap={llm.RETRY_AFTER_CAP}",
    )

    _slept.clear()
    llm.urllib.request.urlopen = _sleeping_429("Wed, 21 Oct 2026 07:28:00 GMT")
    try:
        llm.chat([{"role": "user", "content": "hi"}], llm.Config(endpoint="http://e/v1", model="m"))
    except llm.EndpointError:
        pass
    check(
        "the HTTP-date form falls back to the static ladder, not a skewed clock",
        _slept == [llm.RETRY_SLEEP, llm.RETRY_SLEEP * 2],
        f"slept={_slept}",
    )
finally:
    llm.urllib.request.urlopen = _real_urlopen
    llm.time.sleep = _real_sleep
    llm.RETRY_MAX = _saved_max

# --- #124: the breaker counts connections, not HTTP verdicts ------------------------
_conn = llm.EndpointError("refused", connection=True)
_verdict = llm.EndpointError("HTTP 400 from x: bad request")
check(
    "a connection-level failure says so",
    _conn.connection and not _verdict.connection,
    f"conn={_conn.connection} verdict={_verdict.connection}",
)
_br = llm.Breaker(limit=3)
check(
    "the breaker trips on the Kth consecutive connection failure, not before",
    [_br.fail(), _br.fail(), _br.fail()] == [False, False, True],
    f"consecutive={_br.consecutive}",
)
_br.ok()
check(
    "one answer resets the run of failures",
    _br.consecutive == 0 and not _br.tripped,
    f"consecutive={_br.consecutive}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAIL  {len(failures)} model-id provenance check(s) failed:")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("all model-id provenance checks passed")
