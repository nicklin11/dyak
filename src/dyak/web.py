"""A local web view of one run: `dyak serve` (#242, M1).

The design is the decision comment on #242. This module is its first milestone: a loopback HTTP
server whose page lists the recordings under `--root`, starts one run, and shows that run's own
output while it goes.

Two things keep M1 from becoming a later milestone by accident, and both are load-bearing:

  * **No event machinery.** `pipeline.run` already takes a text sink (`stream=`, `pipeline.py:503`)
    and threads it into every stage. The server passes a sink that splits what it receives into
    lines and appends them to the run's log; `GET /api/events` forwards those lines verbatim as
    SSE `log` events. Nothing here reads them: no `[n/12]` matching, no stage names, no
    percentages, no per-stage durations. The structured sink is ADR-0003 D1's decision (#244) and
    belongs to M2. The only facts this module states on its own are the ones it holds as values --
    the run started, the run ended, the exit code, the note path.
  * **No second CLI.** The run is `cli.process(...)`, the same function the `process` subcommand
    dispatches to, called with `stream=` set to the sink. The exit-code ladder, the work-dir
    retention policy, the bundle and the vault export are the CLI's, not a copy of them, so a run
    started from the page and a run started from the shell cannot disagree about what happened.

Exposure, named rather than left to implementation (the design's section 2):

  * Loopback only unless `--bind` names another address, and a warning is printed when it does.
  * A per-start token (`secrets.token_urlsafe`), always required, accepted as
    `Authorization: Bearer ...` or `?token=...` -- `EventSource` cannot set headers, so the page
    URL carries it. What the token grants is "process a file under `--root` on this machine while
    the server runs": not shell access, and not a path outside the root.
  * `Host` must be the bind address or loopback, so a DNS-rebinding page cannot reach the server
    under a name of its own; a state-changing request's `Origin`, when it sends one, must be the
    server's own origin.
  * A requested file is resolved and must land under `--root`; a symlink pointing out of the root
    is refused.
  * One run at a time. The page chooses which file, never where anything is written: the bundle
    and the vault come from `Bundle.open` and `resolve_vault`, exactly as they do for the CLI.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import exitcodes as ec

#: The default interface: loopback, so the page is reachable from this machine only. `--bind` is
#: the opt-in for anything else, and it is a decision the design names rather than a default.
LOOPBACK = "127.0.0.1"

#: Hosts that are this machine: the loopback address itself, `localhost` (a browser may render the
#: loopback URL that way), the IPv6 loopback `::1`, and its IPv4-mapped form. `_host_ok` strips
#: the brackets before comparing, so a `Host: [::1]:8765` header matches the `::1` member here --
#: which is why the bracketed spelling is not a member of this set.
LOOPBACK_HOSTS = frozenset({LOOPBACK, "localhost", "::1", "::ffff:127.0.0.1"})

#: The design's port (`#242` section 3.1); any free port works, and `--port 0` asks the OS.
DEFAULT_PORT = 8765

#: What each exit code means to the person reading the page, from the design's section 5 table.
#: The sentences are the design's, not new prose: `«N»` is filled from the run's own audit count
#: and `«owner»` is written as "the owner", which is who the design's placeholder means. The keys
#: are `exitcodes.DESCRIPTIONS`' codes plus nothing -- the suite pins that, so a new code cannot
#: reach the page without a sentence.
EXIT_SENTENCES: dict[int, str] = {
    ec.OK: "Done. The note is in your notes folder.",
    ec.USAGE: "Done, without AI: structure, but no summary.",
    ec.MISSING_DEPENDENCY: "Something this computer needs isn't set up yet (see above).",
    ec.DEPENDENCY_FAILED: (
        "Something the computer talks to stopped answering. Try again; if it repeats, ask the owner."
    ),
    ec.QUALITY_GATE_FAILED: (
        "The video was too blurry, or its sound and picture don't match. "
        "The note was still written."
    ),
    ec.AUDIT_FINDINGS: "The note is written, with «N» lines the checker doesn't trust.",
    ec.ARTEFACT_VERIFICATION_FAILED: (
        "The note's files couldn't be verified. Don't rely on it; ask the owner."
    ),
    ec.INCOMPLETE_NOTE: "The note is unfinished — part of it is missing.",
    ec.LOW_COVERAGE: ("Part of the lecture didn't reach the writer, so the note is incomplete."),
    ec.TRUNCATED_SECTIONS: "One section was cut off mid-sentence.",
    ec.INTERRUPTED: "Stopped. Nothing was lost.",
}

#: The page, as a string constant: one file, inline CSS and JS, no CDN and no build step, so the
#: subcommand works with no network and no package manager (the design's section 1). The single
#: substitution is the exit-code table, which is `EXIT_SENTENCES` above -- one source for the
#: sentences rather than a second copy inside the HTML.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dyak</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 16px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 46rem; padding: 1.5rem; }
 h1 { font-size: 1.4rem; }
 button { font: inherit; padding: .6rem 1rem; border-radius: .4rem; border: 1px solid currentColor;
          background: none; color: inherit; cursor: pointer; }
 button:disabled { opacity: .5; cursor: default; }
 ul { list-style: none; padding: 0; margin: 0; }
 li { padding: .6rem .25rem; border-bottom: 1px solid rgba(128,128,128,.3); cursor: pointer; }
 li.selected { font-weight: 700; }
 .big { font-size: 1.15rem; margin: 1rem 0; }
 .muted { opacity: .75; font-size: .95rem; }
 pre { overflow: auto; max-height: 22rem; padding: .5rem; background: rgba(128,128,128,.12);
       border-radius: .4rem; }
 #note { word-break: break-all; }
</style>
</head>
<body>
<h1>dyak</h1>
<p id="intro">Choose a recording, then press Start. One recording at a time.</p>
<div id="list"><p class="muted">Loading…</p></div>
<p><button id="start" disabled>Start</button></p>
<p class="muted">A 75-minute lecture usually takes 20-40 minutes. You can close this page; the
work continues in the terminal.</p>
<p class="muted">To stop a run, press Ctrl-C in the terminal where <code>dyak serve</code> is
running.</p>
<section id="progress" hidden>
  <p class="big" id="what"></p>
  <p><button id="details">Details</button></p>
  <pre id="log" hidden></pre>
</section>
<section id="done" hidden>
  <p class="big" id="verdict"></p>
  <p id="note" class="muted"></p>
  <p><button id="copy">Copy its location</button> <button id="again">Do it again</button></p>
</section>
<script>
const EXIT = __EXIT_CODES__;
const token = new URLSearchParams(location.search).get('token') || '';
const api = (path, opts) => fetch(path + '?token=' + encodeURIComponent(token),
    Object.assign({headers: {'Authorization': 'Bearer ' + token}}, opts || {}));
let selected = null, started = 0, timer = null, stream = null;

function sentence(code, auditErrors) {
  const text = EXIT[String(code)];
  if (text === undefined) return 'Finished with code ' + code + '.';
  if (text.indexOf('«N»') < 0) return text;
  return text.replace('«N»', auditErrors > 0 ? auditErrors : 'some');
}
function bytes(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}
function pick(name, node) {
  selected = name;
  for (const li of document.querySelectorAll('#list li')) li.classList.remove('selected');
  node.classList.add('selected');
  const start = document.getElementById('start');
  start.disabled = false;
  start.textContent = 'Start «' + name + '»';
}
async function loadFiles() {
  const list = document.getElementById('list');
  try {
    const reply = await api('/api/files');
    if (!reply.ok) { list.textContent = 'Cannot list the recordings (' + reply.status + ').'; return; }
    const data = await reply.json();
    if (!data.files.length) { list.innerHTML = '<p class="muted">No files in this folder.</p>'; return; }
    list.innerHTML = '';
    const ul = document.createElement('ul');
    for (const f of data.files) {
      const li = document.createElement('li');
      li.textContent = f.name + ' — ' + bytes(f.size) + ', ' + f.date;
      li.addEventListener('click', () => pick(f.name, li));
      ul.appendChild(li);
    }
    list.appendChild(ul);
  } catch (err) { list.textContent = 'Cannot list the recordings: ' + err; }
}
function tick() {
  const seconds = Math.floor((Date.now() - started) / 1000);
  const minutes = Math.floor(seconds / 60);
  const elapsed = minutes < 1 ? seconds + ' seconds' : minutes + ' minutes';
  document.getElementById('what').textContent =
      'Working on «' + selected + '». It has been going for ' + elapsed + '.';
}
function watch() {
  document.getElementById('progress').hidden = false;
  document.getElementById('done').hidden = true;
  document.getElementById('log').textContent = '';
  started = Date.now();
  tick();
  clearInterval(timer);
  timer = setInterval(tick, 1000);
  if (stream) stream.close();
  stream = new EventSource('/api/events?token=' + encodeURIComponent(token));
  stream.addEventListener('log', (e) => {
    const log = document.getElementById('log');
    const d = JSON.parse(e.data);
    log.textContent += (d.text ?? '') + '\\n';
    log.scrollTop = log.scrollHeight;
  });
  stream.addEventListener('exit', (e) => finish(JSON.parse(e.data)));
}
async function finish(result) {
  clearInterval(timer);
  if (stream) { stream.close(); stream = null; }
  document.getElementById('progress').hidden = true;
  document.getElementById('done').hidden = false;
  let data = result;
  try { data = await (await api('/api/result')).json(); } catch (err) { /* the event's copy */ }
  const auditErrors = data.audit_errors === undefined ? result.audit_errors : data.audit_errors;
  document.getElementById('verdict').textContent = data.error
      ? 'Something went wrong inside dyak. Open Details and show it to the owner.'
      : sentence(data.code, auditErrors);
  document.getElementById('note').textContent = data.note ? 'Note: ' + data.note : '';
  const again = document.getElementById('again');
  again.disabled = false;
}
async function start() {
  const reply = await api('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: selected}),
  });
  if (reply.status === 409) {
    const data = await reply.json();
    document.getElementById('progress').hidden = false;
    document.getElementById('what').textContent = data.error;
    if (data.name) { selected = data.name; watch(); }
    return;
  }
  if (!reply.ok) {
    document.getElementById('progress').hidden = false;
    document.getElementById('what').textContent = 'Cannot start: ' + reply.status + '.';
    return;
  }
  watch();
}
document.getElementById('start').addEventListener('click', start);
document.getElementById('again').addEventListener('click', start);
document.getElementById('details').addEventListener('click', () => {
  const log = document.getElementById('log');
  log.hidden = !log.hidden;
});
document.getElementById('copy').addEventListener('click', () => {
  const text = document.getElementById('note').textContent.replace(/^Note: /, '');
  if (text) navigator.clipboard.writeText(text);
});
(async () => {
  await loadFiles();
  try {
    const data = await (await api('/api/result')).json();
    if (data.running && data.name) {
      // A reload while a run is in flight: the run belongs to the server, so the page attaches
      // to it instead of showing an idle screen. `watch()` opens the stream at cursor 0, and
      // the server replays the run's log, so the panel fills in from the beginning.
      selected = data.name;
      document.getElementById('start').textContent = 'Start «' + data.name + '»';
      watch();
    } else if (data.code !== null && data.code !== undefined) {
      finish(data);
    }
  } catch (err) { /* no run yet */ }
})();
</script>
</body>
</html>
"""

PAGE = _PAGE.replace("__EXIT_CODES__", json.dumps(EXIT_SENTENCES))


def exposure_warning(bind: str) -> str | None:
    """None on loopback; on any other address, what a peer that reaches this port can do.

    A warning is not a mitigation, and the design says so: the token is in the page URL, so
    anything that can reach this interface can list the recordings under `--root` and start a run
    on one. The tailnet is the supported remote case (`--bind <tailnet-ip>`); a LAN or the open
    internet is not a supported configuration, only an available one.
    """
    if bind in LOOPBACK_HOSTS:
        return None
    return (
        f"--bind {bind} is not loopback: anything that can reach this port can list the "
        "recordings under --root and start a run on one, because the token travels in the page "
        "URL. The tailnet is the supported case; a LAN or the internet is not. Stop the server "
        "when you are done."
    )


def contained(root: Path, requested: str) -> Path | None:
    """`requested` resolved to a regular file under `root`, or None if it is not one.

    `resolve()` is the whole containment check, because it follows symlinks: a link inside the
    root that points outside it resolves outside it and is refused. An absolute request is
    resolved too, so `/etc/passwd` fails here rather than at ffprobe.
    """
    try:
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError is a NUL byte in the name: `Path.resolve()` raises it instead of opening
        # anything. The name is untrusted input from a request, so it is a refusal like any other
        # -- 400 with the reason, not a 500 and not a traceback in the server's log.
        return None
    if root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


class Sink:
    """`pipeline.run`'s `stream=`: complete lines in, the run's log out, nothing interpreted.

    The pipeline writes stage lines and continuation lines through one sink (`pipeline.py:503`),
    so this is the same text the shell would print. It is split on newlines only because the SSE
    framing needs whole lines; no line is edited, matched or measured on the way.
    """

    def __init__(self, log: RunLog) -> None:
        self._log = log
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._log.add(line)
        return len(text)

    def flush(self) -> None:
        if self._partial:
            self._log.add(self._partial)
            self._partial = ""

    def isatty(self) -> bool:
        """False: nothing here should decide to colour, paginate or re-wrap for a terminal."""
        return False


class RunLog:
    """The lines a run wrote, plus the events the server holds as values (the design's section 4).

    A list with a cursor per reader rather than one shared `queue.Queue`: the page may be reloaded
    mid-run, and a queue would have handed the second reader only the lines the first one had not
    already taken. Everything the server *knows* -- that a run started, that it ended, its exit
    code, its note -- is an event; everything else is forwarded text.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[dict] = []

    def add(self, text: str) -> None:
        self.event("log", text=text)

    def event(self, kind: str, **fields: object) -> None:
        """`kind` is the SSE event name; the fields are its JSON payload.

        The parameter is not called `name` because `Run.as_json` has a `name` of its own: a
        keyword argument would collide with it (`TypeError: got multiple values`), and the run's
        name is the one the page shows.
        """
        with self._condition:
            self._events.append({"event": kind, "fields": fields})
            self._condition.notify_all()

    def wait(self, cursor: int, timeout: float = 15.0) -> tuple[list[dict], int]:
        """Every event after `cursor`, waiting up to `timeout` for one to exist."""
        with self._condition:
            if cursor >= len(self._events):
                self._condition.wait(timeout)
            events = self._events[cursor:]
            return events, cursor + len(events)


class Run:
    """One run: what the page asked for, what the CLI decided, and where the note landed."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        self.started = time.time()
        self.finished: float | None = None
        self.code: int | None = None
        self.error: str | None = None
        self.note: str | None = None
        self.bundle: str | None = None
        self.audit_errors = 0
        self.header: list[str] = []
        self.result: object | None = None

    @property
    def done(self) -> bool:
        """Finished, whether with an exit code or with an internal error and no code at all.

        `code is None` is not "still running": `exitcodes.py:61-63` records that an uncaught
        internal error deliberately has no code, and a run whose thread died must not leave the
        page saying "working on it" forever.
        """
        return self.finished is not None

    def remember(self, result) -> None:
        """Called by `cli.process` with the `RunResult`, before the vault export."""
        self.result = result
        self.note = str(result.final_note)
        self.bundle = str(result.bundle.root)
        self.audit_errors = len([f for f in result.audit.findings if f.severity == "ERROR"])
        self.header = [
            f"source {result.source}",
            f"transcript {result.transcript.summary()}",
            f"stages {result.seconds:.1f}s",
            f"output {result.bundle.root}",
        ]

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "error": self.error,
            "note": self.note,
            "bundle": self.bundle,
            "audit_errors": self.audit_errors,
            "header": self.header,
            "running": not self.done,
        }


class State:
    """The server's one piece of shared state: the root, the token, the log, the current run."""

    def __init__(self, root: Path, token: str, bind: str) -> None:
        self.root = Path(root).resolve()
        self.token = token
        self.bind = bind
        self.log = RunLog()
        self.port = 0
        self._guard = threading.Lock()
        self.run: Run | None = None

    def busy_with(self) -> str | None:
        """The name of the run in progress, or None. One run at a time, named when refused."""
        with self._guard:
            if self.run is not None and not self.run.done:
                return self.run.name
            return None

    def start(self, name: str, path: Path) -> Run | None:
        with self._guard:
            if self.run is not None and not self.run.done:
                return None
            self.run = Run(name=name, path=path)
            return self.run

    def finish(self, code: int | None, error: str | None = None) -> None:
        with self._guard:
            run = self.run
            if run is None:
                return
            run.code = code
            run.error = error
            run.finished = time.time()
        self.log.event("exit", **run.as_json())


def process_args(path: Path, *, no_llm: bool = False) -> argparse.Namespace:
    """The `process` arguments this run will use, built by the CLI's own parser.

    Hand-building a Namespace would be a second copy of the flags and their defaults, and would
    silently stop matching the CLI the first time a flag is added. Asking the parser cannot drift.
    """
    from . import cli as dyc

    argv = ["process", str(path)]
    if no_llm:
        argv.append("--no-llm")
    return dyc.build_parser().parse_args(argv)


def run_once(state: State, run: Run, *, no_llm: bool = False) -> None:
    """Run the pipeline in this thread, forwarding the sink, and record the exit code.

    `cli.process` is the CLI's own flow -- preflight, work dir, bundle, pipeline, vault export and
    the exit-code ladder -- called with `stream=` set to the sink, so the page shows the run's own
    output and reports the same code the shell would. Its own messages go to the same log, because
    they are the "technical lines" the page keeps in Details.

    `sys.stdout`/`sys.stderr` are redirected for the duration: they are process-wide, so only one
    run may be in flight, which is the same one-run-at-a-time rule the lock enforces. The handler
    silences its access log for the same reason -- it would otherwise land in the run's log.
    """
    from . import cli as dyc

    sink = Sink(state.log)
    code: int | None = None
    error: str | None = None
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            code = dyc.process(
                process_args(run.path, no_llm=no_llm), stream=sink, on_result=run.remember
            )
    except BaseException as exc:
        # A bug in dyak, or a SystemExit from somewhere below. Reported rather than swallowed:
        # the log gets the exception, the page gets `code: null`, and the run stops being "busy".
        error = f"{type(exc).__name__}: {exc}"
        sink.write(f"dyak: internal error: {error}\n")
    finally:
        sink.flush()
    state.finish(code, error)


class Handler(BaseHTTPRequestHandler):
    """The five endpoints, and the token, Host, Origin and containment checks in front of them."""

    server_version = "dyak-serve"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        """No access log: it would write the request line, token included, into the run's log."""

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - the base class's spelling
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._authorized(query):
            return self._refuse(401, "the token is missing or wrong")
        if not self._host_ok():
            return self._refuse(403, "this Host header is not the address dyak is bound to")
        if parts.path == "/":
            return self._send_html(PAGE)
        if parts.path == "/api/files":
            return self._send_json({"files": self._files()})
        if parts.path == "/api/events":
            return self._events()
        if parts.path == "/api/result":
            run = self.state.run
            return self._send_json(run.as_json() if run else {"code": None, "note": None})
        return self._refuse(404, "no such path")

    def do_POST(self) -> None:  # noqa: N802 - the base class's spelling
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._authorized(query):
            return self._refuse(401, "the token is missing or wrong")
        if not self._host_ok():
            return self._refuse(403, "this Host header is not the address dyak is bound to")
        if not self._origin_ok():
            return self._refuse(403, "this Origin is not the page's own")
        if parts.path != "/api/run":
            return self._refuse(404, "no such path")
        return self._run()

    # --- the checks ---------------------------------------------------------------

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        supplied = (query.get("token") or [""])[0]
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = supplied or header[len("Bearer ") :]
        return secrets.compare_digest(supplied, self.state.token)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.strip("[]") in {LOOPBACK, "localhost", "::1", self.state.bind.strip("[]")}

    def _origin_ok(self) -> bool:
        """A state-changing request that names an Origin must name this server's own.

        An absent Origin is a command-line client -- curl, the suite -- which the token already
        gates. A present one that differs is another page trying to drive this server (CSRF).
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in (f"http://{host}", f"https://{host}")

    # --- the endpoints ------------------------------------------------------------

    def _files(self) -> list[dict]:
        """Every regular file under the root, by name, size and date -- never by path.

        The extension is not filtered here: the pipeline is the thing that decides whether a file
        is a recording, and it says so with a named reason (`probe.require_supported`,
        `probe.py:270`). A second list of "supported extensions" in this module could only drift
        from it, and would hide from the page the very error the run reports.
        """
        files = []
        try:
            entries = sorted(self.state.root.iterdir())
        except OSError:
            return files
        for entry in entries:
            if entry.is_symlink() and contained(self.state.root, str(entry)) is None:
                continue
            if not entry.is_file():
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": entry.name,
                    "size": info.st_size,
                    "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime)),
                }
            )
        return files

    def _events(self) -> None:
        """Server-sent events: the run's lines verbatim, and the server's own facts as events.

        The stream carries the same headers as every other response -- `no-store`, `no-referrer`
        and the policy -- rather than a subset of them: a claim that "every response carries
        them" is only true if the one long-lived response does too, and the token is in this
        stream's URL.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self._security_headers()
        self.end_headers()
        cursor = 0
        try:
            while True:
                events, cursor = self.state.log.wait(cursor)
                if not events:
                    # A comment frame: it keeps a proxy or a browser from closing an idle stream
                    # during a stage that prints nothing for minutes.
                    self.wfile.write(b": keepalive\n\n")
                for event in events:
                    payload = json.dumps(event["fields"])
                    frame = f"event: {event['event']}\ndata: {payload}\n\n".encode()
                    self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The page was closed or reloaded. Not an error: the run continues, which is the
            # design's promise that closing the page does not stop the work.
            return

    def _run(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._refuse(400, "bad Content-Length")
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._refuse(400, "the request body is not JSON")
        if not isinstance(request, dict):
            return self._refuse(400, "the request body is not an object")
        name = request.get("name")
        if not isinstance(name, str) or not name:
            return self._refuse(400, "name the file to process")
        busy = self.state.busy_with()
        if busy is not None:
            return self._send_json(
                {"error": f"already working on «{busy}»", "name": busy}, status=409
            )
        path = contained(self.state.root, name)
        if path is None:
            return self._refuse(400, f"{name} is not a file under the served folder")
        run = self.state.start(path.name, path)
        if run is None:
            # Lost the race: a run started between the check above and this call. Name that run
            # rather than saying "a lecture", so the page's message -- and the name it uses to
            # attach to the run in flight -- is true on both 409 paths.
            current = self.state.run
            held = current.name if current is not None else path.name
            return self._send_json(
                {"error": f"already working on «{held}»", "name": held}, status=409
            )
        no_llm = bool(request.get("no_llm"))
        self.state.log.event("run_start", name=run.name, no_llm=no_llm)
        threading.Thread(
            target=run_once, args=(self.state, run), kwargs={"no_llm": no_llm}, daemon=True
        ).start()
        return self._send_json({"started": True, "name": run.name})

    # --- responses ----------------------------------------------------------------

    def _send_html(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, status: int, why: str) -> None:
        self._send_json({"error": why}, status=status)

    def _security_headers(self) -> None:
        """No store, no referrer, and a policy that names the page's only sources.

        The token is in the URL, so it must not reach another origin: `no-referrer` keeps it out
        of a Referer header and `no-store` keeps the page out of a cache. The policy says what the
        page may load -- itself, and inline styles and scripts -- so a CDN reference could not be
        added to the page without failing the suite's own check on this constant.
        """
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'",
        )


class Server(ThreadingHTTPServer):
    """The threaded server, holding the shared state and closing its threads on Ctrl-C."""

    daemon_threads = True
    state: State


def make_server(
    root: Path, *, token: str, bind: str = LOOPBACK, port: int = DEFAULT_PORT
) -> tuple[Server, State]:
    """The server and its state, not yet serving. `port=0` asks the OS for a free port."""
    state = State(root=root, token=token, bind=bind)
    httpd = Server((bind, port), Handler)
    httpd.state = state
    state.port = httpd.server_address[1]
    return httpd, state


def serve(args: argparse.Namespace) -> int:
    """The `serve` subcommand: bind, print the URL with its token, and answer until Ctrl-C."""
    root = Path(args.root).expanduser() if args.root else Path.cwd()
    if not root.is_dir():
        print(f"dyak: --root is not a directory: {root}", file=sys.stderr)
        print(
            "dyak:   remediation: pass --root DIR naming the folder that holds the recordings",
            file=sys.stderr,
        )
        return ec.USAGE
    token = args.token or secrets.token_urlsafe(16)
    try:
        httpd, state = make_server(root, token=token, bind=args.bind, port=args.port)
    except OSError as exc:
        print(f"dyak: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        print(
            "dyak:   remediation: another process may hold that port; pass --port N, or --bind "
            "ADDR to choose the interface",
            file=sys.stderr,
        )
        return ec.MISSING_DEPENDENCY

    # 0.0.0.0 and :: are listen addresses, not addresses a browser can open.
    shown = LOOPBACK if args.bind in ("0.0.0.0", "::") else args.bind
    url = f"http://{shown}:{state.port}/?token={token}"
    print(f"dyak: serving {state.root} to this machine on {args.bind}:{state.port}")
    print(f"dyak:   open {url}")
    warning = exposure_warning(args.bind)
    if warning is not None:
        print(f"dyak: {warning}", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("dyak: stopped.")
    finally:
        httpd.server_close()
    return ec.OK
