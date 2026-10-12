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
whispercpp_url = "http://100.64.0.1:10301"
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
| `DYAK_WHISPERCPP_URL` | `stt/whispercpp.py` | whisper.cpp `POST /inference` endpoint. **No built-in default** (#95): unset means "not configured" (exit 2), not a probe of `127.0.0.1:10302`. The supported target is the shipboard proxy on `http://100.64.0.1:10301`, which refreshes the idle marker; a raw container port has no auto-start and is reclaimed after 300 s by `whisper-idle-stop.timer` |
| `DYAK_WHISPERCPP_LANGUAGE` | `stt/whispercpp.py` | pin the language; unset means autodetect |
| `DYAK_OPENAI_URL` | `stt/openai_compat.py` | hosted STT endpoint (OpenAI-compatible `/v1/audio/transcriptions`). **No built-in default** (#95): unset means "not configured", not the whisper.cpp port |
| `DYAK_OPENAI_KEY` | `stt/openai_compat.py` | key for the hosted STT endpoint |
| `DYAK_OPENAI_MODEL` | `stt/openai_compat.py` | model id; default `whisper-1` |
| `DYAK_OPENAI_MAX_UPLOAD_BYTES` | `stt/openai_compat.py` | per-upload ceiling of the hosted STT route, in bytes; a longer wav is sliced into chunks below it and their word timings spliced, with the sample bytes copied verbatim. Default `26214400` (25 MiB, the documented hosted limit). Bytes, not a unit-suffixed string: `25M` would have to guess between 25,000,000 and 26,214,400, and a wrong guess is the request that exceeds the limit. In the settings file this is written bare — `openai_max_upload_bytes = 26214400`; a quoted count also works, and a unit-suffixed string is exit 1 naming the setting |
| `DYAK_LLM_ENDPOINT` | `llm.py`, `deps.py` | OpenAI-compatible base URL for synthesis (stage 7) and audit (stage 9) |
| `DYAK_LLM_MODEL` | `llm.py`, `deps.py` | model id sent in the request |
| `DYAK_LLM_KEY` | `llm.py` | optional; unset means no `Authorization` header |
| `DYAK_VLM_ENDPOINT` | `llm.py`, `deps.py` | stage 6 frame captioning; unset falls back to `DYAK_LLM_ENDPOINT` |
| `DYAK_VLM_MODEL` | `llm.py`, `deps.py` | as above, falls back to `DYAK_LLM_MODEL` |
| `DYAK_VLM_KEY` | `llm.py`, `deps.py` | as above, falls back to `DYAK_LLM_KEY` |
| `DYAK_VAULT` | `deps.py` | the notes directory the note is exported into (any folder; Obsidian is recommended) |
| `DYAK_SETTINGS` | `settings.py` | the settings *file*, overriding the XDG path; the one variable the settings file cannot carry |
| `DYAK_TERMS_DIR` | `link.py` | atomic term notes for stage 11; default `<vault>/mscs/_terms` |
| `DYAK_OUTPUT_DIR` | `bundle.py` | where the bundle goes; default `$XDG_STATE_HOME/dyak/<lecture>`, or `~/.local/state/dyak/<lecture>` when `XDG_STATE_HOME` is unset |
| `DYAK_WORKDIR` | `workspace.py` | parent of the managed work dir (scratch: `audio.wav` etc.); default `$TMPDIR` |
| `DYAK_BBOX_SOURCE` | `pipeline.py` | stage 5 estimator name; the registry holds `geometric` (default) and `null` (`quality.ESTIMATORS`) |

Every row above except `DYAK_SETTINGS` is also a flat key in the settings file — the variable
name lowercased (`llm_endpoint`, `whispercpp_url`, ...) — and is read through `settings.resolve`
in the module named here.

`DYAK_GATEWAY_URL` is not read anywhere: the check that read it was removed (see the
comment in `deps.py`). Standard Python/XDG variables — `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`TMPDIR` — are also consulted, each with a documented fallback.

## `dyak doctor`

Reports missing or conflicting dependencies and exits 2/3 with the reason and remediation.
Run it before a run, not after a failure. Both model roles are probed: the text endpoint, and
the vision endpoint stage 6 captions with when `DYAK_VLM_*` is set. An unset vision role is
reported, not failed — stage 6 falls back to the text pair.

## `dyak process`

```
dyak process [path] [options]
```

| flag | meaning |
|---|---|
| `path` | video or audio file |
| `--workdir DIR` | parent directory for the managed work dir (default: `$TMPDIR`, or `DYAK_WORKDIR`) |
| `--output-dir DIR` | where to write the output bundle (default: `$XDG_STATE_HOME/dyak/<lecture>`, or `~/.local/state/dyak/<lecture>` if unset) |
| `--overwrite` | replace an existing output bundle instead of writing into it |
| `--vault-path DIR` | copy the finished bundle into this vault; never a pipeline blocker. Defaults to the configured vault, and the run writes into it either way — use `--no-vault-export` to keep the note in the bundle only |
| `--no-vault-export` | finish the bundle without copying it into the vault |
| `--vault-images` | also copy `images/` into the vault. Off by default: the frames are most of the bytes (50 of 59 MiB on lecture 1) and the note refers to none of them, so they stay in the bundle as the pipeline's evidence |
| `--vault-subdir REL` | where inside the vault the bundle lands, relative to it (default: `<vault>/dyak/<lecture>/`, one directory per lecture, #118). Not optional in practice: a bundle is ~188 files, and the vault root holds the user's own notes |
| `--force-export` | replace a vault note that was edited by hand after the last export. Default: refuse, and name the file (#118) |
| `--no-llm` | run without an LLM endpoint: the note is the template skeleton, not a model's. Without this flag `process` refuses to start when `DYAK_LLM_ENDPOINT` is unset or unreachable (#126) |
| `--keep-workdir` | keep the work dir even when the run succeeds |
| `--glossary FILE` | ASR term glossary for stages 9 and 10, in the `\| variant \| canonical \| domain \| date \|` format. Without it the term-drift rule finds nothing and the audit says so |
| `--terms-dir DIR` | atomic term notes for stage 11. Default: `$DYAK_TERMS_DIR`, the `terms_dir` settings key, then `<vault-path>/mscs/_terms`. Without it the note is written unlinked and the stage says so |

### What the progress output looks like

One line per stage, printed when the stage finishes, with the seconds it took. Stage 3 is the
exception: it announces itself *before* the transcription begins, and on the hosted route prints
an indented `chunks k/N` line after each upload returns (#193). This is a real
run — 180 s of lecture 1, seven frames — with the stages that print detail trimmed:

```
  [1/12] probe       0.0s  video h264 1920x1080, audio aac 48000Hz 2ch, 180.0s
  [2/12] audio       0.2s  audio.wav 180.0s 16000Hz mono, 5.5 MiB
  [3/12] stt     starting  180s of audio through whispercpp at http://100.64.0.1:10301, expect 36s-54s; this backend reports no progress
  [3/12] stt         9.1s  52 segments, 611 timed words, speech to 180.0s of 180.0s audio [whispercpp]
  [4/12] frames     12.5s  7 frames, 2.7 MiB, median gap 9s
  [5/12] quality     3.7s  7/7 frames pass (MEGE 57230-146128, threshold 38303, 7 cropped)
  [6/12] caption   119.5s  7 frames aligned, 493 words, 0 without transcript coverage
  [7/12] synth     327.7s  llm, 66 lines, 7/8 sections filled  DEGRADED
  [8/12] lint        0.0s  9 checks, clean
  [9/12] audit     752.1s  4 rules, 0 errors, 7 warnings, 4 discarded, tier 2 ran, 7 findings, 4 discarded, 5/8 sections reviewed (capped at 6 findings)
  [10/12] repair      0.0s  nothing to repair
  [11/12] link        0.0s  34 terms, inserted 2 links across 1 files
  [12/12] report OK: 8 verified, 0 optional absent, frames 7/7
```

Three things about that shape are worth knowing:

- **`[3/12] stt starting` is not a duplicate.** It appears before the transcription begins, and
  has no seconds because nothing has elapsed yet. Every other stage prints exactly one line. On
  the hosted route the indented `chunks k/N` lines appear underneath it as each upload returns;
  those are not stage lines either.
- **The gap between lines is the stage's real cost.** Stage 9 above took 752 s of a 1225 s run.
  On a full 75-minute lecture the same run spends most of its wall clock in transcription,
  frame extraction, synthesis and audit, in that order.
- **Stages that produce findings print them underneath**, indented, as they finish — caption
  counts, audit warnings, repaired lines, linked terms. Those are not stage lines and are not
  counted as such.

The run then closes with the transcript summary, the total, and the bundle path.

### Redirecting the log

```
dyak process lecture.mp4 > run.log 2>&1
```

`run.log` is live. Every stage line, per-frame caption line and audit finding reaches the file
before the next stage begins — the streams are reconfigured line-buffered at the entrypoint, so
`tail -f run.log` tracks the run instead of showing nothing until it exits.

This was not true until #100, and the failure was worth recording. CPython block-buffers
`sys.stdout` in 8192-byte chunks when fd 1 is not a tty, and the pipeline flushed exactly once,
immediately before stage 3. Run 14 sat at 266 bytes for 16 minutes with its last line reading
`[3/12] stt starting`, while the run was in fact in stage 8 with its only socket open to the LLM
gateway. The obvious reading of that log is a 16-minute transcription stall, and what disproved
it was the bundle's file mtimes rather than the log itself.

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
