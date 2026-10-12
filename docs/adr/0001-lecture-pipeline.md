# ADR 0001: One-command lecture pipeline (video -> audited note)

- **Status:** Accepted
- **Date:** 2026-10-02
- **Scope:** MVP. GUI and local vision are explicitly later milestones.

> **How to read this file.** `D<n>` means "decision *n* within *this* ADR". The number is
> local to this file and collides with numbers in ADR-0004; references from anywhere else
> must be written `ADR-0001 Dn`.
>
> `## Decision` is the register. It is **not** the whole story. Everything from
> `## Amendment 2026-10-02` onward is a changelog: six dated amendments that changed
> decisions the register still describes in their original wording. Several register
> entries are therefore stale by construction.
>
> Each entry below carries a `Status:` line saying what that decision means **today** and
> where to read its current text. Read the status first; do not assume the register text
> is current. Amendments are kept rather than folded in because they carry the
> measurements that justify the change — see `docs/adr/README.md` for the reading order.

## Context

A lecture arrives as a video or audio file. The goal is a single Markdown note that
is readable, usable for exam preparation, and — critically — **mathematically
trustworthy**.

The synthesis step is performed by an LLM from a noisy ASR transcript. That step
produces fluent prose that can contain sign errors, rank/dimension errors and
internally inconsistent formulations. The author cannot reliably detect these:
it does not know when it is wrong.

This was measured, not assumed. An independent audit of a finished note found 8
high-confidence errors, including one where three representations of the same
dynamical system contradicted each other and the note's own prose (see
[Evidence](#evidence-the-error-that-justifies-this-pipeline)).

The pipeline therefore needs two properties that pull in opposite directions:

1. It must be **automated to one command**, because the manual path (transcribe,
   extract frames, write note, link terms, review) is long enough that it does not
   get done every lecture.
2. Its output must be **verified by something other than its author**, because the
   author's blind spot is exactly the thing we need checked.

## Decision

### D1 — The name carries no meaning; the interface does

**Status:** in force. Amended 2026-10-02 — the package was renamed; the rule is unchanged.

The package is `dyak`. The subcommand surface carries the meaning:

```
dyak process <video|audio>   # full pipeline
dyak audit <note.md>         # audit an existing note
dyak doctor                  # report missing/conflicting dependencies
```

Rationale: a CLI binary name cannot teach another agent what a tool does. `--help`,
the repository description and the skill description can. Names containing `ai`,
`auto` or `smart` were rejected — they make a promise that cannot be verified before
running the tool, and they go stale when the backing model changes.

**Amendment 2026-10-02 — renamed `dyak` → `dyak`.** The rule above is unchanged;
only the name is. Recorded because this section previously named `dyak` as the
choice, and an ADR that names a different package than the repository ships is a
document that actively misleads.

Two honest notes on the new name:

- It sits closer to the line the rule draws than `dyak` did. A "dyak" suggests
  a partial view, and this tool produces a complete, audited note. The mitigation is
  the same one that always applied: the description, `--help` and the skill text
  carry the meaning, and they are what another agent reads first.
- `dyak` is **taken on PyPI** ("Hierarchical visual models in C++ and Python").
  That is irrelevant for a `pipx install` from git, and only becomes a problem if
  the package is ever published under that name. Recorded now so the collision is not
  rediscovered later.

### D2 — dyak depends on `shipboard`, and does not reimplement STT

**Status: SUPERSEDED 2026-10-03 and again 2026-10-04. Do not implement this text.**
The shipboard dependency was removed (PR #50) and the backend became a pluggable
endpoint. Current text: *Amendment to D2 (2026-10-03)* and *2026-10-04 — D2 amended: one
less backend*. The error-propagation contract below survives in both; the shipboard
specifics do not.

STT is delegated: `shipboard process PATH` already transcribes an existing file to
stdout. whisper.cpp runs **CPU-only by design**, which keeps roughly 1.5 GiB of VRAM
free for llama-swap. A second STT stack inside dyak would duplicate that
configuration and silently compete for the same resources.

**Error propagation is part of the contract.** dyak captures shipboard's stderr
and surfaces it verbatim. If `shipboard` is absent, `dyak doctor` says so with a
remediation line; `process` refuses to start rather than silently degrading. A tool
that swallows its dependency's errors is worse than no tool, because the failure
looks like success.

### D3 — Frames are read from the source, never from a blurred intermediate

**Status:** in force.

Two passes:

1. `detect()` — decode at reduced width (640 px), blur, then a **single** `select` carrying
   both selection conditions: `isnan(prev_selected_t) + gte(t-prev_selected_t, GAP) +
   gt(scene, TH)`. Blur here is **only** a detector pre-filter.
2. `extract_at()` — seek the original source per timestamp, emit a single 1600 px
   frame at q=2.

Two bugs were found and fixed during this work, and are recorded because they are
the kind that recur:

- The blur was originally in the output path, so every emitted frame was blurred.
  Measured Laplacian variance was 1–3.6 (blurred) versus 686–3483 (correct).
- `select='gte(t-prev_selected_t,GAP)'` silently selected **zero** frames:
  `prev_selected_t` initialises to NaN, `t - NaN` is NaN, the comparison is false,
  and ffmpeg does not fail — it writes an empty file and exits 0. Fixed with
  `isnan(prev_selected_t) + gte(...)`.
- `mpdecimate` thresholds are absolute sums over 8x8 blocks, so a blur radius tuned
  for 640 px is wrong at 1280 px. The radius now scales with the detection width.

A 27-minute run that produced zero frames exited successfully. Any pipeline that
reports success must verify artefact counts on disk, not the exit code.

**Amendment 2026-10-04 — `mpdecimate` is gone, and it was hiding the timestamps.**

The `mpdecimate` step was **removed**, not retuned, and the timestamps were **wrong by a
factor of 30**. Both were found by running the real lecture end to end for the first time
with a working stage 6 and comparing the note against the baseline (#70).

`-frame_pts 1` writes the frame's PTS **in the output timebase**. After `-fps_mode
passthrough` that timebase is the input's frame rate — `1/30` for this lecture — so
`d_0000135437.jpg` was frame 135437, not 135437 ms. `detect()`'s docstring promised
milliseconds and returned frame indices. `extract_at()` then sought to `index/1000`
seconds, so all 16 frames came from the first 135 seconds of a 4520 second lecture — the
conferencing screen, before sharing started. `quality.json` carried the fingerprint:
14 of 16 frames OCR to exactly `19 chars`. Fixed with `-enc_time_base 1/1000`.

Separately, `mpdecimate` ran **before** `select`, so it dropped every frame of a static
stretch and `select`'s `max_gap` guarantee never saw them. `max_gap` was documented as
"guarantee a frame at least this often" and delivered 580.9 s. Reordering is worse, not
better — `select` then `mpdecimate` measured 1080 s, because `mpdecimate` then discards
exactly the frames `select` guaranteed. Two chained filters cannot guarantee anything
about the first one's output.

So the dedup condition moved **inside** the same `select` expression as the gap condition,
where neither can suppress the other:

```
select='isnan(prev_selected_t)+gte(t-prev_selected_t\,180)+gt(scene\,0.3)'
```

Measured on lecture 1: 58 frames, worst gap 180.0 s. The guarantee now holds, and 58 frames
across 4520 s is what "covering the lecture" actually costs. `Settings.hi` and `Settings.lo`
became dead knobs and `Settings.scene` replaced them; `blur` remains, now as a
scene-detection aid rather than a dedup aid.

The lesson generalises past this stage: **the `mpdecimate` step was what made the wrong
timestamps survivable.** Both bugs produced a plausible-looking bundle — 16 frames, a
manifest, a quality report, exit 0 — so nothing downstream had a reason to doubt them.

### D4 — Frame quality is gated, and the fix is crop, not a better model

**Status:** in force, but the *order* described below was wrong and has been corrected twice
by measurement. Current text: *Amendment 2026-10-03 — D4, and the gate reads the source* and
*Amendment 2026-10-04 — D4's gate, measured rather than asserted*.

Frames are poor because the screen is mostly UI chrome, not because the model is
weak. Measured: **58** distinct states over 75 minutes.

The "17" this paragraph originally carried was the hand-made selection in the baseline
note's frames directory, not a pipeline measurement. ADR-0005 D1 cited it as one. See
ADR-0005, *Amendment 2026-10-05*.

The pipeline asks the VLM for the document bounding box, crops to it, upscales the
crop 2x, applies an unsharp mask, and then gates the result on a sharpness measure
(Laplacian variance). Below threshold, the frame is re-cropped and re-measured.

Honest limit: the source is VP8 at 1.6 Mbps. Interpolation adds no information. If
the on-screen text is small, it cannot be recovered, and the quality gate should
**report** that rather than pretend otherwise.

### D5 — The audit is a separate model context, and it never rewrites

**Status:** in force.

The auditor receives the finished note and the raw transcript, and reviews the note.
It is not asked to check its own work; it did not write it.

Findings format is enforced:

- every finding carries an **exact quote** from the note and a citation into the
  transcript;
- every finding carries a confidence level;
- the auditor is explicitly permitted to answer "not sure";
- **a finding without a citation is discarded.**

The auditor does not edit the note. It emits a findings list. Repairs are a separate
action with their own log, so that "what was wrong" and "what was changed" stay
distinguishable.

### D6 — Math verification is layered, mechanical layers first

**Status:** in force.

An LLM asked to check formulas is **unreliably** correct. The reason it worked here is
that the prompt carried an explicit rank/dimension clause; two of the errors it found
were exactly the class an LLM is worst at. That is a fragile foundation, so the
architecture does not rest on it:

| Layer | Catches | Determinism |
|---|---|---|
| LaTeX render check | formulas that do not compile | exact |
| Rank/dimension check | scalar vs vector, index mismatches | exact on the covered subset |
| Cross-lecture contradiction | same term defined differently in two notes | high |
| Agent audit | sign errors, `⪰` vs `succ`, physically impossible claims | probabilistic |

The measured run supports this ordering: the mechanical layers cover the
rank/dimension class, and the agent layer caught what no script can — the sign of the
gravitational term, a semidefinite matrix asserted to give a unique global minimum,
and a non-symmetric matrix asserted to be positive definite.

### D7 — Progress is stage-level in the MVP, and the reason is a code fact

**Status:** in force. Note: the premise — that STT is one blocking request with no
streaming — was true of shipboard and is not true of the endpoint backend that replaced it.
Re-measure before relying on the weights.

`shipboard` sends **one** blocking HTTP request to whisper.cpp and waits; there is no
streaming and no per-segment callback. A per-segment progress bar is not available
without a change on the whisper server side.

So MVP shows stage-level progress with weights derived from measured cost, and the
audit stage dominates: ~20 minutes and ~106k context tokens, against 396 k input
tokens for the whole of stage 6. A progress bar that showed 90% during STT and then
sat still for 20 minutes would be a lie about where the time goes.

No dollar figure is quoted for either stage. No price for this gateway is recorded
anywhere in the repository, so this comparison is in tokens and seconds because those
are what was measured. An earlier revision read "versus ~$0.01 for vision"; that
number was never measured. #96.

### D8 — The vision backend is pluggable; local is deferred

**Status: in force. Implemented 2026-10-04 (#67).**
Stage 5 (`quality`) works through `GeometricEstimator`, the `DYAK_BBOX_SOURCE` default.
The `vlm` bbox source was never built and has been removed from the registry — an unbuilt
option that raised rather than being refused as unknown.

Stage 6 (`captions`) now calls a vision model. The transport was never the missing piece:
`llm.chat` passes `messages` through without inspecting them, so an OpenAI-compatible
`image_url` content part is all a vision request is. `llm.py` gained `DYAK_VLM_*`
configuration (falling back to the text names when unset), `vision_message()`,
`frame_fingerprint()`, and stage 6 captions each frame the gate passed, cached by
`(frame fingerprint, model id)` as this decision specifies.

Verified live against the gateway: 1.5 s and 4.6 s for two real frames, the first answered
`NO NEW INFORMATION` for a title screen, the second transcribed a references page. See
[`docs/stages.md`](../stages.md) § *Stage 6*.

What is still not built is the **vision bbox source** this decision originally described for
stage 5. The geometric substitute stands, and `GeometricEstimator` documents why.

Default backend is the gateway (`opencode-go/deepseek-v4-flash-vision-exp`), verified
working on Russian PDF pages, at 396 k input tokens per 73-minute lecture measured in
ADR-0005 D5. A dollar figure for this backend appeared in an earlier revision and was
never measured — no gateway price is recorded in the repository. #96.

A local llama.cpp + mmproj backend is a configuration swap, not a rewrite — but it is
not the default, because a 7B VLM on this hardware reads dense Russian technical text
worse.

### D9 — Artefacts live in the vault, not in this repository

**Status: SUPERSEDED 2026-10-03.** Artefacts now live in a run *bundle* under XDG state,
and the vault is an export target copied into. Current text: *Amendment to D9 (2026-10-03)*.
The intent below — nothing personal enters the repository — survives and still binds.

Transcripts, frames and audit reports are outputs, and they live beside the note in
the Obsidian vault. The repository carries code, ADRs and issue history only.

Frames are gitignored (`mscs/**/frames/`) — they are large, regenerable, and contain
names visible in the conferencing UI. Nothing in the repository may contain a
participant name, a personal IP or a hostname.

## The pipeline

> The 12-stage list below lives here, not in any `D<n>`. Code that cites the stage count
> should link to this section.

```
dyak process ~/Videos/lectures/.../1_lecture_OCS.webm

  0  doctor      verify ffmpeg/ffprobe, shipboard, gateway reachability, vault
  1  probe       ffprobe: streams, duration, codecs -> decide audio-only or not
  2  audio       extract 16 kHz mono wav to a managed temp dir (kept on failure)
  3  stt         shipboard process --timestamps json -> raw transcript
  4  frames      two-pass detect/extract -> frames/<name>/*.jpg + manifest.tsv
  5  quality     VLM returns document bbox -> crop, upscale, unsharp, gate
  6  captions    VLM caption per frame, cached by (frame hash, model id)
  7  synth       apply academic-lecture-synthesizer to raw + captions
  8  lint        LaTeX render check + rank/dimension check       [mechanical]
  9  audit       separate context, note + transcript, cited findings
 10  repair      apply confirmed findings -> fix log             [separate step]
 11  link        mscs-termlink, idempotent, must not link inside math
 12  report      note + audit report + fix log in the vault

  progress bar spans 0-12, weighted by measured cost
```

Exit codes distinguish causes, because a pipeline that fails silently is worse than
one that is loud:

| Code | Meaning |
|---|---|
| 0 | success; all artefacts written and verified |
| 1 | usage error |
| 2 | missing dependency (named, with remediation) |
| 3 | dependency failed (its stderr is reproduced verbatim) |
| 4 | quality gate failed — note written, audit report says why |
| 5 | audit found errors above threshold; note written and **flagged** |
| 6 | required artefacts could not be verified on disk (ADR-0001 D3) |
| 7 | model-written note is incomplete or degraded; review its warnings |
| 8 | less of the lecture reached the model than the coverage threshold allows; the note is missing part of it |
| 9 | a section the model wrote was cut off at the output limit; its ending is missing |
| 130 | interrupted (128 + SIGINT); work dir kept |

An uncaught internal error — a bug in dyak — has no row on purpose: it
propagates as a traceback. The work dir is retained and its path printed first.

### Amendment 2026-10-12 — the required set, and a cross-artefact check (#87)

Stage 12's per-file contract was presence-and-non-empty, and `REQUIRED` named eight root
artefacts plus the frames. On the one real bundle that was 65 of 144 files, and two of the
files outside the check were load-bearing: `captions.json` — stage 6's entire product — was in
neither `REQUIRED` nor `OPTIONAL`, and `quality.json` was optional, so the gate report's own
verdict was verified by nothing. A `quality.json` truncated to one byte exited 0, because
`_rejected_by_gate` caught `JSONDecodeError` and returned an empty set: `frames_found` still
equalled `frames_expected` and the run reported success.

Three changes, and the third is the substantive one.

1. `captions.json` and `synth.json` join `REQUIRED`. Losing either makes a stage's claim
   unfalsifiable: `captions.json` is the alignment stage 6 produced, and stage 12 already read
   `synth.json` back for coverage and truncation (#117, #128).
2. `quality.json` is required **when the source had video**, and required-ness is read from the
   bundle's own `source-provenance.json` (stages 1 and 2). The reason it was optional is real —
   the gate runs on frames and an audio-only source has none — so an unconditional promotion
   would fail a legitimate bundle. Unknown (record absent or unreadable) keeps it optional.
3. `captions.json` and `quality.json` are cross-checked: every alignment's `quality_gate` must
   equal the gate report's verdict for that frame, and a frame the gate report does not mention
   is itself a disagreement. This is the check that would have caught #82 from the bundle
   alone: `captions.json` said every frame passed and `quality.json` said one failed, and each
   file was internally consistent.

`ok` therefore means "nothing required is missing, nothing contradicts, and the frame count
agrees". A contradiction is a verification failure and keeps code **6**: the artefacts are
present, and the verification that they are what they claim to be failed. It gets its own named
reason (`inconsistent`, carrying the frame and both verdicts) rather than a new code, because
code 6 already names exactly this class — "required artefacts could not be verified" — and a
separate code would split two failures whose remediation is the same.

`_check` stays presence-only. A byte-size threshold was considered and rejected: a parse, a
count or a cross-reference is what the issue asked for, and a size floor is a proxy that passes
a truncated file that happens to be large.

### Amendment 2026-10-11 — code 8, and why it is not code 6

Stage 7 cut the lecture into time slices and mapped the eight required sections onto the
first eight of them (`synth.py`, `synthesise`). On a 58-frame lecture that gave
`Вопросы для самопроверки` the text spoken between 2695 and 3079 seconds and dropped
slices 9-12 entirely: about 31% of the lecture reached no request, while stage 12
verified every file and the run exited 0 (#117). The prompt also labelled each request
with the slice name (`«Фрагмент 7»`) rather than the section's own title, so the writer
was never told which of the eight sections it was writing.

Both are now fixed in stage 7: every section request carries the whole lecture, and the
skill's own number, title and brief. Stage 7 also records `coverage` in `synth.json` —
words that reached the model over words in the transcript — and stage 12 reads it back
and refuses exit 0 below `synth.COVERAGE_THRESHOLD` (0.95).

That refusal gets its own code, **8**, and deliberately not 6. Code 6 is "an artefact
could not be verified on disk"; here every artefact is present and verified and the
*deliverable* is still missing a third of the course. Folding the two together would
make `report.json`'s `ok` mean two different things, which is the confusion that let
this run exit 0 in the first place. Code 8 is checked before code 7 for the same reason:
a shortened transcript also makes the note degraded, and returning 7 would report the
symptom instead of the cause.

### Amendment 2026-10-11 — code 9, and the two gates stage 12 was missing

Stage 12's contract was presence-only: `_check` tested that a required file existed and was
non-empty, and `ok` compared the frame count against the manifest. Nothing read the *content*
of what stage 7 wrote, so the two failure classes this pipeline exists to catch both passed
through it clean (#128).

Three gates now, and they are deliberately not the same kind of gate:

1. **Transcript coverage** — read back from `synth.json` and gated (code **8**, above).
2. **Share of sections left as `в лекции не затрагивается`** — reported, never gated. The
   skill's eight sections are an assumption about the lecture's shape. A humanities lecture
   legitimately leaves 4-7 empty, and any threshold here would fail a correct note. The raw
   numbers (`sections_total`, `sections_not_covered`, `sections_not_covered_share`) are in
   `report.json` so a reader can judge them; the run does not.
3. **Sections cut off at the model's output limit** (`finish_reason == "length"`) — gated,
   with its own code, **9**. The text is there, it is real, and its ending is missing, which
   is precisely the shape that passes lint and audit as finished content (#124).

Code 9 is separate from 8 because they are opposite failures: 8 is about what reached the
model, 9 is about what came back. It is separate from 7 for the reason 8 is — a truncated
section also makes the note degraded, and 7 would name the symptom rather than the cause. It
is checked *after* 8, because a shortened transcript can shorten the sections too, and the
input is the thing to report first.

All three are read back from `synth.json` rather than recomputed. Stage 12 measures; a second
implementation of stage 7's arithmetic here would be a second thing to keep true.

### Amendment 2026-10-11 — the bundle records its own run, and verifies its own artefacts (#92, #88)

D9 makes the bundle the artefact root, and the exit code is the project's headline promise.
Both were claims about a run that the run did not record.

`write_timings` was the last statement before `return RunResult(...)`, so `timings.json`
existed only on runs that completed — exactly the runs that were not slow and not broken. And
nothing in the bundle marked run state at all. Stage 7 publishes `note.md` into the bundle, so
an interrupt during stage 9 left a directory holding `note.md`, `captions.json`,
`quality.json` and `lint.json`, with no `report.json`, no `timings.json`, and nothing saying
the run had stopped. To a reader that is a successful run whose verification artefacts happen
to be missing. The pipeline could not tell its own incomplete bundle from a complete one, and
neither could anyone else.

The decision is that a run writes `run-state.json` — a run id, `started_at`/`finished_at`, the
stages completed, the stage reached, and `completed` / `interrupted` / `failed` with the
exception type — and that it does so from a `finally`, together with the timings. `run` is now
a thin wrapper around the stages for this reason and no other: the state has to be written on
the paths that do not return.

Two details are deliberate. `stage_reached` is the stage that was *running*, not the last one
that finished, because "the run stopped after 8" is true but not the useful half. And the file
is a sidecar, unregistered like `timings.json`: it records the run rather than being a product
of a stage, and it is written after stage 12 has already verified the artefacts. It is in
`RUN_ARTEFACTS` so the next run clears it — a stale `completed` beside a fresh bundle is the
one file a reader would mistake for this run's.

This closes the "nothing says when the run happened" half of #88 as well.

**The other half of #88 is the same defect one level down.** `REQUIRED` names are bundle-root
names, but the registry keys are not: stage 3 registers `json` / `txt` / `raw`, stage 4
`manifest`, stage 9 `audit/audit.json`, stage 10 `repair/repair.json`. Resolution was
`artefacts.get(name) or (root / name)`, so for every entry whose key differs the lookup missed
and the check fell through to "a file with this name is sitting in the bundle root" — which a
previous run's file satisfies just as well as this one's. #88 recorded this as three of the
eight; it is **six**, because the three transcript entries miss the same way. The fallback was
load-bearing, not defensive: it was the only thing resolving them.

Resolution is now over the registry twice — by key, then by the name the registered path
carries in the bundle — and never over the filesystem. A name that matches nothing a stage
registered is a failure with a reason. Two consequences are worth stating. The property no
longer depends on `clear_root` having run: a stale `audit.json` is not found even when the
clear is deliberately skipped, which is a stronger claim than #99 could make. And the
`_stub_stt_write` in `test_stages` was naming its files `t.raw.json` / `t.txt`, so the three
REQUIRED transcript entries could not resolve in the one test that drives the real
`pipeline.run` — #88's subject was invisible there precisely because the fake was unfaithful.

## Amendment 2026-10-02 — stages 0-3 shipped, and two corrections

Recorded because the decision text above described a pipeline whose stage 3 was
still blocked, and an ADR that describes a state the code has left behind is a
document that actively misleads.

**Stage 3 is no longer blocked.** `shipboard#6` landed
([PR #7](https://github.com/nicklin11/shipboard/pull/7)). Two of its premises were
measured wrong, and both corrections changed the consumer:

- the default `response_format=json` returns **only** `{"text": ...}` — segments
  exist solely under `verbose_json`, so there was nothing to read off the plain
  transcript. Timestamps are consumed from `--timestamps json`, never reparsed
  out of stdout text;
- `start`/`end` are **seconds as floats**, not milliseconds.

Per-**word** timings come with the same payload. Stages 5-7 bind frames to
paragraphs by word span rather than segment bounds, because segment edges are
decoder artefacts and will not line up with paragraph structure. The `word`
field holds BPE pieces (`" Ин"`, `"ст"`, `"ит"`), so they are carried verbatim
and never detokenised: reassembling them needs whisper.cpp's exact vocabulary,
and a wrong guess corrupts the transcript while leaving every timestamp
plausible.

**Stage 1 ships with stage 2-3.** The stage table assigns no issue to `probe`,
but stage 2 has nothing to extract from without it, so it shipped inside #5
rather than being deferred or, worse, hardcoded. An audio-only input is
**rejected** (exit 1), not accepted: *Not doing* already excludes it, and stage 4
has no frames to extract from an audio file.

**Measured stage costs**, for D7's weights. One end-to-end run on lecture 1
(`1_lecture_OCS.webm`, 4396 s container, 4520 s of audio):

| Stage | Wall | Rate |
|---|---|---|
| 1 `probe` | 0.0 s | — |
| 2 `audio` | 6.4 s | 138 MiB wav written |
| 3 `stt` | 257.2 s | **0.057x realtime** |

STT still dominates the implemented stages by ~40x over extraction, and the
audit (~20 min) will dominate the whole pipeline. Note the short-clip trap: a
25 s probe measured 0.20x because the whisper.cpp warm-up is a fixed cost that a
four-minute lecture amortises away. A progress bar weighted from short-clip
numbers would overestimate STT by 3.5x.

The run produced 1677 segments and 21957 timed words, speech covering 99.88% of
the audio, with zero anomalies.

**The preflight gate is scoped to what the run uses.** Stage 0 as a gate must
not be broader than the stages it guards: `ffmpeg`, `ffprobe` and `shipboard` are
fatal for `process`, the vault and the gateway are not. Stages 0-3 write only into
the managed work dir and never call the gateway, so refusing to transcribe
because stage 12's output directory is missing — or because stage 5's VLM backend
is offline — would be a gate protecting nothing. `dyak doctor` keeps both
fatal, because its job is to report the whole environment.

**`dyak process` exits 1 until stage 12 exists.** Exit 0 means "all artefacts
written and verified" in the table above, and no note is produced yet, so a
successful partial run claiming 0 would be the silent degradation D2 exists to
prevent. The work dir is kept on failure and its path printed: a failed
transcribe that discards its extracted wav costs a 73-minute re-extraction to
debug.

## Amendment 2026-10-03 — stage 4 shipped, and the frame set is not reproducible without a record

`frames` was not written from scratch. It is a move of `lecture-frames`
(`~/.local/bin`, 294 lines), which was the only place the D3 blur fix existed.

**The port is byte-identical to the script it replaces.** Verified by running both on
`1_lecture_OCS.webm` and comparing: `manifest.tsv` is identical, all 16 frames compare
equal under `cmp`, total 2 382 320 bytes on both sides. Three independent runs today
produced the same 16 timestamps, so detection is deterministic here.

**Measured stage cost.** Stage 4 is **287 s** end to end: 232 s detect (a full decode of
the container) and ~55 s extract. That is **0.065x realtime** and it puts stage 4 within
11% of stage 3's 257 s. The amendment above said STT "still dominates the implemented
stages by ~40x over extraction" — true for audio, **no longer true for the pipeline**:

| Stage | Wall | Rate |
|---|---|---|
| 1 `probe` | 0.0 s | — |
| 2 `audio` | 6.4 s | 138 MiB wav written |
| 3 `stt` | 257.2 s | 0.057x realtime |
| 4 `frames` | 287.0 s | **0.065x realtime** |

Stage 4 does not consume the audio at all, so it cannot be folded into stage 3's budget
and cannot be amortised by a longer lecture any differently.

**D7 requires a note that this cost is paid twice if it is not reused.** Detect is the
whole-container decode; re-rendering frames at a different output width must not pay it
again, so `run()` takes `reuse_manifest=True` to skip the detect pass. That is the only
reason the manifest format was kept at all.

**The stored manifest in the vault is not a golden file, and the port found out why.**
`frames/Лекция 1. 01.10.26/manifest.tsv` lists **17** states; the same ffmpeg build
(n9.0.2, 2026-09-22) on the same unmodified source lists **16**, with only 9 timestamps
in common. Same binary, same input, 28 hours apart — so the difference is the *settings*,
not the environment, and the manifest alone does not record which settings. `run()` now
writes `detect.json` beside it: the resolved filter string, every `Settings` field, the
ffmpeg build, and whether the manifest was reused. Without that record a later reader
cannot tell whether the frames or the code changed, and the set cannot be reproduced.

**The retry for ffmpeg's input-open failure was masking a different fault.** The ported
script retried `Error opening input` four times unconditionally, and on this host that
masked a **renamed parent directory**: ffmpeg reported "No such file or directory" five
times in a row for a file that was simply at a new path, and the script's own message
blamed ffmpeg for what was a missing input. `run()` re-checks `source.is_file()` before
each attempt and raises **exit 1, usage** if the path is gone. Only a source that is
verified present on every attempt still earns the retry, and its message says so.

**Zero frames is a failure, not an empty success.** The ported script wrote a
header-only manifest and exited 0. That is exactly the D3 failure mode — artefact counts
verified on disk, never inferred from an exit code — and it is how a 27-minute extraction
could produce nothing and still report success. `run()` raises **exit 3** on a detector
that selects nothing, and again, one level deeper, when ffmpeg exits 0 having written no
frame file.

**Stage 4 does not honour `--max`.** The script's `--max` cap on frame count was dropped:
it samples the *detector's* output before extraction, so a capped run still pays the
whole 232 s decode and then discards work. Sampling belongs to stage 6 (ADR-0005), where
the frames are read by a VLM and the cost is real money.

## Amendment 2026-10-03 — D2 delegated to an endpoint, D9 to a local bundle

Two decisions changed because the code was measured rather than reasoned about.

### Amendment to D2 (2026-10-03) — STT is delegated to an *endpoint*, not to a utility

`TranscriptionBackend` is the boundary: `check()` answers "can you transcribe", `run()`
returns the raw payload, and nothing above the backend parses anything. shipboard is one
implementation and is **no longer mandatory**; `doctor` probes whichever backend is
configured.

**The premise this replaced was wrong on this host.** whisper.cpp does not serve the
OpenAI-compatible routes:

```
GET  /health                    -> 200
GET  /v1/models                 -> 404
POST /v1/audio/transcriptions   -> 404
POST /inference                 -> 200
```

An OpenAI-only backend cannot run here, so both wire formats exist: the native
`/inference`, and an OpenAI-compatible adapter for hosted endpoints.

**shipboard does normalise the envelope.** `/inference` returns an object
(`{task, language, duration, text, segments, ...}`); shipboard returns a bare array. The
segment contents are identical — same `words` keys, same float seconds — but a parser
written for either shape alone would have failed on the other. `core.parse` accepts both
and records the envelope as an anomaly, so a payload that changes shape is visible rather
than silent.

**D2's verbatim-diagnostics guarantee is preserved, not dropped.** shipboard is a
subprocess and had stderr; an HTTP backend has none. The intent — never swallow a backend's
diagnostics — is carried over as the verbatim response body plus the transport-level
message, and HTTP status failures attach the response body as `stderr`. Losing the guarantee
silently would have turned a backend failure into an empty transcript with exit 0.

**The endpoint is not reproducible.** Measured, 25 s clip, three runs per configuration:

| request fields | identical 3x | segment counts |
|---|---|---|
| shipboard's own | no | 11, 11, 11 |
| `temperature` + `no_timestamps` | no | 11, 11, 11 |
| `threads=1` | no | 10, 11, 11 |
| `threads=2` / `threads=4` | no | 11, 11, 11 / 11, 11, 10 |
| after a fresh container restart | no | 11, 10, 11 |

Differences are punctuation and a segment that appears or does not, on **identical
timings**. Ruled out: thread count down to 1, language on or off, temperature on or off,
container state. Cause not identified. This is why two runs over the same 4520 s audio
produced 1677 and then 1520 segments.

Consequences, both of which change what "verified" means here:

- `transcript.json` is **not** a reproducible artefact. D3's rule — verify on disk, never
  infer from an exit code — still holds, but "the same run" is not a stable reference.
- Frame-to-paragraph binding uses word spans, and the timings *were* stable across
  non-identical runs, so stages 5-7 are unaffected. Only the wording moves.

Stage 4 is the opposite: `frames` output is deterministic, three runs gave the same 16
timestamps. A regression check can still be exact on the frame half.

### Amendment to D9 (2026-10-03) — the vault is an export target, not the artefact root

Deliverables go to a **bundle**: `--output-dir`, else `$XDG_STATE_HOME/dyak/<lecture>`.
The vault, when given, is copied into.

**Not `./output/<lecture>`.** D9 exists because artefacts written into a working directory
end up in whatever repository the user is standing in. A CWD-relative default reintroduces
that one level down — a 137.9 MiB wav and a 3.7 MB transcript in a git checkout. XDG
state is per-user, per-purpose, and the conventional home for run output that should
survive.

Cyrillic directory names are preserved rather than transliterated. The lectures here are
`Оптимальные СУ`; rendering that as `Optimal Control Systems` would give a directory whose
name does not match its contents.

**One writer, one verification.** `export_to_vault` copies and then compares each
destination against its source. Two write paths with different semantics is how a tool
ends up with half a note in the vault; a copy that lands short raises rather than being
reported as done, and the bundle — the single writer — is left intact.

The work dir and the bundle are separate because their lifecycles are opposite. The work dir
is scratch, removed on success. `audio.wav` deliberately stays there: it is 137.9 MiB,
reproducible from the source in 6.3 s, and nothing downstream reads it.

## Amendment 2026-10-03 — D4, and the gate reads the source

Stage 5 shipped with two changes to D4, both forced by measurement.

### The order in D4 was wrong

D4 said: crop, upscale, unsharp, **then** gate on Laplacian variance. Gating after
enhancement means scoring the sharpening this stage just applied — the gate would be a
function of its own parameters. Change `unsharp_amount` and the verdict moves. Change the
JPEG encode quality and it moves again.

The shipped order is:

```
crop -> MEASURE (the gate) -> upscale 2x -> unsharp -> measure again (recorded, never gated)
```

Measured on lecture 1, bright document frame, every value at the same 640-wide analysis
raster and through the same encode the stage writes:

| state | variance | vs source |
|---|---|---|
| as extracted | 4 317 | x1.00 |
| whole frame, 2x lanczos | 4 493 | x1.04 |
| whole frame, 2x + unsharp 1.0 | 5 341 | x1.24 |
| cropped, 2x + unsharp 1.0 | 5 962 | x1.38 |

**A first version of this measurement said x14 and x2.9, and that was wrong.** It piped the
enhancement to `rawvideo`, bypassing the JPEG the pipeline writes; DCT quantisation at q=2
removes most of what the upscale creates. Measuring through a convenient proxy rather than
through the artefact is the second time this project has produced a confidently wrong
number that way (the first was comparing whisper.cpp responses through different envelopes).
The x12.1 figure is the rawvideo path; x1.38 is the real one.

The measure is a sound focus detector — 4 317 unblurred, 440 at `gblur=sigma=1`, 65 at
sigma=2, 8 at sigma=4, and exactly 0.0 on a uniform field — but its value depends on the
analysis raster width, which is why `analyse_width` is fixed configuration and recorded in
provenance rather than inferred.

### The bbox is geometric, and the VLM path is absent rather than stubbed

D4 assumes the VLM returns the document bbox. There is no vision client in this codebase
and no gateway configured, so that path cannot run and a stub returning a plausible box
would be worse: stage 6 would caption the wrong region, invisibly.

The frames do not need it. On the 32x18 luma grid the bright frames carry a document pane
over roughly 78% of the frame with a dark UI strip down the left, and the dark frames carry
no bright region at all. `estimate_box` takes the union of bright rows and bright columns on
a 96-cell grid, requires 10% coverage, and returns `None` when there is nothing. `bbox_source`
is recorded as `geometric` so a later VLM source is distinguishable in provenance.

### Threshold 300, calibrated on one lecture

All 16 frames as extracted: 96, 298, 1014, 1400, 1489, 1569, 1627, 1727, 1906, 1973,
2095, 2103, 2449, 2698, 3017, 3217, 3813, 4317, 4357.

300 sits above the two clear failures (96, 298), far below the lowest plausible sharp frame
(1014), and 4.6x above the sigma=2 blur level. **One lecture is not a calibration set.**
The distribution is recorded here so it can be re-derived on more material rather than
rediscovered, and `quality.threshold` is configuration.

### Two failure modes, reported differently

`f_0000000000` fails with "no bright document region found", at sharpness 298. Its problem is
content, not focus. Conflating that with a blur failure would send the reader to sharpen a
frame that has nothing to crop to, so the two are distinct reasons in the report.

### "With the note still written" is not achievable here

D4's exit-4 path requires the note to survive a failed gate. The note is stage 7, which does
not exist. `quality` raises exit 4 and the quality report is the artefact; the acceptance
criterion is recorded as unsatisfiable rather than quietly met by a placeholder.

Refs #9.
## Evidence: the error that justifies this pipeline

Section 7.3 of the finished lecture 1 note contained three mutually exclusive
representations of the Kapitza pendulum. The ODE carried `+(g/l)·sin θ`, and the
(2,1) element of both `A` matrices carried the same sign — while the prose stated the
correct eigenvalues.

Verified numerically at g=9.81, l=1, k=0.3:

```
as written (+g/l):  theta=0 -> lambda = ±2.99, -3.29   => SADDLE
correct  (-g/l):     theta=0 -> lambda = -0.15 ± 3.13i => STABLE
```

The formulas contradicted the note's own text and contradicted physics. Two further
rank/dimension errors were of the same species: a scalar added to a vector, and a
`(1x2)(2x1)` product described as yielding a vector — the latter written explicitly as
a demonstration that dimensions *did* agree.

The errors were fixed and re-verified: with the corrected sign, the lower equilibrium
is stable (centre at k=0) and the upper is a saddle, matching both the prose and the
physics.

## Consequences

**Good**

- Formula defects that the author cannot see are found before the note is used.
- Findings are citable and attributable: synthesis defects (absent from the raw
  transcript) are separable from mis-transcribed lecturer speech.
- The pipeline is honest about its own limits — quality gates report failure instead
  of producing plausible-looking output.

**Bad**

- The audit is the slowest stage by a wide margin (~20 min). This is a real cost and
  the progress bar must not hide it.
- An audit that is wrong is a *worse* failure than no audit, because a human who
  cannot check formulas will trust it. This is why D5 requires citations and D6 puts
  mechanical layers underneath: the agent layer is never the only line of defence.
- Frame quality is capped by the source bitrate, and 58 states over 75 minutes means
  this input is visually sparse. The pipeline must not oversell visual recovery.

## Not doing (MVP boundaries)

- GUI. Later milestone; the CLI is the product.
- Local VLM as the default vision backend.
- Audio-only input path beyond what the video probe naturally produces.
- Cross-lecture RAG. The term catalog and Backlinks already provide cross-lecture
  indexing; a vector index would duplicate it.
- Automatic repair without review (D5).

## Open questions

1. Which model should be the default auditor? `mimo-v2.6-flash` was measured on one
   note and passed, but one note is not a benchmark. The right test is planted errors
   with a measured recall, run across candidate models.
2. Should the audit result block the note's write, or annotate it? Currently the
   proposal is annotate-and-flag (exit 5), which trades away a hard guarantee for
   never losing work.
3. Is `mmproj` worth evaluating at all, given the frames are already sharp?
EOF
## Amendment 2026-10-04 — D4's gate, measured rather than asserted

Stage 5 shipped a Laplacian-variance gate over a fixed 640-wide analysis raster, with the
threshold set at 300 and a bbox heuristic that decided which frames counted. An adversarial
review of that design produced four claims. Each was tested against the 16 extracted
frames before being believed. Two held, one held in mechanism but not magnitude, and one
was wrong on its own terms. The numbers below are from the written artefacts, which is the
only place a number in this pipeline is allowed to come from.

### Confirmed: the bbox heuristic was a quality gate in disguise

`evaluate()` returned `passed=False` whenever `estimate_box()` found no bright region. That
rejected two frames scoring **85 580** and **91 629** MEGE — middle of the passing
distribution, roughly 200 000 edge pixels each — because their canvas is dark. Dark-mode
Beamer themes, MATLAB canvases, terminals and blackboards are valid content for this
lecture. Signal quality and layout classification are different questions and were being
answered by the same predicate.

D4's outcome is now three orthogonal measurements:

- `quality_gate`: focus only, which is what D4 asks for.
- `content_type`: `white_document` | `dark_canvas` | `unknown`.
- `crop_state`: `cropped` | `full_frame_fallback`.

A bbox that cannot be found falls back to the full frame. Nothing is rejected for lacking
one. Lecture 1 goes from 12/16 to **14/16**, and the two remaining failures are separated
by cause: one `EMPTY_CANVAS` (luma std 10.2, p99 45 — nothing on it), one `SOFT`.

`BBoxEstimator` is a Protocol with three implementations: `GeometricEstimator`,
`NullEstimator`, and `VLMEstimator`, which raises `NotConfiguredError` rather than
inventing a box. A fabricated box would make stage 6 caption the wrong region with no
visible symptom; a missing one only costs a wider crop.

### Confirmed: the metric was reading content density

Five frames whose *per-edge* Laplacian sharpness is constant (0.036–0.045) were selected,
so their stroke sharpness is equal by construction. Their total Laplacian variance spans
**7.21x**: 294.8, 1 021.0, 1 741.7, 1 927.4, 2 124.4. Pearson r(edge count, variance) =
0.841. The old threshold separated slides by how much text was on them.

Replaced with MEGE, the mean of `Gx² + Gy²` over pixels above a noise floor (τ = 30). The
edge population divides density out, and the same five frames then spread **1.66x**. The
residue is real: MEGE still rises with stroke *width*, because a 72 pt header and 12 px
body text have genuinely different gradient slopes at identical focus. It is content, not
focus, and 4.3x is what removing the confound is worth, not more.

A unit test now asserts this directly: four copies of the same strokes give 4x the edge
pixels and a MEGE ratio of 1.000.

### Measured, not a bug: the JPEG artifact trap

Stage 4 wrote q=2 JPEG and stage 5's second-derivative operator is an 8x8 detector, which
is the same lattice JPEG quantises. Re-extracting every frame as lossless PNG and measuring
both through the real analysis path gives a delta of **-1.02% to -1.53%, mean -1.26%**.

The mechanism is real; the magnitude is not. The 640-wide rescale runs before the
Laplacian and destroys most block-edge energy first. Stage 4 writes PNG anyway, because a
known signed bias does not belong in a gate, but this is hygiene and it is recorded as such
rather than as a rescue.

### Rejected: the proposed calibration rule

Blur injection on all 16 frames: MEGE falls to **half** its unblurred value at σ 0.8–1.0,
tightly, on every frame. That is the degradation level the gate is placed at.

The rule proposed for the threshold, `min(MEGE(σ=1.2))` across the sharp frames, evaluates
to **24 025** — and all 16 frames score above it. It passes everything. A minimum is the
wrong order statistic for a gate: it is dragged down by the worst frame you happened to
capture.

What is used instead is `decay × median(MEGE over content frames)`, with `decay = 0.43`
from the σ=1.0 half-value point and an absolute floor so a mostly-blank run cannot normalise
its way to "everything passes". The median, not the minimum; and the ratio rather than an
absolute value, because MEGE depends on stroke width, contrast and resolution and is not a
unit that transfers between lecture series. Lecture 1 yields a threshold of 39 400.

### Also changed: the raster is native

Scaling every crop to a fixed 640-wide raster was defended as "constant raster, so the
threshold is comparable". The crops *are* comparable — because the threshold is now derived
from the run rather than fixed. What the fixed raster actually did was apply 3x
anti-aliasing to one crop and 1.5x to another: lecture 1's three crops have scale factors
0.457, 0.480 and 0.582. Measurement now happens at 1:1, on the crop or the full frame.

This costs numpy. In pure Python the pass over 1.4 M pixels is ~20 s per frame, which would
make the quality gate slower than speech recognition.

### Two bugs found underneath

Worth recording separately, because both were invisible for the same reason.

`WorkDir.sub()` creates the *parent* of the path it returns. `work.sub("quality")` therefore
returned a path whose own directory did not exist, and the first write raised
FileNotFoundError. It never fired because the earlier ordering bug — stage 4 moved the
frames into the bundle before stage 5 read them, so every frame failed — aborted the stage
first. Two stacked defects, the first masking the second. The bundle from the end-to-end
run has no `quality.json` in it, which is the evidence.

And the regression test written to catch the ordering bug was calling `pipeline.run` while
`glp.run` was still bound to a stub from an earlier section. It passed vacuously for its
whole life. Restoring the real `run` before the call exposed three more stale stubs in the
same block (`MediaInfo` with a `container` kwarg that no longer exists, `AudioArtefact`
with `bytes_written`, `Transcript` with `duration`). A test that cannot fail is worse than
no test: it reports coverage it does not have.

### Consequence for the remaining stages

Stage 8 (deterministic lint) and stage 12 (report) must be zero-model and must be tested
against artefacts on disk, not against stubs of themselves. Stage 9's deterministic tier
carries the checks that do not need a model; its critic tier is a different interface from
stage 7's synthesis, because a model reviewing its own output in the same context will
ratify the error it is shown.

## Amendment 2026-10-04 — stages 11 and 12 landed, and the first real 12-stage run

The MVP is 12 stages. The run below is the first one to execute all of them against
`1_lecture_OCS.mp4` (AV1 1920x1080, AAC 48 kHz, **4520.1 s**) and it changed two things.

### The infrastructure, not the pipeline, is what fails on a long lecture

Two attempts died at stage 3. Neither was a dyak defect, and both are in shipboard:

- `whisper_wake_proxy.py:152` sets `HTTPConnection(..., timeout=300)`, hardcoded and not
  configurable by environment. A request whose backend work exceeds 300 s returns
  `503 {"error":'timed out'}`.
- `whisper_idle_stop.sh:21` stops the container after 300 s of an un-refreshed marker, and
  the proxy refreshes that marker only in its wake path — **not** during the forward, despite
  the comment in the idle-stop script asserting that it does "before and during". The result
  is `docker stop --time 5` mid-request: the client sees `Remote end closed connection
  without response` and the container is left at `Exited (137)` with `OOMKilled=false`.

whisper.cpp handles a whole lecture in **one** request, so the 300 s boundary is a hard
ceiling on lecture length for this host — roughly 65 minutes. The 257.2 s figure recorded
earlier came from the 4396 s `.webm` and fitted underneath it. **That number was a
measurement that happened to clear a threshold; it was not evidence that the pipeline scales
to lecture length, and reading it as such is the error.**

### Measured stage costs (D7's weights)

| Stage | Wall | Note |
|---|---|---|
| 1 `probe` | 0.1 s | |
| 2 `audio` | 3.5 s | 137.9 MiB wav |
| 3 `stt` | **315.4 s** | 4901 segments, 21837 timed words — **0.070x realtime** |
| 4 `frames` | **319.2 s** | 16 frames, 5.2 MiB, median gap 8 s |
| 5-12 | **7.7 s** total | quality, caption, synth, lint, audit, repair, link, report |
| | **645.9 s** | end to end |

The two-pass frame design and STT are within 4 s of each other, and together are **98.8%** of
the run. Everything the pipeline does to the *note* — quality gating, alignment, synthesis,
lint, audit, repair, linking, verification — is 1.2%. D7's claim that a per-segment progress
bar is unavailable is unchanged and now has a second reason: 99% of the wall time is in two
stages that report nothing until they finish.

Stage 3 is slower per unit audio than the recorded 0.057x, on longer audio and through a
container that was already warm. The short-clip trap recorded earlier still stands.

### Stage 12 caught a bug in itself, on its first real input

It reported `frames 0/16` on a bundle holding all 16 correct frames. `count_frames` globbed
`*.jpg` in the bundle root; frames are published into `images/` and are `.png`, while the
`.jpg` files beside them are the quality gate's enhanced `<frame>_q.jpg` derivatives — 15 of
them for 16 frames. A glob that had matched would have counted 31.

**A verification stage that has only ever run against a directory it built itself is not a
verification stage.** The manifest already names every file that must exist, so each row is
now stat'd by name; that also makes a frame whose derivative survived a detectable loss,
because the directory holds 18 files either way.

### What this run does not establish

The synthesis ran in template mode and the audit's tier 2 did not run, because
`DYAK_LLM_ENDPOINT` is unset. **The 0 findings this run reports are not evidence about the
audit.** Tier 1 ran four deterministic rules against a template note; the 21-finding baseline
was measured against a hand-written note. Comparing the two sets would compare a template
against prose, which is not a regression check in any direction.

---

## 2026-10-04 — D2 amended: one less backend, and the reason was not the one claimed

The subprocess backend is removed. Stage 3 now reaches whisper.cpp native `/inference` or
an OpenAI-compatible endpoint, and nothing sits between the pipeline and the transcriber.

**D2 is not rewritten.** The text above records what was decided, and a reader needs to see
what was believed at the time. This amendment states what changed and on what evidence.

### The justification that turned out to be false

The case for keeping the subprocess backend, and the first case for removing it, was that it
returns a payload *byte-identical* to `/inference` — a claim carried in the backend's own
docstring and repeated into issue #43.

The measurement required before deleting it says otherwise. Same 45 s of lecture 1 audio,
each backend run twice, payloads parsed through `core.parse` and hashed:

```
whispercpp#1   segments=14  sha=3e4a8a15ce634d54
whispercpp#2   segments=15  sha=8b0eeb93767727e6
shipboard#1    segments=15  sha=131dfd739fe90bb2
shipboard#2    segments=15  sha=59f61cb0881b5978

within whispercpp, run1 == run2 : False
within shipboard,   run1 == run2 : False
```

**The endpoint disagrees with itself between two runs seconds apart.** Within-backend
variance is at least as large as cross-backend variance, so the difference cannot be
attributed to the backend at all. No run pair on this host has ever produced identical
bytes, including the backend against itself.

What survives is narrower, and is already stated correctly in `core.parse`: the subprocess
backend returned a **bare array** where `/inference` returns the whole `verbose_json`
object, and `parse()` unwraps either. That unwrap is its entire contribution.

### Why removal still holds

Not on byte identity. On two grounds that were measured:

1. **It broke the pipeline twice, and both failures were its own.** The wake proxy hardcodes
   `HTTPConnection(timeout=300)`, not configurable by environment, so a 4520 s lecture
   returned `503 {"error":'timed out'}`. And `whisper_idle_stop.sh` stops the container after
   300 s of a marker the proxy refreshes only in its wake path — its comment claims "before and
   during", and the "during" does not exist — so the container was killed mid-request
   (`Exited (137)`, `OOMKilled=false`). Because whisper.cpp takes a whole lecture in one
   request, that ceiling bounds lecture length at roughly 65 minutes on this host.
2. **It added a program that did nothing.** One unwrap, already performed.

### The number in D2 that this revises

The `257.2 s` stage-3 figure above was measured on the shorter `.webm` (4396 s) and fitted
under a 300 s ceiling it did not have to clear. It is a measurement that passed a threshold,
not evidence that transcription scales to lecture length. The 4520 s run measured **315.4 s**,
and it is now measured without a proxy timeout in the path.

### What is still not established

**Transcription is not reproducible on this host.** Two identical requests disagree on
segment partitioning, while the text matched word for word over the 45 s clip. Consequences:

- The pipeline's own materials already record that this endpoint "is not reproducible run to
  run" (measured: 25 s clip, three runs, `10, 11, 11` segments). This amendment does not
  contradict that — it confirms it and states the size of the effect.
- Issue #32 asks for a paid endpoint evaluated on reproducibility, figures, terminology and
  cost. Reproducibility is now a **measured weakness of the incumbent**, not an untested
  attribute of the alternative. That changes what the comparison must establish.
- Whether the two removed/kept code paths differ over a full 4520 s lecture was **not**
  measured, only over 45 s. #44 required a full-length comparison with numeric-token
  specificity before deleting, and that was not run. The removal rests on the two grounds
  above, which stand without it — but the unmeasured question is recorded here rather than
  quietly dropped.

### What `doctor` now discloses

Two backends remain, with opposite trust properties: a local whisper.cpp keeps the recording
on the machine; an OpenAI-compatible endpoint sends both audio and transcripts off it. The
`stt` check prints which one is active and states which of the two is in force. Previously the
trust boundary was inferable only by reading this ADR.

## 2026-10-12 — D2 amended: the STT endpoint has no built-in default (#95)

The code shipped `DEFAULT_ENDPOINTS = {"whispercpp": "http://127.0.0.1:10302", "openai":
"http://127.0.0.1:10302"}` and passed it as the last `resolve()` layer. Both entries are the
**raw container port**: no auto-start, no wake-on-first-request, and no `_Heartbeat` to
refresh the marker `whisper-idle-stop.timer` reads. An operator who accepted the default got
exactly the coincidence #62 measured — a 300 s reclaim mid-request, survived only when the
request happened to route through shipboard's proxy on `100.64.0.1:10301`, which does refresh
the marker. The default was therefore not a default in any useful sense: it named a target
whose lifecycle this project does not control.

**The default is removed, not replaced.** An unset URL is now its own state — *not
configured* — distinct from *configured and unreachable*, which `TranscriptionBackend.check()`
already required. An unconfigured backend fails before opening a socket, so `auto` never
probes `127.0.0.1:10302`; the error names `DYAK_WHISPERCPP_URL` (or the `whispercpp_url`
settings key) and the supported proxy. `doctor`'s failure path carries the raw-port-vs-proxy
distinction and the 300 s reclaim.

`WHISPER_REQUEST_TIMEOUT` (client-side) and the timer itself are out of scope: the timer
lives in shipboard. This amendment is about what *dyak* defaults to and what *dyak* tells the
operator. Tracked as #95; the measured history is #62.
