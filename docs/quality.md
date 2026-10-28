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
+ 135.9s   [6/12] caption  1 frames aligned, 2 words, 0 without transcript coverage [opencode-zen/deepseek-v4.1-flash]
+ 135.9s            no frame was captioned -- see caption-provenance.json
+ 135.9s            status='ERROR' source='error' detail='... [Errno 111] Connection refused'
```

## STT anomalies — recorded, never gated (the #93 decision)

`stt-provenance.json` records every parser observation in `results.anomalies` (48 on the
recorded lecture-1 run), and stage 3 prints the first ten. The run does **not** degrade or
fail on the count, and that is a decision rather than an omission: the anomalies are
decoder-level facts (a skipped segment, an end before a start, a payload shape), not a
quality score, and nothing measured so far ties their number to what the note is worth. A
threshold without that measurement would be a number with no denominator.

The decision changes when #49 or #32 produces the measurement — a run-to-run agreement
figure, or a hosted endpoint whose failures are distinguishable. Until then the count is
visible in the artefact and in the log, and the exit code does not depend on it.

## Stage 5 — the sharpness gate

`GeometricEstimator` (the only registered bbox source) scores every frame; the threshold is
37320. On run 13, 58 detected frames: 57/58 pass. A frame that fails the gate is
`GATED_OUT` from `manifest.tsv` and never leaves the machine.

### When no frame passes the gate

A source whose writing scores below the absolute floor can fail every frame -- light-on-dark
handwriting measures well below a floor calibrated on dark-on-white slides. That used to be the
end of the note: stage 7 built its payload from the frames stage 5 passed, so zero frames meant
an empty payload and eight headings reading "no source material", with no model call at all.
Stage 6 had captioned nothing because it had been handed nothing, and the run reported that as a
vision-endpoint failure and exited 3.

Since #345 that run degrades instead of collapsing:

- stage 7 sends the transcript as one slice (`synth.transcript_section`) and every section is
  written from it. The note's provenance line says `Собрано только из стенограммы`, and
  `synth.json` records that no screen content is in it.
- the exit code is **4**, the gate's code, because the frames were measured and rejected. The
  vision-endpoint message is printed only when stage 6 had frames and the endpoint failed.
- the message names the stage that said no:

```
dyak: quality gate failed for 3/3 frames; see /path/to/bundle
dyak:   f_0000360000.png: MEGE 18242 < 20000, cropped, unknown (SOFT)
dyak: stage 6 captioned nothing: stage 5 passed 0 frames, so no image reached the vision
       model; the note was written from the transcript alone
```

The absolute floor is a build constant in this version, so a recording that scores below it
cannot be captioned; making it a settings key is a separate decision about what the gate means.

## Stage 8 — the lint gate

Nine mechanical checks against the note before the audit runs. The nine counts checks *that ran*,
which is not what `lint-provenance.json`'s `rules` list holds: that list names the ids those checks
can report, and one check can report more than one. Findings land in `lint.json`.
On run 13: `9 checks, 0 errors, 0 warnings`. The bundle's `findings-ledger.json` now carries
the per-rule counts across runs of the same lecture, so a zero is a recorded zero over N runs
rather than an absence; a rule that has never fired is still named there (#93).

## Stage 9 — the audit

Four deterministic tier-1 checks — term drift, dimension mismatch, unsourced numbers, empty math —
plus a tier-2 separate-context critic run over the note. The four is the checks; `tier1.rules` in
`audit-provenance.json` is the list of *ids* they can report, which is a different quantity: a check
that finds nothing to check reports a `-skipped` marker for the absence, and `check_dimensions`
alone reports up to three ids. `tier1.rules_run` beside it, and `tier1_rules_run` in `audit.json`,
are the four checks that ran. Each finding quotes the line it judges. Findings above threshold exit
5; the note is still written and flagged, because the repairable state of a 3800-word note is worth
more to the reader than a clean failure.

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
