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
| 6 | required artefacts could not be verified on disk, whether the run reached stage 12 or stopped at the stage that needed the artefact |
| 7 | model-written note is incomplete or degraded; review its warnings |
| 8 | less of the lecture reached the model than the coverage threshold allows; the note is missing part of it |
| 9 | a section the model wrote was cut off at the output limit; its ending is missing |
| 130 | interrupted by the user; work dir kept |

## Development

CI and a local run have to measure the same ruff, so the pin lives in `pyproject.toml`'s
`dev` extra and nowhere else. One command runs what CI's `check` job runs:

```
tools/check.sh
```

It reads the pinned version out of `pyproject.toml`, uses a `ruff` on `PATH` only when it is
exactly that build (otherwise it runs that same version through `pipx run --spec`), and then
runs `ruff check`, `ruff format --check` and the five suites. `pip install -e ".[dev]"` puts
the pinned ruff in your own environment.

This matters because the two builds disagree on the same tree. On `15fef1f` the host's
0.16.10 reports 123 findings where the pin passes; on `4a3f67e` the host still reports the
same 123, while the pin reports one `E402` — the finding that turned CI's Lint step red. The
host's output is identical on both, so it cannot tell the passing tree from the failing one.
To compare two revisions, use the pinned binary on both:

```
git archive <rev> | tar -x -C /tmp/tree
ruff check src tests --output-format=concise | sed 's/:[0-9]*:[0-9]*:/:/' | sort > /tmp/tree.txt
comm -13 /tmp/before.txt /tmp/after.txt
```

The `git archive` extraction is the point: a working tree can carry staged or untracked
files the revision does not have.

## Where the rest is

- [`docs/`](docs/README.md) — one-line index. [`running.md`](docs/running.md) (configuration),
  [`artefacts.md`](docs/artefacts.md) (the bundle), [`quality.md`](docs/quality.md) (the gates),
  [`stages.md`](docs/stages.md) (the 12 stages), [`adr/`](docs/adr/README.md) (decisions).
- [Issues](https://github.com/nicklin11/dyak/issues) grouped by
  [milestones](https://github.com/nicklin11/dyak/milestones) — what is open and why.

## Licence

See [LICENSE](LICENSE).
