# ADR 0003: The pipeline emits events; renderers consume them

- **Status:** Accepted
- **Date:** 2026-10-03
- **Proposal:** [#14](https://github.com/nicklin11/dyak/issues/14)
- **Scope:** MVP. The terminal renderer is not designed here.

> **`D<n>`** = decision *n* within *this* ADR. The number is local to this file and
> collides with numbers in ADR-0001 and ADR-0004; references from anywhere else must be
> written `ADR-000N Dn`. Reading order and index: [`docs/adr/README.md`](README.md).

## Context

The requirement is a view of what the summarisation stages are doing. The measured facts
constrain what such a view can honestly show.

- Stage 7 `synth` and stage 9 `audit` are each **one** blocking HTTP request to a model
  endpoint. They return a single response. **There are no intermediate steps to display.**
- Stage 3 `stt` is one blocking request to whisper.cpp and, per ADR-0001 D7, offers no
  per-segment callback.
- Measured cost on lecture 1: stages 1–3 total 264 s, of which stage 3 is 257 s. The audit
  is ~20 minutes and ~106k context tokens.

So "each step of the summarisation" cannot be satisfied by watching stages 7 and 9 tick. The
only way to obtain per-step progress inside them is to require the model to emit a
machine-readable event stream — which trades summary quality and output determinism for
cosmetics, and makes the note's shape non-diffable against the baseline in #3.

## Decision

### D1 — The pipeline does not render. It emits.

`dyak process --events jsonl` writes one JSON object per line to stdout, or to a file with
`--events FILE`:

```json
{"v":1,"event":"stage_start","stage":3,"name":"stt"}
{"v":1,"event":"stage_end","stage":3,"seconds":257.2,"artefacts":["transcript.json"]}
{"v":1,"event":"warning","stage":3,"text":"segment 412 ends before it starts"}
```

Human-readable output is the default **rendering** of the same events, not a second code
path. Anything that renders from the stream is a consumer, including `less -f` and `jq`.

### D2 — Only observable facts are events

Elapsed wall time, artefact paths, byte counts, gate verdicts, anomaly text. Model-reported
progress percentages are **not** events: they are a model's estimate of itself, and a bar
driven by them is precisely the D7 lie.

### D3 — Versioned stream

`"v":1` on every line. Additive changes are compatible; renaming or removing a field is a
breaking change. The stream is a public interface the moment it ships.

### D4 — Blocking work is bracketed, not smoothed

Before a blocking request the pipeline emits its estimate interval and the basis for it
(`expect 3.8min-23min, 4520s of audio`); after it, the actual elapsed. **The gap between the two
is data**, and it is emitted rather than hidden. *Amended 2026-10-12 (#186): the interval is
emitted only where a measurement exists for the backend that will actually run — whisper.cpp's
measured rate — and a hosted endpoint whose rate this repo has not measured prints `no rate
measured for this endpoint` instead of borrowing one. `no streaming (D7)` is no longer in the
line: the hosted route reports the wav slices it observes as they advance (`chunks k/N`), which
is observed state and not the fabricated bar D7 bans (#193).*

Stage 3 already prints this bracket; D1 generalises it to every blocking stage.

## Alternatives rejected

- **A progress bar weighted across stages 0–12.** Retained as the default human rendering,
  because the weights are measured (ADR-0001 D7) — but it is a rendering of the stream, not
  its source.
- **Streaming model reasoning as progress.** Rejected: it changes what the model produces and
  destroys the fixed output shape the #3 regression check depends on.
- **An interactive TUI for configuration.** Rejected: a second binary to save two command-line
  arguments. `dyak config set` is the deliverable (ADR-0004 D2).

## Consequences

**Good.** The expensive, silent part of the pipeline becomes measurable. A display can be
added without touching stage code, which is what makes a future GUI cheap instead of a
rewrite.

**Bad.** `--events jsonl` becomes a public interface, and D3's versioning discipline is the
price of admission.

**Not decided.** The terminal UI itself. No stage depends on it.

## Acceptance

- `--events jsonl` emits one parseable line per stage start, stage end and warning, and
  nothing that is not one of those.
- Default human output is produced from the same events.
- Stage 3's estimate bracket appears before the request, the actual elapsed after it.
- Adding a stage does not require editing any renderer.
- The pre-existing stage lines (`  [3/12] stt 257.2s …`) are unchanged in content, only in how
  they are produced.

Implements ADR-0001 D7. Part of #3.