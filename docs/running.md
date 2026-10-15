# Running dyak — configuration, flags, subcommands

This file is the full reference. [`tests/test_claims.py`](../tests/test_claims.py) checks that
every `DYAK_*` variable the code reads and every flag and subcommand `cli.py` registers
appears here, so the table cannot rot silently.

Three sources of configuration, in the order the code consults them:

1. an explicit CLI flag (`--vault-path`, `--terms-dir`, ...)
2. the corresponding `DYAK_*` environment variable
3. the settings file (`$DYAK_SETTINGS`, else `$XDG_CONFIG_HOME/dyak/settings.toml`)

and then the built-in default. One function, `settings.resolve`, decides that order; no call
site reads an environment variable for a setting itself. The result records which layer won
(`flag` / `env` / `file` / `default`), which is what lets a warning or `dyak doctor` name the
source rather than re-derive it.

The settings file is a real third layer. Keys are **flat**, and a key's name is the
environment variable lowercased:

```toml
vault = "/path/to/vault"
stt = "whispercpp"
whispercpp_url = "http://127.0.0.1:8080"
llm_endpoint = "http://127.0.0.1:10300/v1"
llm_model = "qwen3.6-35b"
openai_max_upload_bytes = 26214400
```

Every run-affecting `DYAK_*` variable in the table below can be set this way — `llm_endpoint`
is settable in the file exactly as `DYAK_LLM_ENDPOINT` is in the environment. A key whose
setting is a number in the environment and in the built-in default — currently only
`openai_max_upload_bytes` — is written bare in the file, as in the block above; a quoted count
also works, so an existing file does not break. `DYAK_SETTINGS`
is the one exception: it names the file being read, so the file cannot carry it. TOML tables
(`[llm]`) are not supported; a table is dropped with a warning rather than read as a nested
setting. A key the code does not define — including a typo like `llm_endpont` — is reported on
stderr and not honoured, so a misspelled key fails loudly instead of falling back to a default
in silence. The file is written on a first run with nothing configured.

Precedence is the resolver's, tracked in
[#52](https://github.com/nicklin11/dyak/issues/52). The `dyak config set|get|list` subcommand
that ADR-0004 D2 describes is **not implemented**; editing the file by hand is the supported
path.

## Process: environment variables

| variable | read by | purpose, default |
|---|---|---|
| `DYAK_STT` | `stt/backends.py` | backend selection: `auto` (default), `whispercpp`, `openai`. `auto` probes whisper-cpp first, so with both configured the local one keeps the recording on the host; neither configured or reachable exits 2 |
| `DYAK_WHISPERCPP_URL` | `stt/whispercpp.py` | whisper.cpp `POST /inference` endpoint. **No built-in default** (#95): unset means "not configured" (exit 2). Point it at the address your whisper.cpp server listens on, e.g. `http://127.0.0.1:8080`; `DYAK_OPENAI_URL` is the hosted alternative |
| `DYAK_WHISPERCPP_LANGUAGE` | `stt/whispercpp.py` | pin the language; unset means autodetect |
| `DYAK_OPENAI_URL` | `stt/openai_compat.py` | hosted STT endpoint (OpenAI-compatible `/v1/audio/transcriptions`). **No built-in default** (#95): unset means "not configured", not the whisper.cpp port. Both shapes are accepted: `http://127.0.0.1:8080` *or* `http://127.0.0.1:8080/v1` — the probe hits `GET /v1/models` and the transcription `POST /v1/audio/transcriptions` either way, never `/v1/v1/...` (#322) |
| `DYAK_OPENAI_KEY` | `stt/openai_compat.py` | key for the hosted STT endpoint |
| `DYAK_OPENAI_MODEL` | `stt/openai_compat.py` | model id; default `whisper-1` |
| `DYAK_OPENAI_MAX_UPLOAD_BYTES` | `stt/openai_compat.py` | per-upload ceiling of the hosted STT route, in bytes; a longer wav is sliced into chunks below it and their word timings spliced, with the sample bytes copied verbatim. Default `26214400` (25 MiB, the documented hosted limit). Bytes, not a unit-suffixed string: `25M` would have to guess between 25,000,000 and 26,214,400, and a wrong guess is the request that exceeds the limit. In the settings file this is written bare — `openai_max_upload_bytes = 26214400`; a quoted count also works, and a unit-suffixed string is exit 1 naming the setting |
| `DYAK_LLM_ENDPOINT` | `llm.py`, `deps.py` | OpenAI-compatible base URL for synthesis (stage 7) and audit (stage 9) |
| `DYAK_LLM_MODEL` | `llm.py`, `deps.py` | model id sent in the request |
| `DYAK_LLM_KEY` | `llm.py` | optional; unset means no `Authorization` header |
| `DYAK_LLM_CONCURRENCY` | `llm.py`, `audit.py`, `synth.py` | section requests stages 7 and 9 may have in flight at once; default `1`, and a value that is not a positive whole number is exit 1 naming the variable. Default is 1 because the endpoint this is built against is a local gateway: one model behind one URL serves concurrent requests by queueing them, so the knob buys wall clock only where the provider does (#153). Above 1 every section is sent, and stage 9's finding cap changes meaning — at 1 it stops requests, above 1 it becomes a report limit on the collected findings (`cap_scope`/`cap_dropped` in `audit.json`, and the count in the run line); the two readings do not produce the same audit. Measured on the tailnet gateway `http://100.64.0.2:8317/v1` (`opencode-go/glm-5.3-flash`) with stage 9's 8-section fixture at `limit=99`, so neither arm is capped: **8.2 s at 1, 3.6 s at 4** (medians of three runs, 2.3×), while the seconds the endpoint itself reported went from 8.0 to 10.2 — the gateway queues what it is sent, and the wall clock is what the run pays |
| `DYAK_VLM_ENDPOINT` | `llm.py`, `deps.py` | stage 6 frame captioning; unset falls back to `DYAK_LLM_ENDPOINT` |
| `DYAK_VLM_MODEL` | `llm.py`, `deps.py` | as above, falls back to `DYAK_LLM_MODEL` |
| `DYAK_VLM_KEY` | `llm.py`, `deps.py` | as above, falls back to `DYAK_LLM_KEY` |
| `DYAK_VAULT` | `deps.py` | the notes directory the note is exported into (any folder; Obsidian is recommended) |
| `DYAK_SETTINGS` | `settings.py` | the settings *file*, overriding the XDG path; the one variable the settings file cannot carry |
| `DYAK_TERMS_DIR` | `link.py` | atomic term notes for stage 11; default `<vault>/mscs/_terms` |
| `DYAK_OUTPUT_DIR` | `bundle.py` | where the bundle goes; default `$XDG_STATE_HOME/dyak/<lecture>`, or `~/.local/state/dyak/<lecture>` when `XDG_STATE_HOME` is unset |
| `DYAK_WORKDIR` | `workspace.py` | parent of the managed work dir (scratch: `audio.wav` etc.); default `$TMPDIR` |
| `DYAK_BBOX_SOURCE` | `pipeline.py` | stage 5 estimator name; the registry holds `geometric` (default) and `null` (`quality.ESTIMATORS`) |
| `DYAK_SERIAL` | `pipeline.py` | run the two pre-caption branches — stages 2-3 (`audio`→`stt`) and stages 4-5 (`frames`→`quality`) — one after the other instead of concurrently (#115). Unset, or `0`/`false`/`no`/`off`, is concurrent, which is the default and what a real run does; `1`/`true`/`yes`/`on` is sequential, for a control arm and for a host where the two branches contend for one resource. Any other value is exit 1 naming the variable: a kill-switch must not read a typo as "off" |

Every row above except `DYAK_SETTINGS` is also a flat key in the settings file — the variable
name lowercased (`llm_endpoint`, `whispercpp_url`, ...) — and is read through `settings.resolve`
in the module named here.

`DYAK_GATEWAY_URL` is not read anywhere: the check that read it was removed (see the
comment in `deps.py`). Standard Python/XDG variables — `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`TMPDIR` — are also consulted, each with a documented fallback.

### Every honoured key and its default

The table above says what each variable does; this one says what the resolver does when nothing
supplies a value — no flag, no environment variable, no settings-file key. It is the code's own
answer, not a transcription: the set is `settings.HONOURED_SETTINGS` (derived in `settings.py`
from its own `DYAK_*` constants), the flat key is `settings.settings_key`, and the default is the
`default=` argument at that key's `resolve()` call site, evaluated in the module that passes it.
`tests/test_claims.py` re-derives all three and fails if a row and the code disagree, so a new
setting cannot be declared without a row here.

`DYAK_SETTINGS` is the one honoured variable with no row: it names the file being read, so the
file cannot carry it. `none` means the resolver returns no value at all, which for several keys is
the "not configured" state described above (the STT, LLM and VLM URLs); where a path is composed
anyway — `output_dir` from `$XDG_STATE_HOME/dyak/<lecture>`, `terms_dir` from
`<vault>/mscs/_terms`, the VLM pair from the LLM pair — that happens in the stage that needs it,
after the resolver has said `none`. A default that is a path on the machine running the tool is
written `$HOME`/`$TMPDIR` here, because this document has to read the same on another machine.

| environment variable | settings-file key | built-in default |
|---|---|---|
| `DYAK_VAULT` | `vault` | `$HOME/Documents/obs_notes` |
| `DYAK_TERMS_DIR` | `terms_dir` | none |
| `DYAK_WORKDIR` | `workdir` | `$TMPDIR` |
| `DYAK_OUTPUT_DIR` | `output_dir` | none |
| `DYAK_BBOX_SOURCE` | `bbox_source` | `geometric` |
| `DYAK_SERIAL` | `serial` | `0` |
| `DYAK_STT` | `stt` | `auto` |
| `DYAK_WHISPERCPP_URL` | `whispercpp_url` | none |
| `DYAK_WHISPERCPP_LANGUAGE` | `whispercpp_language` | none |
| `DYAK_OPENAI_URL` | `openai_url` | none |
| `DYAK_OPENAI_KEY` | `openai_key` | none |
| `DYAK_OPENAI_MODEL` | `openai_model` | `whisper-1` |
| `DYAK_OPENAI_MAX_UPLOAD_BYTES` | `openai_max_upload_bytes` | `26214400` |
| `DYAK_LLM_ENDPOINT` | `llm_endpoint` | none |
| `DYAK_LLM_MODEL` | `llm_model` | none |
| `DYAK_LLM_KEY` | `llm_key` | none |
| `DYAK_LLM_CONCURRENCY` | `llm_concurrency` | `1` |
| `DYAK_VLM_ENDPOINT` | `vlm_endpoint` | none |
| `DYAK_VLM_MODEL` | `vlm_model` | none |
| `DYAK_VLM_KEY` | `vlm_key` | none |

## `dyak doctor`

Reports missing or conflicting dependencies and exits 2/3 with the reason and remediation.
Run it before a run, not after a failure. Both model roles are probed: the text endpoint, and
the vision endpoint stage 6 captions with when `DYAK_VLM_*` is set. An unset vision role is
reported, not failed — stage 6 falls back to the text pair.

## `dyak settings`

```
dyak settings [--vault-path DIR] [--workdir DIR] [--output-dir DIR] [--terms-dir DIR]
```

Prints every key the settings file honours, the value in effect, the layer that supplied it
(`flag > env > file > default`) and the built-in default. API keys are redacted. A listing is
not a failure: it exits 0.

The flags resolve one key each, as if the run's own flag had been passed, so the value it shows
is the one a run would get:

| flag | resolves |
|---|---|
| `--vault-path DIR` | the `vault` key |
| `--workdir DIR` | the `workdir` key |
| `--output-dir DIR` | the `output_dir` key |
| `--terms-dir DIR` | the `terms_dir` key |

## `dyak process`

```
dyak process [path] [options]
```

| flag | meaning |
|---|---|
| `path` | video or audio file |
| `--workdir DIR` | parent directory for the managed work dir (default: `$TMPDIR`, or `DYAK_WORKDIR`) |
| `--output-dir DIR` | where to write the output bundle (default: `$XDG_STATE_HOME/dyak/<lecture>`, or `~/.local/state/dyak/<lecture>` if unset) |
| `--overwrite` | empty an existing output bundle's root before the run starts. Its one observable difference today: without it, `pipeline.run` copies the previous run's `captions.json` aside before the root clear (`pipeline.py:407-409`), and stage 6 reuses those captions; with it the root is already empty, so stage 6 re-captions every frame. Everything else the flag does, the run's own clear does anyway |
| `--vault-path DIR` | copy the finished bundle into this vault; never a pipeline blocker. Defaults to the configured vault, and the run writes into it either way — use `--no-vault-export` to keep the note in the bundle only |
| `--no-vault-export` | finish the bundle without copying it into the vault |
| `--vault-images` | also copy `images/` into the vault. Off by default: the frames are most of the bytes (50 of 59 MiB on lecture 1) and the note refers to none of them, so they stay in the bundle as the pipeline's evidence |
| `--vault-subdir REL` | where inside the vault the bundle lands, relative to it (default: `<vault>/dyak/<lecture>/`, one directory per lecture, #118). Not optional in practice: a bundle is ~188 files, and the vault root holds the user's own notes |
| `--force-export` | replace a vault note that was edited by hand after the last export. Default: refuse, and name the file (#118) |
| `--no-llm` | run without an LLM endpoint: the note is the template skeleton, not a model's. Without this flag `process` refuses to start when `DYAK_LLM_ENDPOINT` is unset or unreachable (#126) |
| `--keep-workdir` | keep the work dir even when the run succeeds |
| `--glossary FILE` | ASR term glossary for stages 9 and 10, in the `\| variant \| canonical \| domain \| date \|` format. Without it the term-drift rule does not run, so a clean report does not mean the note's terms were checked: `audit-provenance.json` records `"glossary": null` when no path was supplied and the `{"path", "is_file", "entries", "error"}` state otherwise, `repair-provenance.json` records the same object, and the stage names the reason on its own line |
| `--terms-dir DIR` | atomic term notes for stage 11. Default: `$DYAK_TERMS_DIR`, the `terms_dir` settings key, then `<vault-path>/mscs/_terms`. Without it the note is written unlinked and the stage says so |

### What the progress output looks like

One line per stage, printed when the stage finishes, with the seconds it took. Four stages also
write *continuation* lines while they work: stage 3 announces itself before the transcription
begins and, on the hosted route, prints an indented `chunks k/N` after each upload returns
(#193); stages 6, 7 and 9 print an indented `frames k/N` or `sections k/N` before each unit's
blocking call (#197). The count is the units **examined** so far — a cached, capped or skipped
unit still advances it, so it can pass a stage's own "reviewed" figure — and it carries no clock,
because no rate has been measured for the vision or text endpoint. Stages 2-5 are two branches —
`audio`→`stt` and `frames`→`quality` — joined by stage 6 (#115), so their lines can arrive out
of stage order and each of them names the branch that wrote it, `(A)` or `(B)`. `DYAK_SERIAL=1`
puts the two branches back on one thread in stage order, and then there is no tag to print. With
`DYAK_LLM_CONCURRENCY > 1` stages 7 and 9 still print `sections k/N` in section order: the lines
are written where the requests are dispatched, on the calling thread, so the counter cannot
interleave — and with four calls in flight it runs several units ahead of the replies instead of
one behind each of them (#153). This is a real run — 180 s of lecture 1, seven frames — with the
stages that print detail trimmed:

```
  [1/12] probe       0.0s  video h264 1920x1080, audio aac 48000Hz 2ch, 180.0s
  [2/12] audio       0.2s  audio.wav 180.0s 16000Hz mono, 5.5 MiB  (A)
  [3/12] stt     starting  180s of audio through whispercpp at http://127.0.0.1:8080, expect 36s-54s; this backend reports no progress  (A)
  [3/12] stt         9.1s  52 segments, 611 timed words, speech to 180.0s of 180.0s audio [whispercpp]  (A)
  [4/12] frames     12.5s  7 frames, 2.7 MiB, median gap 9s  (B)
  [5/12] quality     3.7s  7/7 frames pass (MEGE 57230-146128, threshold 38303, 7 cropped)  (B)
           frames 0/7
  [6/12] caption   119.5s  7 frames aligned, 493 words, 0 without transcript coverage
           sections 0/8
  [7/12] synth     327.7s  llm, 66 lines, 7/8 sections filled  DEGRADED
  [8/12] lint        0.0s  9 checks, clean
           sections 0/8
  [9/12] audit     752.1s  4 rules, 0 errors, 7 warnings, 4 discarded, tier 2 ran, 7 findings, 4 discarded, 5/8 sections reviewed (capped at 6 findings)
  [10/12] repair      0.0s  nothing to repair
  [11/12] link        0.0s  34 terms, inserted 2 links across 1 files
  [12/12] report OK: 8 verified, 0 optional absent, frames 7/7
```

Four things about that shape are worth knowing:

- **`[3/12] stt starting` is not a duplicate.** It appears before the transcription begins, and
  has no seconds because nothing has elapsed yet. Every other stage prints exactly one line, but
  four of them also print indented continuation lines underneath: `chunks k/N` on the hosted STT
  route, `frames k/N` in stage 6 and `sections k/N` in stages 7 and 9, written before each
  unit's call. Those are not stage lines either, and only each loop's first line is shown above.
- **The gap between lines is the stage's real cost.** Stage 9 above took 752 s of a 1225 s run.
  On a full 75-minute lecture the same run spends most of its wall clock in transcription,
  frame extraction, synthesis and audit, in that order.
- **The `(A)`/`(B)` tag is the branch, not the stage.** `(A)` is `audio`/`stt`, `(B)` is
  `frames`/`quality`; stage 6 is the join and prints untagged, as do stage 1 and stages 7-12.
  The tag is appended to the line the pipeline already wrote, continuation lines included, so a
  log whose stages finish out of order stays readable: in the sample above the order is 1, 2,
  3-start, 4, 5, 3-end, 6 — `frames` and `quality` finished while `stt` was still transcribing,
  which is the point of the change rather than a bug in the numbering. Serial runs print the
  stages in numerical order and no tag at all.
- **Stages that produce findings print them underneath**, indented, as they finish — caption
  counts, audit warnings, repaired lines, linked terms. Those are not stage lines and are not
  counted as such.

The run then closes with the transcript summary, the total, and the bundle path.

### Redirecting the log

```
dyak process lecture.mp4 > run.log 2>&1
```

`run.log` is live. Every stage line, continuation counter line, per-frame caption line and audit
finding reaches the file before the next stage begins — the streams are reconfigured line-buffered
at the entrypoint, so `tail -f run.log` tracks the run instead of showing nothing until it exits.

This was not true until #100, and the failure was worth recording. CPython block-buffers
`sys.stdout` in 8192-byte chunks when fd 1 is not a tty, and the pipeline flushed exactly once,
immediately before stage 3. Run 14 sat at 266 bytes for 16 minutes with its last line reading
`[3/12] stt starting`, while the run was in fact in stage 8 with its only socket open to the LLM
gateway. The obvious reading of that log is a 16-minute transcription stall, and what disproved
it was the bundle's file mtimes rather than the log itself.

## `dyak serve`

```
dyak serve [--root DIR] [--port N] [--bind ADDR] [--token TOKEN] [--open]
```

Serves a local page that lists the recordings under `--root`, runs one at a time through the
same code `process` runs — the same exit-code ladder, work dir, bundle and vault export — and
shows that run's own output while it goes. This is M1 of #242: there is no Stop button (stop a
run with Ctrl-C in the terminal, and the page says so), no readiness screen and no math
rendering yet.

| flag | meaning |
|---|---|
| `--root DIR` | the folder the page may list and process (default: the current directory). A requested file must resolve inside it, symlinks included, so a link pointing out of the folder is refused rather than followed |
| `--port N` | TCP port to listen on (default: `8765`). `--port 0` asks the OS for a free one |
| `--bind ADDR` | the interface to listen on (default: `127.0.0.1`, loopback only). Any other address — a tailnet address included — exposes the page to whatever can reach that interface, and a warning is printed saying so. IPv4 only: the server socket is `AF_INET`, so an IPv6 address fails to bind and the subcommand exits 2 |
| `--token TOKEN` | pin the token instead of generating one per start, so the page URL can be bookmarked |
| `--open` | open the page in this machine's browser once the server is listening |

Every request must present the per-start token, as `Authorization: Bearer …` or as `?token=…`
(the page's own URL carries it, because `EventSource` cannot set a header), and every request
must arrive under a `Host` this machine answers to. There is no anonymous endpoint and no
request that reaches outside `--root`. The token is a capability rather than a secret: while the
server runs, whoever holds it can process a file under `--root` on this machine.

The page keeps the run's lines in a Details panel, exactly as the terminal prints them, and
shows the sentence that belongs to the run's exit code — the same eleven codes `process` uses
(`exitcodes.py`), never a new one and never a percentage the page worked out for itself. Closing
the page does not stop the run: the run is in the server's process, and a reloaded page
reattaches to it.

## `dyak audit`

```
dyak audit [note]
```

Run the audit stage against an existing note. Registered, not implemented in this build.

## `dyak term`

```
dyak term link    [paths...] [--write] [--terms-dir DIR] [--vault-path DIR]
dyak term index   [--write] [--only-used] [--terms-dir DIR] [--vault-path DIR]
dyak term check   [--terms-dir DIR] [--vault-path DIR]
```

Ported from `mscs-termlink`. `link` injects wikilinks into Markdown notes (dry run unless
`--write`); `index` reports which lectures mention which term and writes `_index.md` on
`--write` (`--only-used` omits terms used nowhere); `check` runs diagnostics on the terms
directory and the lecture notes. The terms directory resolves from `--terms-dir`, then
`$DYAK_TERMS_DIR`, then the `terms_dir` settings key, then `<--vault-path>/mscs/_terms`.
