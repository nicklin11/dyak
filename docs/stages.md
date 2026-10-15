# Pipeline stages — what exists, what is open, what is verified

ADR-0001 records what was **decided**. This file records what is **built**. They are not
the same thing, and the difference is where the work actually is.

The stage list and the exit-code table are in
[ADR-0001 § The pipeline](../adr/0001-lecture-pipeline.md#the-pipeline).

## How to read the columns

**Code** — the module that implements the stage. Verified by reading the file.

**Code present** — `yes` means the module exists and is called from `pipeline.run`.
It does **not** mean the stage has been seen to work on a real lecture. See
[Verification](#verification-status).

**Stage fully working** — the stage ran on the 4520 s lecture and left its artefacts in a
bundle, and the last column names where its provenance is. Every stage now records something,
either a `*-provenance.json` or, for stage 4, `detect.json`. Stage 10 records
`repair-provenance.json`, and makes no calls — it is deterministic, and a document of the calls
would be an artefact about constants, not provenance. What the file does record is the tool,
its rule set, what it changed, and since #257 the endpoint and the seed.

| stage | provenance | what it answers |
|---|---|---|
| 1, 2 | `source-provenance.json` | what the input was, and which ffmpeg measured it |
| 3 | `stt-provenance.json` | backend, endpoint, every request field, digests of both transcripts |
| 4 | `detect.json` | the full filter chain, every setting, the ffmpeg version |
| 5–12 | `*-provenance.json` | the tool, its rule set, and what it changed; stage 10's also carries the endpoint and the seed (#257) |
| 6 | per-frame `caption_trace` in `captions.json` | which model captioned this frame, tokens, timing, called or cached |
| 6 | `caption_status` per frame | `NO_NEW_INFORMATION` says the model declined; it is a success, not an error, and a high share means the screens went unread |
| 7, 9 | `synth-`/`audit-llm-transcript.json` | every call: full messages, model, tokens, seconds, attempts, `finish_reason` |
| 1-12 | `timings.json` | wall clock per stage, and their sum in `total_seconds`. Written after stage 12, not registered as an artefact. Since #115 stages 2-5 run as two branches, their seconds overlap and the sum is larger than the run's wall clock: `total_seconds` is the cost of the stages, and `run-state.json`'s `started_at`/`finished_at` are the time the run took |
| 1-12 | `run-state.json` | the run id, `started_at`/`finished_at`, the stage reached, and `completed`/`interrupted`/`failed`. Written in a `finally`, so an interrupted run's bundle says so (#92) |

`finish_reason` is why the model stopped. `length` means the endpoint's output cap cut it off:
the section is real but incomplete, and stage 7 marks it degraded instead of letting a
truncated derivation pass lint and audit as finished content (#124).

Stage 9's requests put the **transcript first and the section last** (#123). All eight calls
then share the transcript as one common prefix, which is the condition a provider
prefix-cache is built for; with the section first, every request's prefix was unique and
nothing could be shared. This changes what `audit-llm-transcript.json` records, so the order
is part of the artefact's shape rather than an implementation detail. A section above
`audit.SECTION_CHARS` (6000) is still audited only in part, but which sections were cut is now
named in `audit.json` under `tier2_truncated` and in the stage's run line, instead of being
applied silently to the request.

The three LLM stages keep different amounts on purpose. Stages 7 and 9 make 8 calls each and
store the whole `messages` array — ~190 KiB per call for stage 9, which needs the section text
to be auditable. Stage 6 makes 58 and stores a summary; the full form would be ~11 MiB per
run, more than the rest of the bundle. Attribution still works: model, tokens, seconds,
attempts, fingerprint, and whether the frame was called or taken from cache.

## The table

| # | Stage | Code | Code present | Stage fully working |
|---|---|---|---|---|
| 0 | `doctor` | `deps.py`, `cli.py` | yes | yes — green with a real gateway |
| 1 | `probe` | `probe.py` | yes | **yes** — `source-provenance.json` |
| 2 | `audio` | `audio.py` | yes | **yes** — same document |
| 3 | `stt` | `stt/` — `core`, `backends`, `whispercpp`, `openai_compat` | yes | **yes** — 4520 s, 4901 segments via the proxy |
| 4 | `frames` | `frames.py` | yes | **yes** — 58 frames, deterministic |
| 5 | `quality` | `quality.py` — `GeometricEstimator` | yes | **yes** — 57/58 pass, threshold 37320 |
| 6 | `caption` | `caption.py`, `llm.py` | yes | **yes** — 57 frames captioned, 16 refusals, per-frame `caption_trace` |
| 7 | `synth` | `synth.py` | yes | **yes** — 8/8 sections, 8 LLM calls recorded, stage 6 captions in every prompt. Since #117 every request carries the whole lecture and `synth.json` records `coverage`; the run above predates that and its coverage was 0.69 |
| 8 | `lint` | `lint.py` | yes | **yes** — 9 checks, clean |
| 9 | `audit` | `audit.py` | yes | **yes** — 4 rules, tier 2 ran, 8 LLM calls recorded |
| 10 | `repair` | `repair.py` | yes | **yes** — deterministic, nothing to repair |
| 11 | `link` | `link.py` | yes | **yes** — 34 terms, 96 links |
| 12 | `report` | `report.py` | yes | **yes** — 8 verified, frames 57/57 |

Stages 7–10 and 1–2 are not "broken" or "missing". They are written, imported by
`pipeline.run`, and covered by `tests/test_stages.py`. What they lack is a **verified run
over the lecture**, which is a different and cheaper claim to earn.

Stages 2-5 are one **branch pair**, not four steps in a queue (#115): `audio`→`stt` and
`frames`→`quality` share no input, so they run concurrently, one thread each, and stage 6 is
the join — it needs both the transcript and the manifest, so it cannot start before both
branches have published. The stage numbers keep their meaning (`[4/12] frames` is still stage 4
of twelve), but a run's lines for stages 2-5 arrive out of numerical order and carry the branch
that wrote them, `(A)` or `(B)`; see [ADR-0001 § The pipeline, amendment 2026-10-12][adr115].
`DYAK_SERIAL=1` runs the pair sequentially instead, which is how the fork's wall-time saving is
measured against an identical run. Stage 5's rule is unchanged either way: it gates the frames
stage 4 wrote, and a branch that failed does not take its sibling's published artefacts with it.

[adr115]: ../adr/0001-lecture-pipeline.md#amendment-2026-10-12--stages-2-5-run-as-two-branches-joined-by-stage-6-115

## Stage 6 — what was added, and what it is still not

Stage 6 was two halves: a deterministic alignment that worked, and a captioning half that
had **no vision client at all**. `llm.py` was text-only — `grep -niE 'image|vision|b64|base64|multimodal|image_url' src/dyak/llm.py` returned nothing — and `caption.py` said so itself:

```
captioning is NOT_CONFIGURED -- no vision client in this codebase
```

That string is gone. Stage 6 now builds an OpenAI-compatible message carrying the frame as
a `data:` URI and calls the same `chat()` the text stages use; the transport was never the
missing part, only the message shape and the configuration were.

**Verified live**, 2026-10-04, against `http://100.64.0.2:8317/v1` with
`opencode-go/glm-5.3-flash`, on two real frames from lecture 1:

| frame | time | result |
|---|---|---|
| `f_0000000000.jpg` (53 KiB) | 1.5 s | `NO NEW INFORMATION` — a title screen with nothing the audio did not already carry |
| `f_0000617096.jpg` (163 KiB) | 4.6 s | a full transcription of the references page: four numbered courses, the first literature entry, and the screen-share banner occluding a line |

58 frames took **446.8 s** of wall clock for the whole stage -- 7.7 s per frame, and
**88% of every LLM call the pipeline makes**. `caption_trace` in `captions.json` records
the model, tokens, timing and whether each frame was called or cached; see #81.

The second caption also shows the provenance chain working: `served_model` came back as
`glm-5.3-flash` while the requested id was `opencode-go/glm-5.3-flash`. Both are recorded,
and the disagreement is visible rather than resolved silently (#61).

### Failure behaviour, which changed deliberately

The old code wrote `caption_status: NOT_CONFIGURED` into the artefact and exited 0. Now:

| Situation | Behaviour |
|---|---|
| No vision endpoint | every frame marked `NO_ENDPOINT`, run **continues**, exit non-zero |
| Endpoint configured but refusing | per-frame `ERROR` with the endpoint's message |
| Every frame failed | exit 3, `DEPENDENCY_FAILED`, with the per-frame reasons |
| Caption returned but empty | `EMPTY`, counted as a failure |
| Endpoint not answering at all | after 3 consecutive connection failures the stage stops calling; the frames it never reached are `STOPPED`, not 55 more retry ladders (#124) |

The first row follows the contract stage 7 already has: `TemplateSynthesizer` exists so the
pipeline runs end to end with no model configured, and stage 6 must not be the stage that
breaks it. A note written without slide content is a worse note, not no note — but it is a
worse note the run must say out loud.

## Stage 5 is **not** open, despite what the code used to say

`resolve_estimator` registered three names. `vlm` raised `NotConfiguredError` on both
branches, so `DYAK_BBOX_SOURCE=vlm` failed at the crop instead of being refused as the
unknown name it is. It has been removed from the registry (#67): a vision bbox source is a
real idea that is still unbuilt, and an unbuilt option should be absent rather than present
and broken.

Stage 5 itself works through `GeometricEstimator`, the default.

## Verification status

**1. A full 12-stage run including stage 6.** Done, repeatedly. Run 3 on
`1_lecture_OCS.mp4` (4520 s, AV1 1080p): 58 frames detected and gated, 58 captioned with 0
failures, note 53.5 kB, lint `9 checks, 0 errors, 0 warnings`, stage 12
`8 verified, 0 optional absent, frames 58/58`, exit 0 in 1935.6 s.

**2. The suites are still not hermetic.** `tests/test_stages.py` and `tests/test_doctor.py`
read ambient `DYAK_*` and fail when those are exported. Pre-existing, reproduced on
pristine `main`, filed as #63. "The tests pass" still means "the tests pass in a clean shell".

Two variables are pinned by this work rather than deferred to #63, because the changes that
introduced them also introduced writes. Without the pin, running the suite created
`~/.config/dyak/settings.toml` holding the developer's real vault path, and wrote
`audio.wav`, `transcript.txt`, `transcript.json` and `transcript.raw.json` into
`~/Documents/obs_notes/dyak/`. `DYAK_SETTINGS` also belongs in the CI scrub list.

**3. Compared against the hand-written baseline.** Run 7's `note.linked.md` against
`Лекция 1. 01.10.26.md` (64150 bytes), re-measured rather than carried forward:

| | baseline | generated |
|---|---:|---:|
| bytes | 64150 | 50903 |
| words | 4841 | 3808 |
| `## N.` sections | 8 | 8 |
| unnumbered `##` | 1 | 0 |
| wikilinks | 159 | 96 |
| display `$$..$$` | 10 | 6 |
| inline `$..$` | 122 | 51 |
| tables (rows) | 90 | **0** |
| `### N.M` subsections | 18 | **0** |
| `[неразборчиво]` | 3 | 18 |

The section skeleton matches: 8 `## N.` headings either way. The two structural gaps belong
to the synthesizer skill, not the pipeline, and are recorded in #74 with the measurements.
They are also the user's explicit call — "оставить, записать как issue" — so the pipeline
does not paper over them.

`[неразборчиво]` at 18 against 3 is a stage-3 transcription artefact, not a synthesis one:
run 6's audit flagged the contradiction, where a marker appeared inside a span the transcript
renders in full. #49 is the underlying measurement.

## Stage 12 — what it reports

`report.json` states the coverage of its own verification, computed on disk at run end:

| field | what it answers |
|---|---|
| `verified` / `absent` / `missing` | artefacts checked, expected-but-absent optional ones, required-but-not-found ones |
| `inconsistent` | where two artefacts contradict each other, or one of the pair cannot be read: `captions.json` against `quality.json` (#87) |
| `source_had_video` / `quality_gate_required` | what the bundle's `source-provenance.json` says about the input, and whether that made `quality.json` required. `null` = record absent, so the gate stays optional |
| `files_checked` | how much of the bundle stage 12 actually confirmed |
| `files_in_bundle_excluding_this_stage` | everything it could have checked |
| `unchecked` | the absolute list of files it did not check |
| `frames_expected` / `frames_found` | frames against `manifest.tsv` |
| `transcript_coverage` / `transcript_coverage_threshold` | how much of the transcript reached the model in stage 7, read back from `synth.json` rather than recomputed. Below the threshold the run does not exit 0 (#117) |
| `sections_total` / `sections_not_covered` / `sections_not_covered_share` | how many of the eight sections stage 7 left as `в лекции не затрагивается`. Reported, never gated: a lecture without the skill's eight-section shape leaves some legitimately empty (#128) |
| `sections_truncated` / `sections_truncated_numbers` | sections the model wrote that the endpoint then cut off at its output cap (`finish_reason == "length"`). Gated: a run with one does not exit 0 (#124, #128) |
| `note_promoted` | whether the vault copy happened |

Since `5c66731`, `verified ∪ unchecked = files_in_bundle_excluding_this_stage` holds on the
artefact itself, and a reader can compute the coverage claim rather than trust the prose.
Before that release, the module's prose claimed 143 files were checked when 65 were — the
artefact above is the correction. `files_checked ==
files_in_bundle_excluding_this_stage` is still the end state; what remains outside the check
is the `*-provenance.json` family, the intermediate notes and the rejected frames.

`verified` is presence-and-non-empty per file. The content check is `inconsistent`: #87
promoted `captions.json` and `synth.json` into `REQUIRED`, made `quality.json` required when
the source had video, and added the cross-reference between `captions.json` and
`quality.json`. That last one is the check that would have caught #82 from the bundle alone —
the two files were each internally consistent and contradicted each other about a frame.

## Where the note is written

`dyak process` exports into the vault by default, into `<vault>/dyak/`. The vault
resolves `--vault-path` → `$DYAK_VAULT` → the `vault` key in
`$XDG_CONFIG_HOME/dyak/settings.toml` → `~/Documents/obs_notes`, and the settings file is
written on a first run with nothing configured. Before this, a run with no flag wrote nothing
to the vault at all and left the note in `~/.local/state/dyak/` (#71).

The subdirectory is not cosmetic: a bundle is ~170 files, 25 in the bundle root and 145 under
`images/`, and the vault root is where the user's own notes live. A vault path that does not
exist is reported and named, never created — `export_to_vault` ends in `mkdir(parents=True)`,
so exporting to a typo would materialise an empty directory that `dyak doctor` then
certifies as healthy.

## Claims in the code that are now true

`src/dyak/stages.py` states:

```python
IMPLEMENTED = 12
```

It was false while stage 6 had no vision client, and it is true as of #67. The count in
every progress line is an accurate claim again.

Two defects in that block were real and are fixed:

- **The citation was wrong.** The module said "every stage D3 lists". `D3` is about reading
  frames from the source; the twelve-stage list is in ADR-0001 § *The pipeline*. An instance
  of the unqualified `D<n>` problem in #66 where the number did not merely collide, it
  pointed at the wrong decision.
- **The module's own docstring gave the rule the constant broke** — "`[6/12] caption` on a
  six-stage pipeline is a claim about the software, not a formatting preference."

What `IMPLEMENTED = 12` still is not: a measurement. It is a declaration. If a stage is ever
opened again, this constant is what will keep claiming the stage is closed, and nothing in
the test suite catches that.