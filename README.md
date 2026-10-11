# dyak

Turns a lecture recording into a structured, **independently audited** Markdown note.
Every stage of the pipeline records *provenance* — which model, which parameters, how many
tokens, what changed — so a note is accounted for, not just produced.

The output is plain Markdown with `[[wikilinks]]`, so it works in any notes app. We recommend
[Obsidian](https://obsidian.md) for managing the notes: its backlinks and graph view pick up
the terms and cross-links the pipeline writes.

## Install

```
git clone https://github.com/nicklin11/dyak
cd dyak
pipx install .
```

Python 3.11+, `ffmpeg`/`ffprobe` on `PATH`, one Python dependency (`numpy`, for the sharpness
gate). Verify the environment before a run:

```
dyak doctor
```

If `dyak` dies at import with `ModuleNotFoundError`, the environment is stale, not broken:
`pipx install -e` resolves dependencies once, so a later change to `pyproject.toml` does not
reach it. Re-install with `pipx install --force -e <path>`.

## Configure

Two things minimum: an OpenAI-compatible LLM endpoint for synthesis and audit, and a
transcription backend (local whisper.cpp preferred when it answers).

```
DYAK_LLM_ENDPOINT=http://127.0.0.1:8317/v1
DYAK_LLM_MODEL=<model>
DYAK_VAULT=/path/to/notes                   # optional: where the note is copied
```

With `DYAK_STT=auto` (the default) the local whisper.cpp endpoint keeps the recording on
this machine; set `DYAK_OPENAI_URL` for a hosted one. The full variable table, every flag
and every subcommand: [`docs/running.md`](docs/running.md).

## Run

```
dyak process lecture.webm
echo $?
```

The bundle lands in `$XDG_STATE_HOME/dyak/<lecture>/`, the note in the vault. What the
bundle contains and who wrote each file: [`docs/artefacts.md`](docs/artefacts.md).

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | usage error |
| 2 | missing dependency (named, with remediation) |
| 3 | dependency failed (stderr reproduced verbatim) |
| 4 | quality gate failed; note written, report says why |
| 5 | audit found errors above threshold; note written and flagged |
| 6 | required artefacts could not be verified on disk |
| 7 | model-written note is incomplete or degraded; review its warnings |
| 130 | interrupted by the user; work dir kept |

## Where the rest is

- [`docs/`](docs/README.md) — one-line index. [`running.md`](docs/running.md) (configuration),
  [`artefacts.md`](docs/artefacts.md) (the bundle), [`quality.md`](docs/quality.md) (the gates),
  [`stages.md`](docs/stages.md) (the 12 stages), [`adr/`](docs/adr/README.md) (decisions).
- [Issues](https://github.com/nicklin11/dyak/issues) grouped by
  [milestones](https://github.com/nicklin11/dyak/milestones) — what is open and why.

## Licence

See [LICENSE](LICENSE).
