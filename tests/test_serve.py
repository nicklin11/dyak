#!/usr/bin/env python3
"""`dyak serve`: M1 of the local web view (#242).

The design is the decision comment on #242, and this suite pins the parts of its M1 that can be
checked from outside the module:

    registration   `serve` is in `build_parser()` and its `fn` is `web.serve`; web.py is the
                   implementation, not a second entry point
    stdlib only    the module imports nothing outside the standard library, the page is a string
                   constant in it, and the page loads nothing from anywhere else
    the five       GET /, GET /api/files, POST /api/run, GET /api/events, GET /api/result, each
    endpoints      driven by the page and answered live against a real server on port 0
    one sink       the run's lines arrive in the SSE stream exactly as `pipeline.run` wrote them,
                   and nothing in the module interprets them
    exposure       token always, Host and Origin checked, a file contained under --root, one run
                   at a time, loopback by default with a warning when it is not
    the codes      every code in `exitcodes.DESCRIPTIONS` has the design's sentence in the page
    the page       the page's own JavaScript is run under node against a stub DOM: the log
                   listener parses the SSE payload, and a load during a run attaches to it
    docs and CI    `## dyak serve` names the subcommand and every flag, and ci.yml runs this file

Everything is local and stubbed: a fake pipeline (the shape `tests/test_stages.py` uses) and a
loopback server on port 0. No ffmpeg, no endpoint, no host state -- the result is the same on a
machine with a live transcriber and on one without (#175, #184). The one tool it may use is node,
for the page's JavaScript; a host without node prints SKIP for that section rather than passing it
silently, and CI has node.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()

from dyak import audit as dyau  # noqa: E402
from dyak import caption as dycap  # noqa: E402
from dyak import cli as dyc  # noqa: E402
from dyak import exitcodes as ec  # noqa: E402
from dyak import link as dylk  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import repair as dyrep  # noqa: E402
from dyak import report as dyro  # noqa: E402
from dyak import workspace as dyw  # noqa: E402

failures: list[str] = []

# A run redirects `sys.stdout` process-wide for its duration (`web.run_once`, and the CLI's own
# messages are the point of it), so this suite's output must not go through the object that
# redirect replaces. Otherwise a PASS line printed while a run is in flight lands in that run's
# log and the "verbatim, in order" check below starts depending on which thread printed first.
OUT = sys.stdout


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}", file=OUT)
    if not cond:
        failures.append(name)


# --- the module exists, and it is the one the design names -----------------------------
# #242's design is explicit: the implementation is `src/dyak/web.py`, not `serve.py`, and the
# subcommand is registered in `cli.py` beside doctor/process/audit/term. On unmodified main this
# import is the first thing that fails, and that failure is the point of the check.
try:
    from dyak import web  # noqa: E402
except ImportError as exc:  # pragma: no cover - the unmodified-main case
    check("dyak.web exists, and the implementation is web.py not serve.py", False, str(exc))
    print(f"\nFAILED: {len(failures)} -> {failures}", file=OUT)
    raise SystemExit(1)

WEB_SRC = REPO / "src" / "dyak" / "web.py"
check("the implementation lives in src/dyak/web.py", WEB_SRC.is_file(), str(WEB_SRC))
check(
    "no serve.py was added beside it",
    not (REPO / "src" / "dyak" / "serve.py").exists(),
    "a second module for the same subcommand is what the design rejected",
)

SERVE_PARSER = dyc.build_parser()


def _subparser(parser, name: str):
    """The sub-parser registered under `name`, or None. Same lookup `test_claims.py` uses."""
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action.choices.get(name)
    return None


SERVE_SUB = _subparser(SERVE_PARSER, "serve")
try:
    _serve_args = SERVE_PARSER.parse_args(["serve"])
except SystemExit:
    # On a tree without the registration argparse exits 2 here. That is a failure of this
    # suite, not of the suite's ability to report: the checks below still print.
    _serve_args = None
check(
    "serve is registered in build_parser() and dispatches to web.serve",
    _serve_args is not None and _serve_args.fn is web.serve,
    f"fn={getattr(_serve_args, 'fn', None)}",
)
check(
    "serve's own help says what it is",
    SERVE_SUB is not None and "loopback" in SERVE_SUB.description,
    getattr(SERVE_SUB, "description", "serve is not registered"),
)
check(
    "serve defaults to loopback, so reaching another machine is opt-in",
    _serve_args is not None
    and _serve_args.bind == web.LOOPBACK
    and _serve_args.port == web.DEFAULT_PORT,
    f"bind={getattr(_serve_args, 'bind', None)} port={getattr(_serve_args, 'port', None)}",
)
_serve_flags = [o for a in getattr(SERVE_SUB, "_actions", []) for o in a.option_strings]
for _flag in ("--root", "--port", "--bind", "--token", "--open"):
    check(f"serve registers {_flag}", _flag in _serve_flags, str(_serve_flags))

# --- standard library only, and a page that is a constant ------------------------------
WEB_TREE = ast.parse(WEB_SRC.read_text(encoding="utf-8"), filename=str(WEB_SRC))
imported: set[str] = set()
for _node in ast.walk(WEB_TREE):
    if isinstance(_node, ast.Import):
        imported.update(alias.name.split(".")[0] for alias in _node.names)
    elif isinstance(_node, ast.ImportFrom) and _node.level == 0 and _node.module:
        imported.add(_node.module.split(".")[0])
outside = sorted(name for name in imported if name not in sys.stdlib_module_names)
check(
    "web.py imports the standard library and nothing else",
    not outside,
    f"not stdlib: {outside}",
)
check(
    "and it imports enough to be the server it claims to be",
    {"http", "json", "secrets", "threading"} <= imported,
    f"imported: {sorted(imported)}",
)
check(
    "the page is a string constant in the module, so nothing is packaged or built",
    isinstance(web.PAGE, str) and len(web.PAGE) > 2000 and "<!doctype html>" in web.PAGE.lower(),
    f"{len(web.PAGE)} chars, type {type(web.PAGE).__name__}",
)
check(
    "no static directory and no package data is needed for it",
    not (REPO / "src" / "dyak" / "static").exists(),
    "the design rejected a static/ directory: it needs packaging changes",
)
check(
    "the page references no CDN, no external script and no external style",
    "http://" not in web.PAGE
    and "https://" not in web.PAGE
    and "<script src" not in web.PAGE
    and "<link" not in web.PAGE,
    "a CDN would leak the page URL, which carries the token",
)

# --- M1's one progress mechanism -------------------------------------------------------
# #244 owns the structured sink (ADR-0003 D1) and it is M2's, so M1 must not anticipate it. The
# checks below are the difference between "forwards the text sink" and "grew a second mechanism":
# no event types, no jsonl, and no interpretation of the lines on the way through.
check(
    "M1 adds no event machinery of its own",
    not (REPO / "src" / "dyak" / "events.py").exists()
    and "jsonl" not in WEB_SRC.read_text(encoding="utf-8"),
    "ADR-0003 D1's typed sink is #244 and M2's milestone",
)
check(
    "the pipeline's own stream= is what the server passes",
    "stream=sink" in WEB_SRC.read_text(encoding="utf-8")
    and "stream=stream" in (REPO / "src" / "dyak" / "cli.py").read_text(encoding="utf-8"),
    "the sink must travel to pipeline.run's stream= unchanged",
)
check(
    "nothing in the module reads a stage number out of a line",
    not re.search(r"\[\s*\d+\s*/\s*\d+\s*\]", WEB_SRC.read_text(encoding="utf-8")),
    "a page that parses [n/12] is a second source of truth for what stage a run is in",
)

# --- the exit codes, in the design's words ---------------------------------------------
# The design's section 5 table, verbatim. A page that says "something went wrong" to a child is
# the failure this table exists to prevent, so the sentences are pinned rather than trusted.
DESIGN_SENTENCES = {
    0: "Done. The note is in your notes folder.",
    1: "Done, without AI: structure, but no summary.",
    2: "Something this computer needs isn't set up yet (see above).",
    3: "Something the computer talks to stopped answering. Try again; if it repeats, ask the owner.",
    4: "The video was too blurry, or its sound and picture don't match. The note was still written.",
    5: "The note is written, with «N» lines the checker doesn't trust.",
    6: "The note's files couldn't be verified. Don't rely on it; ask the owner.",
    7: "The note is unfinished — part of it is missing.",
    8: "Part of the lecture didn't reach the writer, so the note is incomplete.",
    9: "One section was cut off mid-sentence.",
    130: "Stopped. Nothing was lost.",
}
check(
    "every exit code the tree defines has a sentence",
    set(DESIGN_SENTENCES) == set(ec.DESCRIPTIONS),
    f"table {sorted(DESIGN_SENTENCES)}, exitcodes {sorted(ec.DESCRIPTIONS)}",
)
check(
    "the module's sentences are the design's, not a reworded copy",
    web.EXIT_SENTENCES == DESIGN_SENTENCES,
    str(
        {
            k: (web.EXIT_SENTENCES.get(k), v)
            for k, v in DESIGN_SENTENCES.items()
            if web.EXIT_SENTENCES.get(k) != v
        }
    ),
)
# The page, not the dict: the page is what the child reads, and the substitution into the HTML is
# exactly the step that could silently drop a code.
_page_table = re.search(r"const EXIT = (\{.*?\});", web.PAGE, flags=re.DOTALL)
PAGE_SENTENCES = json.loads(_page_table.group(1)) if _page_table else {}
check(
    "the rendered page carries the same table",
    {int(k): v for k, v in PAGE_SENTENCES.items()} == DESIGN_SENTENCES,
    str(PAGE_SENTENCES)[:200],
)

# --- exposure: containment, the warning, and the checks in front of the endpoints ------
with tempfile.TemporaryDirectory() as _tmp:
    _root = Path(_tmp) / "lectures"
    _root.mkdir()
    (_root / "lecture1.mp4").write_bytes(b"x")
    (_root / "sub").mkdir()
    (_root / "sub" / "lecture2.mp4").write_bytes(b"x")
    _outside = Path(_tmp) / "outside.mp4"
    _outside.write_bytes(b"x")
    os.symlink(_outside, _root / "link.mp4")
    check(
        "a file under the root is accepted, at any depth",
        web.contained(_root, "lecture1.mp4") is not None
        and web.contained(_root, "sub/lecture2.mp4") is not None,
        str(web.contained(_root, "sub/lecture2.mp4")),
    )
    check(
        "a path that climbs out of the root is refused",
        web.contained(_root, "../outside.mp4") is None
        and web.contained(_root, str(_outside)) is None,
        "traversal and an absolute path are the same answer",
    )
    check(
        "a symlink out of the root is refused, because resolve() follows it",
        web.contained(_root, "link.mp4") is None,
        "the check is the resolved path, not the name",
    )
    check(
        "a directory is not a recording",
        web.contained(_root, "sub") is None,
        "only a regular file is accepted",
    )
check(
    "loopback raises no warning",
    web.exposure_warning(web.LOOPBACK) is None and web.exposure_warning("localhost") is None,
    "the default interface is not an exposure",
)
_warn = web.exposure_warning("100.64.0.1")
check(
    "a non-loopback bind warns, and the warning names what a peer can do",
    _warn is not None and "--root" in _warn and "token" in _warn,
    str(_warn),
)
_serve_src = WEB_SRC.read_text(encoding="utf-8")
check(
    "serve prints that warning, rather than only defining it",
    re.search(r"if warning is not None:\n\s+print\(", _serve_src) is not None,
    "a warning nobody prints is not a warning",
)

# --- the live server --------------------------------------------------------------------
TOKEN = "test-token-not-a-secret"
LINES = (
    "[1/12] probe starting",
    "[2/12] audio done in 1.2s",
    "           frames 3/58",
    "[3/12] stt starting",
)


def healthy_checks(*, required=None):
    """Accept `required` so the stub tracks `run_all`'s signature (as test_stages.py does)."""
    return [dyc.Check(name="ffmpeg", ok=True, detail="ok", path="/usr/bin/ffmpeg", version="7.1.1")]


def stub_result(source, work, bundle):
    """The shape `fake_pipeline` in `tests/test_stages.py` builds: real Report objects, so the ladder is real."""
    note = work.path / "note.md"
    note.write_text("# note\n", encoding="utf-8")
    audio = work.path / "audio.wav"
    audio.write_bytes(b"x")
    artefacts = {"audio.wav": audio, "note.md": note}
    if bundle is not None:
        artefacts = {name: bundle.publish(name, path) for name, path in artefacts.items()}
    return SimpleNamespace(
        source=source,
        artefacts=artefacts,
        final_note=artefacts["note.md"],
        seconds=1.5,
        bundle=bundle,
        quality=SimpleNamespace(
            frames=[], passed=[], failed=[], ok=True, summary=lambda: "0/0", explain=lambda: ""
        ),
        enhanced=[],
        transcript=SimpleNamespace(anomalies=(), summary=lambda: "3 segments, 40 timed words"),
        audit=dyau.Report(),
        repair=dyrep.Report(),
        link=dylk.Report(),
        note=SimpleNamespace(degraded=False, synthesizer="test", notes=()),
        caption=dycap.Report(),
        caption_outcome=dycap.CaptionOutcome(
            captioned=1, reused=0, failed=0, model="test-model", served_model=None
        ),
        report=dyro.Report(),
    )


release = threading.Event()


def serve_pipeline(source, work, **kw):
    """The stub: it writes the lines `pipeline.run` would write to the sink, then holds.

    Holding is what makes the one-run-at-a-time check a real check: the second request arrives
    while this one is inside `pipeline.run`, which is the only state that can refuse it.
    """
    stream = kw.get("stream") or sys.stdout
    for line in LINES:
        stream.write(line + "\n")
    stream.flush()
    release.wait(20)
    return stub_result(source, work, kw.get("bundle"))


_tmp = Path(tempfile.mkdtemp(prefix="dyak-serve-suite-"))
_root = _tmp / "lectures"
_root.mkdir()
(_root / "lecture1.mp4").write_bytes(b"x" * 2048)
(_root / "notes.txt").write_bytes(b"y")
(_tmp / "outside.mp4").write_bytes(b"z")
os.symlink(_tmp / "outside.mp4", _root / "link.mp4")
os.environ[dyw.WORKDIR_ENV] = str(_tmp / "work")
os.environ["DYAK_OUTPUT_DIR"] = str(_tmp / "bundles")
os.environ["DYAK_VAULT"] = str(_tmp / "vault")
(_tmp / "vault").mkdir()

_real_run_all, _real_run = dyc.run_all, dyp.run
dyc.run_all = healthy_checks
dyp.run = serve_pipeline

httpd, state = web.make_server(_root, token=TOKEN, port=0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://{web.LOOPBACK}:{state.port}"


def call(path, *, method="GET", payload=None, token=TOKEN, host=None, origin=None):
    """One request, returning (status, body, headers). `token=None` sends none at all."""
    url = BASE + path
    if token is not None:
        url += ("&" if "?" in path else "?") + "token=" + token
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, method=method, data=data)
    if host:
        request.add_header("Host", host)
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read().decode(), dict(reply.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        # No answer at all: the socket timed out or the connection dropped. Reported as status 0
        # so a tree that lost a check produces FAIL lines instead of a traceback that hides the
        # checks after it -- the accumulation rule the suites in this directory follow.
        return 0, str(exc), {}


def json_of(body: str) -> dict:
    """The parsed body, or an empty dict. A broken tree answers with a traceback, not JSON."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


try:
    status, body, headers = call("/")
    check(
        "GET / answers the page", status == 200 and "<!doctype html>" in body.lower(), f"{status}"
    )
    check(
        "the page is served with no-store and no-referrer",
        headers.get("Cache-Control") == "no-store"
        and headers.get("Referrer-Policy") == "no-referrer",
        f"{headers.get('Cache-Control')} / {headers.get('Referrer-Policy')}",
    )
    check(
        "and with a policy that names the page's own sources",
        "default-src 'none'" in headers.get("Content-Security-Policy", ""),
        headers.get("Content-Security-Policy", ""),
    )

    status, body, _ = call("/api/files")
    listed = json.loads(body)["files"] if status == 200 else []
    names = [f["name"] for f in listed]
    check(
        "GET /api/files lists the recordings under --root",
        status == 200 and "lecture1.mp4" in names and "notes.txt" in names,
        body[:200],
    )
    check(
        "the listing carries names, sizes and dates, and no path",
        all(set(f) == {"name", "size", "date"} for f in listed) and "/" not in json.dumps(listed),
        json.dumps(listed)[:200],
    )
    check(
        "a symlink pointing out of the root is not listed",
        "link.mp4" not in names,
        str(names),
    )

    check("a request with no token is refused", call("/", token=None)[0] == 401, "401")
    check("a request with the wrong token is refused", call("/", token="nope")[0] == 401, "401")
    for path in ("/api/files", "/api/events", "/api/result"):
        check(
            f"{path} is refused without the token",
            call(path, token=None)[0] == 401,
            "the token is required by every endpoint, not only the page",
        )
    check(
        "a Host header that is not this machine is refused (DNS rebinding)",
        call("/", host="evil.example")[0] == 403,
        "403",
    )
    # The bracketed IPv6 form: a browser writes the port after the bracket, and a rebinding page
    # can write anything at all. `_host_ok` reads up to the `]` and takes what follows only if it
    # is a port, so the loopback spellings are allowed and every foreign or malformed one is not.
    for _host, _want in (
        ("[::1]", 200),
        (f"[::1]:{state.port}", 200),
        (f"localhost:{state.port}", 200),
        ("[::ffff:127.0.0.1]:80", 200),
        ("[evil.example]:80", 403),
        ("[::1]evil", 403),
        ("[::1", 403),
        ("evil.example:80", 403),
    ):
        check(
            f"Host: {_host} is answered with {_want}",
            call("/", host=_host)[0] == _want,
            f"got {call('/', host=_host)[0]}",
        )
    check(
        "a state-changing request from another Origin is refused (CSRF)",
        call(
            "/api/run",
            method="POST",
            payload={"name": "lecture1.mp4"},
            origin="http://evil.example",
        )[0]
        == 403,
        "403",
    )
    check(
        "a run for a path outside --root is refused",
        call("/api/run", method="POST", payload={"name": "../outside.mp4"})[0] == 400,
        call("/api/run", method="POST", payload={"name": "../outside.mp4"})[1],
    )
    check(
        "a run for the symlink out of the root is refused",
        call("/api/run", method="POST", payload={"name": "link.mp4"})[0] == 400,
        call("/api/run", method="POST", payload={"name": "link.mp4"})[1],
    )
    check(
        "a run with no file named is refused",
        call("/api/run", method="POST", payload={})[0] == 400,
        call("/api/run", method="POST", payload={})[1],
    )
    # `Path.resolve()` raises ValueError for a NUL byte, and the name is untrusted input, so the
    # refusal is a 400 with a reason. A 500 here is a traceback in the server's log, which is the
    # one thing a request from a page must never be able to cause.
    _nul_status, _nul_body = call(
        "/api/run", method="POST", payload={"name": "lecture1\u0000.mp4"}
    )[:2]
    check(
        "a NUL byte in the requested name is a 400, not a 500 traceback",
        _nul_status == 400 and json_of(_nul_body).get("error"),
        f"{_nul_status} {_nul_body[:120]}",
    )

    frames: list[tuple[str, str]] = []
    sse_headers: dict[str, str] = {}
    stop_reader = threading.Event()

    def read_events() -> None:
        try:
            request = urllib.request.Request(f"{BASE}/api/events?token={TOKEN}")
            with urllib.request.urlopen(request, timeout=10) as reply:
                sse_headers.update(dict(reply.headers))
                name, data = "", ""
                deadline = time.time() + 25
                while not stop_reader.is_set() and time.time() < deadline:
                    line = reply.readline().decode().rstrip("\n")
                    if line.startswith("event: "):
                        name = line[len("event: ") :]
                    elif line.startswith("data: "):
                        data = line[len("data: ") :]
                    elif line == "" and name:
                        frames.append((name, data))
                        if name == "exit":
                            return
                        name, data = "", ""
        except Exception:
            return

    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    time.sleep(0.4)

    status, body, _ = call("/api/run", method="POST", payload={"name": "lecture1.mp4"})
    check(
        "POST /api/run starts a run and says so",
        status == 200 and json.loads(body)["started"],
        f"{status} {body}",
    )
    time.sleep(0.6)
    busy_status, busy_body = call("/api/run", method="POST", payload={"name": "lecture1.mp4"})[:2]
    check(
        "a second run while one is in flight is refused, and names the one running",
        busy_status == 409 and json_of(busy_body).get("name") == "lecture1.mp4",
        f"{busy_status} {busy_body}",
    )
    # The race: a run can start between `busy_with()` and `state.start()`. Stubbing the first check
    # out is how that path is reached, and it has to name the run for the same reason the other one
    # does -- the page attaches by name, and "already working on a lecture" leaves it nothing to
    # attach to.
    _real_busy_with = state.busy_with
    state.busy_with = lambda: None
    try:
        race_status, race_body = call("/api/run", method="POST", payload={"name": "lecture1.mp4"})[
            :2
        ]
    finally:
        state.busy_with = _real_busy_with
    check(
        "the 409 past busy_with() names the run too, so both paths let the page attach",
        race_status == 409 and json_of(race_body).get("name") == "lecture1.mp4",
        f"{race_status} {race_body}",
    )
    # What the page's load path reads to decide whether to attach: the run belongs to the server,
    # so /api/result has to say it is in flight while it is, not only once it is over.
    live_status, live_body = call("/api/result")[:2]
    live = json_of(live_body)
    check(
        "GET /api/result reports the run in flight, which is how a reloaded page attaches",
        live_status == 200 and live.get("running") is True and live.get("name") == "lecture1.mp4",
        f"{live_status} {live_body[:200]}",
    )
    release.set()
    for _ in range(100):
        if state.run is not None and state.run.done:
            break
        time.sleep(0.1)
    reader.join(timeout=10)
    stop_reader.set()
    check(
        "the SSE response carries the same security headers as every other one",
        sse_headers.get("Cache-Control") == "no-store"
        and sse_headers.get("Referrer-Policy") == "no-referrer"
        and "default-src 'none'" in sse_headers.get("Content-Security-Policy", ""),
        f"{sse_headers.get('Cache-Control')} / {sse_headers.get('Referrer-Policy')} / "
        f"{sse_headers.get('Content-Security-Policy')}",
    )

    log_lines = [e["fields"]["text"] for e in state.log._events if e["event"] == "log"]
    start = next(
        (i for i, text in enumerate(log_lines) if text == LINES[0]),
        -1,
    )
    check(
        "the SSE stream carries the pipeline's lines verbatim, in order",
        start >= 0 and tuple(log_lines[start : start + len(LINES)]) == LINES,
        f"log={log_lines[:8]}",
    )
    check(
        "the lines reach the page as their own SSE events",
        any(name == "log" and json.loads(data)["text"] == LINES[0] for name, data in frames),
        str(frames[:4]),
    )
    check(
        "the run's start and exit are events the server holds as values",
        any(name == "run_start" for name, _ in frames)
        and any(name == "exit" for name, _ in frames),
        str([name for name, _ in frames]),
    )
    exit_payload = next((json.loads(data) for name, data in frames if name == "exit"), {})
    check(
        "the exit event carries the code the CLI's ladder returned",
        exit_payload.get("code") == ec.OK,
        str(exit_payload),
    )

    status, body, _ = call("/api/result")
    result = json.loads(body)
    check(
        "GET /api/result reports the code, the note and the header lines",
        status == 200
        and result["code"] == ec.OK
        and result["note"]
        and Path(result["note"]).is_file()
        and result["header"]
        and result["running"] is False,
        body[:300],
    )
    check(
        "the note path is the run's own final_note, not a path rebuilt from the request",
        state.run.result is not None and result["note"] == str(state.run.result.final_note),
        f"{result['note']} vs {getattr(state.run.result, 'final_note', None)}",
    )
    check(
        "the run was exported to the vault the CLI resolves, so the page's promise holds",
        any("files exported to" in text for text in log_lines),
        str([t for t in log_lines if "vault" in t]),
    )
    check(
        "the page derives nothing from the log: no stage pattern and no percentage",
        re.search(r"\[\s*\d+\s*/\s*\d+\s*\]", web.PAGE) is None
        and "%" not in web.PAGE
        and "stage" not in web.PAGE.lower(),
        "the lines are shown as text; a progress bar needs M2's typed sink (#244), not a parse",
    )
    check(
        "the page drives all five endpoints",
        all(
            marker in web.PAGE
            for marker in ("/api/files", "/api/run", "/api/events", "/api/result")
        )
        and "EventSource" in web.PAGE,
        "the page must be the client of every endpoint the design lists",
    )
finally:
    dyc.run_all, dyp.run = _real_run_all, _real_run
    release.set()
    httpd.shutdown()
    httpd.server_close()

# --- the page's own JavaScript, run rather than read -------------------------------------
# The server half of the page's contract is pinned above, against a live server. The browser half
# -- the log listener parsing the SSE payload instead of showing the envelope, and the load path
# attaching when /api/result says a run is in flight -- only runs in a browser, and a source-level
# string match would not pin it. `node` runs the page's own script against a stub document, fetch
# and EventSource, so the behaviour is observed. Where node is absent the suite prints SKIP and
# this half is unpinned there, the convention tests/test_e2e.py:688 uses for ffmpeg; the CI image
# provides node, so CI does run it (see the PR body for the run that shows the PASS lines).
_SCRIPT = re.search(r"<script>(.*?)</script>", web.PAGE, re.S)
PAGE_JS = _SCRIPT.group(1) if _SCRIPT else ""
check(
    "the page carries its script in one <script> tag, so the JavaScript below can be run",
    bool(PAGE_JS),
    "web.PAGE has no <script>...</script>: the browser checks below cannot run, and every check "
    "after this one still does",
)

HARNESS = r"""
// Runs the page's own script with a stub document, fetch and EventSource. argv[2] is the scenario,
// argv[3] the page's script; the exit code is the verdict and the last line says which check ran.
const fs = require('fs');
const scenario = process.argv[2];
const script = fs.readFileSync(process.argv[3], 'utf8');
const elements = {};
function el(id) {
  if (!elements[id]) {
    const node = {
      id, textContent: '', hidden: false, disabled: false, scrollTop: 0, children: [],
      listeners: {}, classList: { add() {}, remove() {} }, innerHTML: '',
      addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
      appendChild(child) { node.children.push(child); },
    };
    elements[id] = node;
  }
  return elements[id];
}
global.document = {
  getElementById: el,
  createElement: () => el('created-' + Object.keys(elements).length),
  querySelectorAll: () => [],
};
const sources = [];
class EventSource {
  constructor(url) { this.url = url; this.listeners = {}; sources.push(this); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  close() { this.closed = true; }
  fire(name, data) { for (const fn of this.listeners[name] || []) fn({ data }); }
}
global.EventSource = EventSource;
global.location = { search: '?token=TOK' };
global.navigator = { clipboard: { writeText() {} } };
let resultPayload = { code: null, note: null };
let runStatus = 200;
let runBody = { started: true, name: 'lecture1.mp4' };
if (scenario === 'running' || scenario === 'log') {
  resultPayload = { running: true, name: 'lecture1.mp4', code: null, note: null };
}
if (scenario === 'busy') {
  runStatus = 409;
  runBody = { error: 'already working on \u00ablecture1.mp4\u00bb', name: 'lecture1.mp4' };
}
global.fetch = async (url) => {
  if (url.startsWith('/api/files')) {
    return { ok: true, status: 200, json: async () => ({ files: [] }) };
  }
  if (url.startsWith('/api/result')) {
    return { ok: true, status: 200, json: async () => resultPayload };
  }
  if (url.startsWith('/api/run')) {
    return { ok: runStatus < 400, status: runStatus, json: async () => runBody };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  eval(script);
  await wait(60);
  if (scenario === 'busy' || (scenario === 'log' && !sources.length)) {
    for (const fn of el('start').listeners['click'] || []) await fn();
    await wait(60);
  }
  if (scenario === 'idle') {
    console.log(`${sources.length === 0 ? 'PASS' : 'FAIL'}  idle load attached ${sources.length}`);
    process.exit(sources.length === 0 ? 0 : 1);
  }
  if (scenario === 'running') {
    const ok = sources.length === 1 && sources[0].url.includes('/api/events?token=TOK');
    console.log(`${ok ? 'PASS' : 'FAIL'}  load attached ${sources.length} stream(s)`);
    process.exit(ok ? 0 : 1);
  }
  if (scenario === 'log') {
    if (!sources.length) { console.log('FAIL  no stream to fire a log event at'); process.exit(1); }
    sources[0].fire('log', JSON.stringify({ text: '[1/12] probe starting' }));
    await wait(10);
    const seen = el('log').textContent;
    const ok = seen === '[1/12] probe starting\n';
    console.log(`${ok ? 'PASS' : 'FAIL'}  panel shows ${JSON.stringify(seen)}`);
    process.exit(ok ? 0 : 1);
  }
  const ok = sources.length === 1 && sources[0].url.includes('/api/events?token=TOK');
  console.log(`${ok ? 'PASS' : 'FAIL'}  409 attached ${sources.length} stream(s)`);
  process.exit(ok ? 0 : 1);
})();
"""

NODE = shutil.which("node")
if NODE is None:
    print(
        "SKIP  node is not on PATH, so the page's own JavaScript is not run "
        "(the browser half of the page is unpinned on this host)",
        file=OUT,
    )
else:
    with tempfile.TemporaryDirectory() as _js_tmp:
        _harness = Path(_js_tmp) / "harness.js"
        _harness.write_text(HARNESS, encoding="utf-8")
        _page_js = Path(_js_tmp) / "page.js"
        _page_js.write_text(PAGE_JS, encoding="utf-8")
        for _scenario, _what in (
            ("idle", "a page load with no run attaches no stream"),
            ("running", "a page load during a run attaches to it"),
            ("log", "the panel shows the line, not the JSON envelope"),
            ("busy", "a 409 attaches the page to the run in flight"),
        ):
            try:
                _done = subprocess.run(
                    [NODE, str(_harness), _scenario, str(_page_js)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                _code = _done.returncode
                _out = (_done.stdout + _done.stderr).strip().replace("\n", " | ")
            except (OSError, subprocess.SubprocessError) as exc:
                _code, _out = 1, str(exc)
            check(
                f"the page's own JavaScript: {_what}",
                _code == 0,
                _out[:200],
            )

# --- the docs and the CI wiring ---------------------------------------------------------
RUNNING = (REPO / "docs" / "running.md").read_text(encoding="utf-8")
check(
    "docs/running.md documents the subcommand in its own section",
    re.search(r"^## `dyak serve`", RUNNING, flags=re.MULTILINE) is not None,
    "the design puts the section beside ## dyak process",
)
_serve_section = (
    RUNNING.split("## `dyak serve`", 1)[1].split("\n## ", 1)[0]
    if "## `dyak serve`" in RUNNING
    else ""
)
check(
    "the section names the subcommand and every flag it registers",
    "serve" in _serve_section
    and all(flag in _serve_section for flag in ("--root", "--port", "--bind", "--token", "--open")),
    _serve_section[:300],
)
check(
    "and it states the loopback default and the token",
    web.LOOPBACK in _serve_section and "token" in _serve_section,
    "the exposure decision belongs in the docs, not only in the module",
)
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ci_code = "\n".join(line for line in CI.splitlines() if not line.lstrip().startswith("#"))
check(
    "ci.yml runs this suite",
    "tests/test_serve.py" in ci_code,
    "a suite nothing invokes cannot fail (#173)",
)
check(
    "ci.yml still runs every other suite in tests/",
    sorted(set(re.findall(r"tests/test_[A-Za-z0-9_]+\.py", ci_code)))
    == sorted(f"tests/{p.name}" for p in (REPO / "tests").glob("test_*.py")),
    "the claim suite pins this too; a missing suite here means this file broke the wiring",
)

_env.restore(SAVED_ENV)
print(file=OUT)
if failures:
    print(f"FAILED: {len(failures)} -> {failures}", file=OUT)
    raise SystemExit(1)
print("all serve checks passed", file=OUT)
