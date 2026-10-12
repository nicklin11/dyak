# Quality — what the gates reject, and the measurements behind them

Every gate writes its decision to the bundle, so a rejection is a document, not just an exit
code. See [`artefacts.md`](artefacts.md) for where each record lands.

## Exit codes vs degradation

| code | meaning |
|---|---|
| 0 | success |
| 1 | usage error |
| 2 | missing dependency |
| 3 | dependency failed |
| 4 | quality gate failed; note written, report says why — a content frame too soft (`quality.json`), or a source whose video and transcript disagree: more than 30% of the frames stage 6 considered have no transcript coverage (`captions.json`) |
| 5 | audit found errors above threshold; note written and flagged |
| 6 | required artefacts could not be verified on disk, whether the run reached stage 12 or stopped at the stage that needed the artefact |
| 7 | model-written note is incomplete or degraded; review its warnings |
| 8 | less of the lecture reached the model than the coverage threshold allows; the note is missing part of it |
| 9 | a section the model wrote was cut off at the output limit; its ending is missing |
| 130 | interrupted; work dir kept |

The source of truth is `exitcodes.DESCRIPTIONS`; [`tests/test_claims.py`](../tests/test_claims.py)
holds the README table to it.

## What exit 0 does not tell you

A reachable model is not implied by exit 0. Three cases degrade instead of failing, and each
leaves evidence in the bundle:

| where | condition | what you see |
|---|---|---|
| stage 6 | no vision endpoint | every frame `NO_ENDPOINT`, run continues, exit non-zero |
| stage 9 | tier-2 critic down | **exit 0** with a `critic/unavailable` WARN; the audit degrades to tier 1 |
| stage 6 | some frames failed | **exit 0**; per-frame `caption_status: ERROR` with the endpoint error in `caption_trace.detail` |

The third is the one to check for. `CaptionOutcome.ok` is a zero test, not a threshold, so 56
of 57 captioned is a success. Read the counts on the `[6/12] caption` line, or count
`caption_trace` entries whose `source` is `error`:

```
[6/12] caption  1 frames aligned, 2 words, 0 without transcript coverage
         no frame was captioned -- see caption-provenance.json
         status='ERROR' source='error' detail='... [Errno 111] Connection refused'
```

## Stage 5 — the sharpness gate

`GeometricEstimator` (the only registered bbox source) scores every frame; the threshold is
37320. On run 13, 58 detected frames: 57/58 pass. A frame that fails the gate is
`GATED_OUT` from `manifest.tsv` and never leaves the machine.

## Stage 8 — the lint gate

Nine mechanical checks against the note before the audit runs. Findings land in `lint.json`.
On run 13: `9 checks, 0 errors, 0 warnings`. That the deterministic half of the audit chain has
run but never produced a finding on a real run is itself tracked:
[#93](https://github.com/nicklin11/dyak/issues/93).

## Stage 9 — the audit

Four tier-1 rules (deterministic) plus a tier-2 separate-context critic run over the note. Each
finding quotes the line it judges. Findings above threshold exit 5; the note is still written
and flagged, because the repairable state of a 3800-word note is worth more to the reader than
a clean failure.

## What the note should look like — the baseline comparison

Against the hand-written baseline (re-measured on run 7, not carried forward):

| | baseline | generated |
|---|---:|---:|
| bytes | 64150 | 50903 |
| words | 4841 | 3808 |
| `## N.` sections | 8 | 8 |
| wikilinks | 159 | 96 |
| display `$$..$$` | 10 | 6 |
| inline `$..$` | 122 | 51 |
| tables (rows) | 90 | **0** |
| `### N.M` subsections | 18 | **0** |

The section skeleton matches; the two structural gaps — no tables, no numbered subsections —
belong to the synthesizer, not the pipeline, and are tracked with their measurements in
[#74](https://github.com/nicklin11/dyak/issues/74).
