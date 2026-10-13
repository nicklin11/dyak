#!/usr/bin/env python3
"""#289: the STT endpoint's credential does not reach run-state.json.

The same URL leak #284 fixes in `stt-provenance.json` has a second route out of the process: the
failure messages. `check()` (both backends) interpolates `self.base_url` into the
`DependencyError` it raises -- `whispercpp.py:115`, `:149` and `openai_compat.py:172`, `:203` at the
base this suite was written on; on current main the same four sites are `whispercpp.py:117`, `:153`
and `openai_compat.py:173`, `:206` -- and those messages *are* the exception's `str()`, which
`pipeline.py:540` records verbatim in `run-state.json`'s `error` (`pipeline.py:280`), a file
`bundle.py` publishes. Measured before any fix: a run whose STT endpoint is
`http://svc:<password>@127.0.0.1:<port>?api_key=<query>&api-version=2026-01-01` writes both invented
credentials into that artefact, and that artefact is the only file in the bundle carrying them.

**This suite is the pin, not the fix.** The masking is #308's, on main since `2dfac69`: both
producers mask the endpoint where they build the record (`redact_endpoint(self.base_url)`), so
nothing in `src/` changes for these checks to pass. What this file adds is the evidence -- the four
messages are checked for the credential's absence *and* for the endpoint staying namable, the four
request URLs are checked for its presence, and the structural checks below are what notice a fifth
message site added later.

Six checks below are behavioural and five are structural, and they pull in opposite directions on
purpose: the *record* must never quote the credential, and the *request* must still send it. A fix
that redacted the request instead of the message would pass every record check and break
authentication, so the request's own URL is captured and asserted raw.

`redact_endpoint` is the helper #273/#279 already made the four `llm` messages go through, and
#308 reuses it at these four sites rather than reimplementing it. Its use from `stt` is safe in the
direction that matters -- the checks below walk the real import graph of this tree and show the LLM
layer closes over no `stt` module, so that edge cannot become a cycle.

Every value here is invented and every endpoint is a name that resolves nowhere.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()

from dyak import runner  # noqa: E402
from dyak.stt import openai_compat as dyoc  # noqa: E402
from dyak.stt import whispercpp as dyw  # noqa: E402

failures: list[str] = []

PASSWORD = "not-a-real-stt-password"
QUERY = "not-a-real-stt-query-secret"
HEADER_KEY = "not-a-real-stt-header-key"
VERSION = "2026-01-01"

OPENAI_URL = f"http://svc:{PASSWORD}@h:8390?api_key={QUERY}&api-version={VERSION}"
WHISPER_URL = f"http://svc:{PASSWORD}@h:8391?token={QUERY}&api-version={VERSION}"
NOT_HTTP = f"ftp://svc:{PASSWORD}@h:8390/x?api_key={QUERY}"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def _message(cls, url: str) -> str:
    """`check()`'s message for a backend whose endpoint refuses the connection."""

    def _refused(request, *args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    previous = urllib.request.urlopen
    urllib.request.urlopen = _refused
    try:
        cls(url).check()
        return "(no exception raised)"
    except runner.DependencyError as exc:
        return exc.message
    finally:
        urllib.request.urlopen = previous


def _request(cls, url: str) -> dict:
    """The first request the backend makes, captured instead of sent."""
    seen: list[dict] = []

    class _Response:
        def read(self, size: int = -1) -> bytes:
            return b'{"object": "list", "data": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _capture(request, *args, **kwargs):
        seen.append({"url": request.full_url, "headers": dict(request.headers or {})})
        return _Response()

    previous = urllib.request.urlopen
    urllib.request.urlopen = _capture
    try:
        cls(url).check()
    except runner.DependencyError:
        pass
    finally:
        urllib.request.urlopen = previous
    return seen[0] if seen else {"url": "(no request)", "headers": {}}


# --- the record: the credential is masked, and the rest of the URL is not --------------------
# The `h:8390` host is in the assertion as well as the credentials: the redaction has to name the
# endpoint, or two failed runs cannot be told apart afterwards -- which is the whole reason the
# URL was in the message.
_openai_message = _message(dyoc.OpenAICompat, OPENAI_URL)
check(
    "#289: the OpenAI-compatible failure message quotes the redacted endpoint",
    PASSWORD not in _openai_message
    and QUERY not in _openai_message
    and "***@" in _openai_message
    and "api_key=***" in _openai_message
    and "h:8390" in _openai_message
    and f"api-version={VERSION}" in _openai_message,
    _openai_message,
)
_whisper_message = _message(dyw.HttpWhisperCpp, WHISPER_URL)
check(
    "#289: and so does whisper.cpp's, including the credential-named `token`",
    PASSWORD not in _whisper_message
    and QUERY not in _whisper_message
    and "***@" in _whisper_message
    and "token=***" in _whisper_message
    and "h:8391" in _whisper_message,
    _whisper_message,
)
_refusals: dict[str, str] = {}
for _name, _cls in (("openai_compat", dyoc.OpenAICompat), ("whispercpp", dyw.HttpWhisperCpp)):
    try:
        _cls(NOT_HTTP).check()
        _refusals[_name] = "(no exception raised)"
    except runner.DependencyError as _exc:
        _refusals[_name] = _exc.message
check(
    "#289: the not-an-http(s) refusals quote the redacted URL too -- all four sites",
    all(
        PASSWORD not in text and QUERY not in text and "***@" in text and "api_key=***" in text
        for text in _refusals.values()
    ),
    json.dumps(_refusals),
)
# Two endpoints that differ only where the credentials are not: the redacted forms must differ,
# and the differing part must be the part that survives.
_other_message = _message(
    dyoc.OpenAICompat, f"http://svc:{PASSWORD}@h:8390?api_key={QUERY}&api-version=2025-12-31"
)
check(
    "#289: two endpoints stay distinguishable after redaction",
    _other_message != _openai_message
    and f"api-version={VERSION}" in _openai_message
    and "api-version=2025-12-31" in _other_message,
    f"{_openai_message!r} vs {_other_message!r}",
)


# --- the wire: the request still carries the credential --------------------------------------
# The check that matters most. Redacting the message is a record fix; redacting the request would
# be an authentication break, and it would still pass every check above.
os.environ["DYAK_OPENAI_KEY"] = HEADER_KEY
_openai_request = _request(dyoc.OpenAICompat, OPENAI_URL)
_whisper_request = _request(dyw.HttpWhisperCpp, WHISPER_URL)
check(
    "#289: the request keeps the raw credential, userinfo and query both",
    PASSWORD in _openai_request["url"]
    and QUERY in _openai_request["url"]
    and PASSWORD in _whisper_request["url"]
    and QUERY in _whisper_request["url"],
    f"openai={_openai_request['url']} whisper={_whisper_request['url']}",
)
check(
    "#289: and the Authorization header still carries its key",
    _openai_request["headers"].get("Authorization") == f"Bearer {HEADER_KEY}",
    str(_openai_request["headers"]),
)


# --- the structure: four messages through the helper, four requests raw -----------------------
# The behavioural checks above prove today's two backends; this one is what notices a fifth
# message site added later, which is how #289's family came to exist in the first place.
def _sources() -> dict[str, str]:
    return {
        name: (REPO / "src" / "dyak" / "stt" / name).read_text(encoding="utf-8")
        for name in ("whispercpp.py", "openai_compat.py")
    }


def _message_sites(source: str) -> tuple[int, int]:
    """`DependencyError(..., message=...)` mentioning `base_url`: (raw, through the helper).

    Counted off the AST rather than by grepping the file, because `{self.base_url}` also appears
    in the request URL and in `parameters()`' `endpoint` -- and those two must stay raw, one
    because it is the request (this issue) and one because it is `stt-provenance.json` (#284).
    """
    raw = redacted = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "message":
                continue
            text = ast.unparse(keyword.value)
            if "base_url" not in text:
                continue
            if "redact_endpoint" in text:
                redacted += 1
            else:
                raw += 1
    return raw, redacted


def _request_interpolations(source: str) -> int:
    """`f"{self.base_url}/..."` inside a `urllib.request.Request(...)` call."""
    found = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if ast.unparse(node.func) != "urllib.request.Request":
            continue
        first = node.args[0]
        if isinstance(first, ast.JoinedStr) and "self.base_url" in ast.unparse(first):
            found += 1
    return found


_src = _sources()
_sites = {name: _message_sites(text) for name, text in _src.items()}
_redacted = sum(redacted for _raw, redacted in _sites.values())
_raw_messages = sum(raw for raw, _redacted in _sites.values())
_raw_requests = sum(_request_interpolations(text) for text in _src.values())
check(
    "#289: all four messages go through the helper, and all four requests stay raw",
    _redacted == 4 and _raw_messages == 0 and _raw_requests == 4,
    f"messages (raw, redacted)={_sites} raw requests={_raw_requests}",
)


# --- the import graph: the new edge cannot close a cycle -------------------------------------
# `stt` importing `llm` is only safe while `llm` reaches no `stt` module. Asserted over the tree
# rather than by hand: an import added to `llm` later has to fail here, not in production.
def _modules() -> dict[str, Path]:
    found = {}
    for path in sorted((REPO / "src" / "dyak").rglob("*.py")):
        relative = path.relative_to(REPO / "src").with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def _imports(module: str, path: Path, known: dict[str, Path], *, lazy: bool) -> set[str]:
    """Every `dyak.*` module this file imports, resolved where a file exists.

    `lazy=False` keeps only module-scope imports, which is the set that can actually close an
    import cycle; `lazy=True` adds the ones written inside a function body, which this tree uses
    for exactly the back-edges that would otherwise be cycles (`backends.py:126`,
    `settings.py:140`, `:147`, `:159`).
    """
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = (
        list(ast.walk(tree)) if lazy else [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    )
    out: set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = package.split(".")
        if node.level > len(base):
            continue
        prefix = base[: len(base) - (node.level - 1)]
        if node.module:
            target = ".".join([*prefix, *node.module.split(".")])
            if target in known:
                out.add(target)
        else:
            for alias in node.names:
                target = ".".join([*prefix, alias.name])
                if target in known:
                    out.add(target)
    return out


def _closure(start: str, *, lazy: bool) -> set[str]:
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for target in _imports(current, _known[current], _known, lazy=lazy):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


_known = _modules()
_scope = _closure("dyak.llm", lazy=False)
_everything = _closure("dyak.llm", lazy=True)
_lazy_edges = {
    name: sorted(
        _imports(name, _known[name], _known, lazy=True)
        - _imports(name, _known[name], _known, lazy=False)
    )
    for name in ("dyak.settings", "dyak.stt.backends")
}
check(
    "#289: `llm`'s module-scope closure reaches no STT module, so `stt -> llm` is acyclic",
    not [m for m in _scope if m == "dyak.stt" or m.startswith("dyak.stt.")],
    f"module scope: {sorted(_scope)}; with function-local imports: "
    f"{sorted(m for m in _everything if m.startswith('dyak.stt'))}; "
    f"lazy edges in between: {_lazy_edges}",
)
_stt_edges = {
    name: sorted(_imports(name, _known[name], _known, lazy=False))
    for name in ("dyak.stt.whispercpp", "dyak.stt.openai_compat")
}
check(
    "#289: and both STT clients are in that edge, importing the helper at module scope",
    all("dyak.llm" in edges for edges in _stt_edges.values()),
    json.dumps(_stt_edges),
)
# The other half of the argument: the path from `llm` to `stt` that *does* exist is
# `llm -> settings -> (inside a function) stt`, which is why it is not a cycle either.
check(
    "#289: the one path from `llm` to `stt` is function-local in `settings`",
    _closure("dyak.llm", lazy=True) != _scope
    and "dyak.stt" in _everything
    and not any("stt" in edges for edges in _lazy_edges.values())
    and "dyak.stt" in _imports("dyak.settings", _known["dyak.settings"], _known, lazy=True),
    f"scope={sorted(_scope)} lazy in settings="
    f"{sorted(_imports('dyak.settings', _known['dyak.settings'], _known, lazy=True))}",
)
# The back-edge the STT clients already carry (`stt.whispercpp` imports `deps`, `deps` imports
# `stt`) is not a cycle for the same reason: the backend *factories* are imported inside
# `resolve`'s body rather than at module scope (`backends.py:126`).
_backends_tree = ast.parse(_known["dyak.stt.backends"].read_text(encoding="utf-8"))
_lazy = sorted(
    alias.name
    for node in ast.walk(_backends_tree)
    if isinstance(node, ast.ImportFrom) and node.module is None and node.level == 1
    for alias in node.names
)
_toplevel = {
    alias.name
    for node in _backends_tree.body
    if isinstance(node, ast.ImportFrom) and node.module is None
    for alias in node.names
}
check(
    "#289: `backends` imports both clients inside `resolve`, not at module scope",
    {"openai_compat", "whispercpp"} <= set(_lazy)
    and not ({"openai_compat", "whispercpp"} & _toplevel),
    f"lazy={_lazy} module-scope={sorted(_toplevel)}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all STT endpoint checks passed")
