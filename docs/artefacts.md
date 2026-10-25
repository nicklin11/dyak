# The bundle — what a run writes

The bundle lives in `$XDG_STATE_HOME/dyak/<lecture>/` by default (`$DYAK_OUTPUT_DIR` or
`--output-dir` name it explicitly). The root is **flat**: `images/` is the only subdirectory.

Produced by run 13, `1_lecture_OCS.mp4` (4520 s): 30 entries in the root, 58 frames under
`images/`; runs after #93 add `findings-ledger.json`, so the root count is 31.

## Root files, by writer

| file | written by | what it is |
|---|---|---|
| `transcript.raw.json` | stage 3 `stt` | the endpoint's payload, untouched |
| `transcript.json` | stage 3 `stt` | structured transcript, with word timings |
| `transcript.txt` | stage 3 `stt` | plain text |
| `manifest.tsv` | stage 4 `frames` | frame index: timestamp, path, reason |
| `detect.json` | stage 4 `frames` | the ffmpeg filter chain and settings actually used |
| `quality.json` | stage 5 `quality` | per-frame sharpness and the gate decision |
| `captions.json` | stage 6 `caption` | per-frame captions and `caption_trace` (model, tokens, timing, called or cached) |
| `synth.json` | stage 7 `synth` | the synthesis state; the note chain is `note.md` → `note.repaired.md` → `note.linked.md` |
| `note.md` | stage 7 `synth` | the note as synthesised |
| `lint.json` | stage 8 `lint` | mechanical gate findings, before the audit |
| `audit.json` | stage 9 `audit` | findings, each with its quoted line |
| `repair.json` | stage 10 `repair` | what was changed and what was declined |
| `note.repaired.md` | stage 10 `repair` | the note after repair |
| `link.json` | stage 11 `link` | wikilinks inserted |
| `note.linked.md` | stage 11 `link` | the note after linking |
| `report.json` | stage 12 `report` | what stage 12 verified, and what it did not find |
| `timings.json` | `pipeline` | wall clock per stage and the total; written after stage 12, not a registered artefact |
| `run-state.json` | `pipeline` | the run's id, `started_at`/`finished_at`, the stage reached, and whether the run **completed**, was **interrupted**, or **failed**; written in a `finally`, so it exists on the runs that did not finish |
| `findings-ledger.json` | `pipeline` | per-rule finding counts **across runs of this lecture**, including every rule that has produced 0; written after stages 8 and 9, not a registered artefact, and deliberately not cleared between runs (#93) |

`timings.json` and `run-state.json` are sidecars written by `pipeline.run` itself rather than
by a stage. Both are written from the same `finally`, so a run that raised still has its
per-stage seconds and still says how it ended (#92). Neither is registered through
`Bundle.record`, so neither travels to the vault; both are in `bundle.RUN_ARTEFACTS`, so the
next run clears them. A stale `outcome: completed` left beside a fresh bundle is exactly the
file a reader would mistake for this run's.

`findings-ledger.json` is the third sidecar, and the exception to that clearing rule: it is
not in `RUN_ARTEFACTS`, because its whole point is to accumulate. After stage 8 and again
after stage 9 the pipeline folds that stage's findings into it, and the counters — `runs`,
and per rule `findings`, `runs_with_findings`, `last_seen` — are what turn a zero in a single
`lint.json` from "no finding" into "no finding in N recorded runs, for a rule that was
declared" (#93). It is not exported to the vault, for the same reason `timings.json` is not:
it is a record of runs, not a product of the note.

`note.synth.md` does not exist: `Bundle.publish` names published files by their source
basename, and stage 7 publishes `note.md` plus `synth.json`.

## Provenance — one file per stage

Every stage writes a `*-provenance.json` recording what produced its output:

| stages | provenance | what it answers |
|---|---|---|
| 1, 2 | `source-provenance.json` | what the input was, and which ffmpeg measured it |
| 3 | `stt-provenance.json` | backend, endpoint, every request field, digests of both transcripts |
| 4 | `detect.json` (see above) | the full filter chain, every setting, the ffmpeg version |
| 5–12 | `quality`/`caption`/`synth`/`lint`/`audit`/`repair`/`link`/`report`-provenance.json | the tool, its rule set, and what it changed |
| 6 | per-frame `caption_trace` in `captions.json` | which model captioned this frame, tokens, timing, called or cached |
| 7, 9 | `synth-llm-transcript.json`, `audit-llm-transcript.json` | every call: full messages, model, tokens, seconds, attempts, `finish_reason` |

`finish_reason` is the endpoint saying why the model stopped. `length` means the output cap cut
it off: the text is real but incomplete, and stage 7 marks that section degraded rather than
letting a truncated derivation ship as finished content (#124).

The three LLM stages keep different amounts on purpose. Stages 7 and 9 make 8 calls each and
store the whole `messages` array — ~190 KiB per call, needed so the audit is checkable. Stage
6 makes 58 and stores a summary; the full form would be ~11 MiB per run, more than the rest of
the bundle. Attribution still works: model, tokens, seconds, attempts, fingerprint, and
whether the frame was called or taken from cache.

## The scratch work dir

`audio.wav` (137.9 MiB on lecture 1) and everything else stage 2–12 work through goes to a
temporary work dir, removed when the run succeeds; `--keep-workdir` keeps it, `--workdir`
names its parent.

## `report.json` — what stage 12 actually verified

| field | meaning |
|---|---|
| `ok` | the overall verdict: nothing required is missing, nothing contradicts, and the frame count agrees |
| `verified` / `absent` / `missing` | artefacts checked on disk, expected-but-absent optional ones, and required-but-not-found ones |
| `inconsistent` | where two artefacts contradict each other, or one of the pair cannot be read. Currently `captions.json` against `quality.json`: every alignment's `quality_gate` must equal the gate report's verdict for that frame (#87) |
| `source_had_video` / `quality_gate_required` | whether the bundle's own `source-provenance.json` says the input had video, and whether that made `quality.json` required. `null` means the record is absent or unreadable, and `quality.json` stays optional |
| `frames_expected` / `frames_found` | frame count against `manifest.tsv` |
| `note_promoted` | whether the vault copy happened |
| `files_checked` | files whose presence stage 12 confirmed (68 of 146 after #87: `captions.json` and `synth.json` joined `REQUIRED` and `quality.json` became required for a video source) |
| `files_in_bundle_excluding_this_stage` | everything present on disk the stage could have checked |
| `unchecked` | the absolute list of files stage 12 did not check |

Since `5c66731`, a reader can compute the coverage claim from the artefact itself: `verified ∪
unchecked = files_in_bundle_excluding_this_stage`.

`verified` is still presence-and-non-empty per file. The content check is `inconsistent`:
`captions.json` and `quality.json` describe the same frames from opposite ends of the pipeline,
and a disagreement between them fails the run with its own named reason rather than being read
as agreement (#87). This is the check that would have caught #82 from the bundle alone — the
two files were each internally consistent and contradicted each other.
