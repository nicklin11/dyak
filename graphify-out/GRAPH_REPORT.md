# Graph Report - .  (2026-10-11)

## Corpus Check
- 52 files · ~101,039 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 985 nodes · 1628 edges · 77 communities (47 shown, 30 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 70 edges (avg confidence: 0.67)
- Token cost: 41,000 input · 4,200 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Bounding-box estimation|Bounding-box estimation]]
- [[_COMMUNITY_Caption frames & alignment|Caption frames & alignment]]
- [[_COMMUNITY_Bundle artefacts (audittracelink)|Bundle artefacts (audit/trace/link)]]
- [[_COMMUNITY_Stage 9 audit checks|Stage 9 audit checks]]
- [[_COMMUNITY_Stage 8 lint rules|Stage 8 lint rules]]
- [[_COMMUNITY_LLM client & config|LLM client & config]]
- [[_COMMUNITY_Stage 7 synthesis & assembly|Stage 7 synthesis & assembly]]
- [[_COMMUNITY_Design rationales (interface, exits)|Design rationales (interface, exits)]]
- [[_COMMUNITY_Stage 11 wikilink builder|Stage 11 wikilink builder]]
- [[_COMMUNITY_Packaging & code lint tooling|Packaging & code lint tooling]]
- [[_COMMUNITY_Stage 2 audio extraction|Stage 2 audio extraction]]
- [[_COMMUNITY_Stage fixture tests|Stage fixture tests]]
- [[_COMMUNITY_Stage 0 doctor dependency checks|Stage 0 doctor dependency checks]]
- [[_COMMUNITY_ADR decision records|ADR decision records]]
- [[_COMMUNITY_Stage 12 report builder|Stage 12 report builder]]
- [[_COMMUNITY_CLI entry & parsing|CLI entry & parsing]]
- [[_COMMUNITY_Bundle paths & vault export|Bundle paths & vault export]]
- [[_COMMUNITY_Stage 4 frame detection|Stage 4 frame detection]]
- [[_COMMUNITY_Package roots & exit codes|Package roots & exit codes]]
- [[_COMMUNITY_whisper.cpp backend|whisper.cpp backend]]
- [[_COMMUNITY_Pipeline orchestration|Pipeline orchestration]]
- [[_COMMUNITY_Managed work directory|Managed work directory]]
- [[_COMMUNITY_DYAK_ configuration surface|DYAK_* configuration surface]]
- [[_COMMUNITY_Stage 10 mechanical repair|Stage 10 mechanical repair]]
- [[_COMMUNITY_External dependency runner|External dependency runner]]
- [[_COMMUNITY_OpenAI-compatible STT backend|OpenAI-compatible STT backend]]
- [[_COMMUNITY_Stage 3 STT core|Stage 3 STT core]]
- [[_COMMUNITY_Output & vault path config|Output & vault path config]]
- [[_COMMUNITY_Provenance & decision docs|Provenance & decision docs]]
- [[_COMMUNITY_Sharpness calibration tools|Sharpness calibration tools]]
- [[_COMMUNITY_Transcription backend protocol|Transcription backend protocol]]
- [[_COMMUNITY_Frame gate artefacts|Frame gate artefacts]]
- [[_COMMUNITY_HTTP test fakes|HTTP test fakes]]
- [[_COMMUNITY_JPEG probing tool|JPEG probing tool]]
- [[_COMMUNITY_Finite-float parsing helpers|Finite-float parsing helpers]]
- [[_COMMUNITY_urlopen test fakes|urlopen test fakes]]
- [[_COMMUNITY_Stage provenance artefacts|Stage provenance artefacts]]
- [[_COMMUNITY_STT backend registry|STT backend registry]]
- [[_COMMUNITY_Stage 7 promotion fakes|Stage 7 promotion fakes]]
- [[_COMMUNITY_Stage 12 coverage claims|Stage 12 coverage claims]]
- [[_COMMUNITY_Settings precedence (ADR-0002)|Settings precedence (ADR-0002)]]
- [[_COMMUNITY_Env isolation test helpers|Env isolation test helpers]]
- [[_COMMUNITY_stderr test fakes|stderr test fakes]]
- [[_COMMUNITY_Audit & math-verification philosophy|Audit & math-verification philosophy]]
- [[_COMMUNITY_Lint gate documentation|Lint gate documentation]]
- [[_COMMUNITY_Live log  stdout buffering issue|Live log / stdout buffering issue]]
- [[_COMMUNITY_Audit critic stub|Audit critic stub]]
- [[_COMMUNITY_Backend check fakes|Backend check fakes]]
- [[_COMMUNITY_Synthesizer test stub|Synthesizer test stub]]
- [[_COMMUNITY_ffmpeg dependency checks|ffmpeg dependency checks]]
- [[_COMMUNITY_Named-backend fakes|Named-backend fakes]]
- [[_COMMUNITY_Quality runner tool|Quality runner tool]]
- [[_COMMUNITY_Distribution decision (zipapps)|Distribution decision (zipapps)]]
- [[_COMMUNITY_Fake ffmpeg fixture|Fake ffmpeg fixture]]
- [[_COMMUNITY_Health-check stubs|Health-check stubs]]
- [[_COMMUNITY_HTTP queue fixture|HTTP queue fixture]]
- [[_COMMUNITY_Isolated run test helper|Isolated run test helper]]
- [[_COMMUNITY_Note fixture|Note fixture]]
- [[_COMMUNITY_Raised-error assert helper|Raised-error assert helper]]
- [[_COMMUNITY_Section heading helper|Section heading helper]]
- [[_COMMUNITY_Lint rules test helper|Lint rules test helper]]
- [[_COMMUNITY_WAV fixture builder|WAV fixture builder]]
- [[_COMMUNITY_Wire transcript recorder|Wire transcript recorder]]
- [[_COMMUNITY_ADR-0003 D2 events|ADR-0003 D2 events]]
- [[_COMMUNITY_ADR-0003 D3 stream|ADR-0003 D3 stream]]
- [[_COMMUNITY_D numbering convention|D numbering convention]]
- [[_COMMUNITY_detect.json concept|detect.json concept]]
- [[_COMMUNITY_source-provenance.json concept|source-provenance.json concept]]
- [[_COMMUNITY_Stage 10 repair concept|Stage 10 repair concept]]
- [[_COMMUNITY_Stage 11 link concept|Stage 11 link concept]]
- [[_COMMUNITY_Stage 1 probe concept|Stage 1 probe concept]]
- [[_COMMUNITY_Stage 2 audio concept|Stage 2 audio concept]]
- [[_COMMUNITY_Stage 3 stt concept|Stage 3 stt concept]]
- [[_COMMUNITY_Stage 8 lint concept|Stage 8 lint concept]]
- [[_COMMUNITY_Stage 9 audit concept|Stage 9 audit concept]]
- [[_COMMUNITY_dyak package|dyak package]]

## God Nodes (most connected - your core abstractions)
1. `DYAK_* environment variable table (full configuration reference)` - 22 edges
2. `Report` - 19 edges
3. `WorkDir` - 19 edges
4. `dyak (lecture recording -> audited Obsidian note)` - 19 edges
5. `Bundle` - 17 edges
6. `BackendInfo` - 17 edges
7. `Check` - 16 edges
8. `run()` - 15 edges
9. `align()` - 13 edges
10. `run()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `pip install -e . runs the installed package (pipx incident)` --semantically_similar_to--> `stale editable install -> ModuleNotFoundError; re-install with pipx install --force -e`  [INFERRED] [semantically similar]
  .github/workflows/ci.yml → README.md
- `dyak (lecture recording -> audited Obsidian note)` --references--> `dyak doctor subcommand`  [INFERRED]
  README.md → docs/running.md
- `dyak (lecture recording -> audited Obsidian note)` --references--> `ffmpeg / ffprobe`  [EXTRACTED]
  README.md → .github/workflows/ci.yml
- `dyak (lecture recording -> audited Obsidian note)` --cites--> `DYAK_* environment variable table (full configuration reference)`  [INFERRED]
  README.md → docs/running.md
- `dyak (lecture recording -> audited Obsidian note)` --references--> `dyak process subcommand`  [INFERRED]
  README.md → docs/running.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **the 12-stage pipeline forming the audited bundle** — docs_artefacts_stage1_probe, docs_artefacts_stage2_audio, docs_artefacts_stage3_stt, docs_artefacts_stage4_frames, docs_artefacts_stage5_quality, docs_artefacts_stage6_caption, docs_artefacts_stage7_synth, docs_artefacts_stage8_lint, docs_artefacts_stage9_audit, docs_artefacts_stage10_repair, docs_artefacts_stage11_link, docs_artefacts_stage12_report, docs_artefacts_bundle [INFERRED 0.75]
- **one provenance record per stage in the bundle** — docs_artefacts_provenance, docs_artefacts_detect_json, docs_artefacts_caption_trace, docs_artefacts_llm_transcripts, docs_artefacts_bundle [INFERRED 0.85]
- **three-layer configuration resolution (flag, env, settings file)** — docs_running_config_precedence, docs_running_env_var_table, docs_running_settings_file, docs_running_dyak_settings [INFERRED 0.85]
- **Twelve-stage dyak pipeline** — docs_stages_stage_0_doctor, docs_stages_stage_1_probe, docs_stages_stage_2_audio, docs_stages_stage_3_stt, docs_stages_stage_4_frames, docs_stages_stage_5_quality, docs_stages_stage_6_caption, docs_stages_stage_7_synth, docs_stages_stage_8_lint, docs_stages_stage_9_audit, docs_stages_stage_10_repair, docs_stages_stage_11_link, docs_stages_stage_12_report [EXTRACTED 1.00]
- **Per-stage provenance artefact scheme** — docs_stages_source_provenance_json, docs_stages_detect_json, docs_stages_caption_trace, docs_stages_caption_status, docs_stages_report_json, docs_stages_stage_12_report [EXTRACTED 1.00]
- **Vision endpoint surface: config, provenance, classification** — docs_stages_stage_6_caption, adr_0004_endpoints_d3, adr_0004_endpoints_d4, adr_0006_frame_classifier_d1, adr_0006_frame_classifier_d2 [INFERRED 0.85]
- **Frame assessment flow: detect -> gate -> caption budget** — adr_0001_two_pass_frame_detection, adr_0001_bbox_estimator_protocol, adr_0001_frame_quality_gate, adr_0001_mege, adr_0005_caption_budget_is_stage4_output [INFERRED 0.85]
- **Silent-failure countermeasures across the pipeline** — adr_0001_error_propagation_contract, adr_0001_verify_artefacts_on_disk, adr_0002_backend_never_bundled, adr_0002_doctor [INFERRED 0.75]
- **Defences against unverifiable LLM synthesis** — adr_0001_kapitza_error_evidence, adr_0001_independent_audit_context, adr_0001_layered_math_verification [INFERRED 0.85]

## Communities (77 total, 30 thin omitted)

### Community 0 - "Bounding-box estimation"
Cohesion: 0.08
Nodes (45): Box, _analysis_raster(), BBoxEstimator, Box, _dimensions(), _enhance(), enhanced_name(), evaluate() (+37 more)

### Community 1 - "Caption frames & alignment"
Cohesion: 0.08
Nodes (34): align(), Alignment, caption_frames(), CaptionOutcome, _first_word_ms(), load_cache(), overlapping(), provenance() (+26 more)

### Community 2 - "Bundle artefacts (audit/trace/link)"
Cohesion: 0.05
Nodes (42): audit.json (findings, each with its quoted line), caption_trace (model, tokens, timing, called or cached), captions.json (per-frame captions), link.json (wikilinks inserted), synth-llm-transcript.json / audit-llm-transcript.json (every call, full messages), note.linked.md (the note after linking), note.md (the note as synthesised), note.repaired.md (the note after repair) (+34 more)

### Community 3 - "Stage 9 audit checks"
Cohesion: 0.08
Nodes (36): audit(), check_dimensions(), check_empty_math(), check_term_drift(), check_unsourced_numbers(), _clean_symbol(), _declared_shapes(), Finding (+28 more)

### Community 4 - "Stage 8 lint rules"
Cohesion: 0.12
Nodes (30): check_assets(), check_braces(), check_delimiters(), check_duplicate_heading(), check_empty_section_claims(), check_filler(), check_links_in_math(), check_mojibake() (+22 more)

### Community 5 - "LLM client & config"
Cohesion: 0.08
Nodes (27): _body(), chat(), Config, EndpointError, frame_fingerprint(), image_data_uri(), NotConfiguredError, An OpenAI-compatible chat client, with nothing else in it.  No SDK. `urllib` and (+19 more)

### Community 6 - "Stage 7 synthesis & assembly"
Cohesion: 0.10
Nodes (28): assemble(), build_sections(), caption_text(), choose(), LLMSynthesizer, normalise(), Note, provenance() (+20 more)

### Community 7 - "Design rationales (interface, exits)"
Cohesion: 0.06
Nodes (35): BBoxEstimator Protocol (Geometric/Null/VLM estimators), Caption cache keyed by (frame fingerprint, model id), The name carries no meaning; the interface does (D1), Verbatim error propagation from dependencies (never swallow diagnostics), Exit code contract: 0 success, 1 usage, 2 missing dep, 3 dep failed, 4 gate, 5 flagged, 130 interrupted, Frame quality gate: fix is crop, not a better model (D4), ADR 0001: One-command lecture pipeline (video -> audited note), MEGE: mean gradient-square energy over noise floor (focus metric) (+27 more)

### Community 8 - "Stage 11 wikilink builder"
Cohesion: 0.11
Nodes (30): _as_list(), build_index(), check_report(), collect_targets(), _excluded(), index_report(), link_paths(), link_text() (+22 more)

### Community 9 - "Packaging & code lint tooling"
Cohesion: 0.07
Nodes (21): pyproject.toml (requires-python >=3.11), pipx install (installation path), stale editable install -> ModuleNotFoundError; re-install with pipx install --force -e, ruff (linter/formatter), imports_of(), AST, `a.b.c` -> `a`. Relative imports have no root and return ''., Every module named by an import statement, as (top-level name, line).      Relat (+13 more)

### Community 10 - "Stage 2 audio extraction"
Cohesion: 0.11
Nodes (26): DependencyError, AudioArtefact, check_coverage(), extract(), Stage 2: extract a 16 kHz mono wav from the source recording.  16 kHz mono PCM i, Warn when the extracted audio is much shorter than the source.      Returns the, Extract 16 kHz mono PCM wav from `source` into `dest`. Verified on disk., Assert the wav exists, carries audio, and is in the agreed format. (+18 more)

### Community 11 - "Stage fixture tests"
Cohesion: 0.08
Nodes (10): _caption_reply(), dirty_pipeline(), fake_pipeline(), fake_resolve(), fake_run(), _fake_vision_chat(), observe(), _refusal_chat() (+2 more)

### Community 12 - "Stage 0 doctor dependency checks"
Cohesion: 0.12
Nodes (28): CompletedProcess, Check, check_executable(), check_ffmpeg(), check_ffprobe(), check_llm(), check_numpy(), check_stt() (+20 more)

### Community 13 - "ADR decision records"
Cohesion: 0.07
Nodes (29): ADR-0003 D1 — pipeline emits events, ADR-0003 D4 — blocking work is bracketed, ADR-0004 D1 — one config file, env overrides, ADR-0004 D2 — dyak config set|get|list, ADR-0004 D3 — separate vision and text sections, ADR-0004 D4 — provenance in the artefact, ADR-0004 D5 — doctor reports endpoints, never the key, ADR-0006 D1 — caption call returns frame_class (+21 more)

### Community 14 - "Stage 12 report builder"
Cohesion: 0.14
Nodes (20): _check(), count_frames(), expected_frames(), inventory(), promote_note(), provenance(), Stage 12: report. Assemble the deliverable set and verify it on disk.  This stag, Nothing required is missing, and the frame count agrees with the manifest. (+12 more)

### Community 15 - "CLI entry & parsing"
Cohesion: 0.14
Nodes (23): ArgumentParser, _audit(), build_parser(), _doctor(), _line_buffer(), main(), _pending(), _preflight() (+15 more)

### Community 16 - "Bundle paths & vault export"
Cohesion: 0.12
Nodes (14): Bundle, The output bundle: what a run leaves behind.  Separate from `workspace.py` becau, One run's output directory., Resolve the bundle root and create it.          Precedence: `--output-dir` > `DY, Empty `images/` before this run's frames land in it. Returns files removed., Remove the previous run's artefacts from the root. Returns what was removed., Move a work-dir artefact into the bundle and remember it.          `shutil.move`, Size of what is registered, which is not the same as what is on disk.          # (+6 more)

### Community 17 - "Stage 4 frame detection"
Cohesion: 0.18
Nodes (23): detect(), detect_filter(), extract_at(), ffmpeg_version(), hhmmss(), provenance(), Stage 4 — frames: two-pass extraction of distinct on-screen states (ADR-0001 D3), `runner.run`, with a retry for the host's intermittent input-open failure. (+15 more)

### Community 18 - "Package roots & exit codes"
Cohesion: 0.17
Nodes (13): Exit codes are a contract, not documentation (ADR-0001).  Every stage imports it, dyak — one command turns a lecture recording into an audited note.  See docs/adr, Path, _digest(), format_timestamp(), provenance(), Stage 3 core: the normalised transcript types, the parser, and the writers.  ADR, Write the three transcript artefacts into `workdir`. (+5 more)

### Community 19 - "whisper.cpp backend"
Cohesion: 0.15
Nodes (13): BackendInfo, Exception, Path, BackendInfo, What `doctor` prints and what the stage report records., HttpWhisperCpp, HTTP backend for whisper.cpp's native `POST /inference`.  This is the protocol t, The request fields, exactly as they will be sent.          Factored out of `_mul (+5 more)

### Community 20 - "Pipeline orchestration"
Cohesion: 0.18
Nodes (16): Bundle, _estimate(), _interval(), publishes_stage7_transcript(), Stage orchestration for `dyak process`.  Stages 1-4 live here. Stage 0 (`doctor`, Per-stage seconds into the bundle. Best effort: never fail a run over a log., One unit for the whole interval.      Choosing per endpoint produced "15s-2min",, Wall-clock estimate for stage 3, in seconds.      Short inputs are not faster pe (+8 more)

### Community 21 - "Managed work directory"
Cohesion: 0.14
Nodes (12): Check, _parent_from_env(), The managed work directory for one `dyak process` run (ADR-0001 stage 2).  Stage, `DYAK_WORKDIR` sets the parent of the managed dir, not the dir itself.      A pe, A per-run scratch directory with explicit release or retain semantics., Success path: remove the directory unless it is retained., Keep the directory. Returns whether it is now retained., One line describing where the artefacts are, or that the scratch is gone. (+4 more)

### Community 22 - "DYAK_* configuration surface"
Cohesion: 0.19
Nodes (16): DYAK_GATEWAY_URL is not read anywhere (its check was removed), DYAK_LLM_ENDPOINT (synthesis stage 7 and audit stage 9), DYAK_LLM_KEY (optional), DYAK_LLM_MODEL, DYAK_OPENAI_KEY, DYAK_OPENAI_MODEL (default whisper-1), DYAK_OPENAI_URL (hosted STT endpoint), DYAK_STT (backend selection: auto/whispercpp/openai) (+8 more)

### Community 23 - "Stage 10 mechanical repair"
Cohesion: 0.20
Nodes (11): _balance_inline(), Change, provenance(), Stage 10: repair what can be repaired mechanically, and say what was not.  ADR-0, Close an odd trailing `$`, or escape a stray one on an otherwise math-free line., Return the repaired markdown and what was done to it., repair(), Report (+3 more)

### Community 24 - "External dependency runner"
Cohesion: 0.17
Nodes (14): _as_bytes(), DependencyError, emit_verbatim(), Running an external dependency, and the exit-code contract around it.  `deps.py`, Locate `name` on PATH or raise MISSING_DEPENDENCY., Run `name args...`, return stdout as bytes, raise DependencyError otherwise., A dependency could not be run, or ran and failed. Carries everything.      `stde, Write a dependency's stderr to our stderr, unmodified. True if it wrote.      No (+6 more)

### Community 25 - "OpenAI-compatible STT backend"
Cohesion: 0.18
Nodes (8): BackendInfo, Exception, Path, OpenAICompat, Fail early on a non-JSON body; the envelope itself is handled by core.parse., What this backend will send, for `stt-provenance.json`.          The model id is, Transcribes over `POST /v1/audio/transcriptions`., _reason()

### Community 26 - "Stage 3 STT core"
Cohesion: 0.17
Nodes (12): Path, Transcript, Pick a backend. `DYAK_STT` wins; otherwise whisper.cpp native, then OpenAI., resolve(), One BPE piece with its timing. `text` is verbatim from whisper.cpp., Segment, Word, backend() (+4 more)

### Community 27 - "Output & vault path config"
Cohesion: 0.15
Nodes (14): the scratch work dir (audio.wav, removed on success, --keep-workdir keeps it), DYAK_OUTPUT_DIR (bundle destination), DYAK_TERMS_DIR (atomic term notes, default <vault>/mscs/_terms), DYAK_VAULT (the Obsidian vault), DYAK_WORKDIR (parent of the managed scratch work dir), --keep-workdir, --no-vault-export, --output-dir (+6 more)

### Community 28 - "Provenance & decision docs"
Cohesion: 0.29
Nodes (11): docs/adr/README.md (decision record index), the output bundle ($XDG_STATE_HOME/dyak/<lecture>/, flat root), timings.json (wall clock per stage, written by pipeline), every gate writes its decision to the bundle (rejection is a document, not just an exit code), docs/ index, issue #85 - zero-shot classifier plan, the single-copy rule (a claim lives in exactly one place), --vault-images (off by default; frames stay in the bundle as evidence) (+3 more)

### Community 29 - "Sharpness calibration tools"
Cohesion: 0.29
Nodes (10): ndarray, blurred_png(), main(), native_gray(), Path, Calibrate the stage-5 sharpness threshold by blur injection.  The threshold in A, One frame, blurred by `sigma`, re-encoded as PNG.      PNG because a lossy re-en, MEGE and the edge population, at the raster's own native resolution. (+2 more)

### Community 30 - "Transcription backend protocol"
Cohesion: 0.18
Nodes (7): Protocol, Transcribe one 16 kHz mono wav. Return the raw payload; do not parse it., Where this backend points, for `doctor` and the stage report., Raise runner.DependencyError unless this backend is usable *now*.          Must, Transcribe and return the raw payload., What this backend will send, for the stage-3 provenance document.          Requi, TranscriptionBackend

### Community 31 - "Frame gate artefacts"
Cohesion: 0.20
Nodes (10): detect.json (ffmpeg filter chain and settings), manifest.tsv (frame index: timestamp, path, reason), quality.json (per-frame sharpness and gate decision), Stage 4 - frames, Stage 5 - quality, GATED_OUT (failed frame never leaves the machine), GeometricEstimator (the only registered bbox source), stage 5 sharpness gate (threshold 37320; 57/58 pass on run 13) (+2 more)

### Community 32 - "HTTP test fakes"
Cohesion: 0.22
Nodes (3): FakeHTTP, Stands in for urllib.request.urlopen. Records what was sent, returns what is que, _Response

### Community 33 - "JPEG probing tool"
Cohesion: 0.33
Nodes (7): extract(), main(), measure(), Path, Measure three claims before accepting them. Not part of the package; run from th, Classic Tenengrad and the edge population behind it, in one pass.      Returns (, sobel_stats()

### Community 34 - "Finite-float parsing helpers"
Cohesion: 0.39
Nodes (8): _finite(), _number(), _optional_float(), parse(), First `limit` bytes, marked when anything was cut.      An unmarked truncation r, Coerce to a finite float, or None.      Both NaN and +/-inf must be rejected, no, _truncated(), _words()

### Community 35 - "urlopen test fakes"
Cohesion: 0.25
Nodes (3): fake_urlopen(), _FakeHTTPResponse, _netloc()

### Community 36 - "Stage provenance artefacts"
Cohesion: 0.29
Nodes (7): per-stage *-provenance.json (what produced each output), Stage 1 - probe, Stage 2 - audio, Stage 3 - stt, transcript.raw.json / transcript.json / transcript.txt (stage 3), one line per stage progress output (gap between lines is the stage's real cost), the [3/12] stt starting pre-line is not a duplicate

### Community 37 - "STT backend registry"
Cohesion: 0.33
Nodes (3): _env_endpoint(), Stage 3 backends: one interface, several wire formats.  `TranscriptionBackend` r, Backend for OpenAI-compatible `/v1/audio/transcriptions` endpoints.  For hosted

### Community 38 - "Stage 7 promotion fakes"
Cohesion: 0.33
Nodes (6): fake_frames_run(), Path, Run the real promotion helper over a stage-7 work dir and report what it publish, spy_quality_run(), _stage7_promotes(), truncate_copy()

### Community 39 - "Stage 12 coverage claims"
Cohesion: 0.40
Nodes (5): coverage claim: verified union unchecked = files_in_bundle_excluding_this_stage (since 5c66731), issue #87 - files_checked gap (65 of 144 on run 13), report.json verified fields (ok, verified, absent, missing, files_checked, ...), report.json (what stage 12 verified and did not find), Stage 12 - report

### Community 40 - "Settings precedence (ADR-0002)"
Cohesion: 0.40
Nodes (5): ADR-0002 (referenced for precedence / dependency truth D2), configuration precedence: CLI flag -> DYAK_* env -> settings file, DYAK_SETTINGS (settings file path override), issue #52 - real per-layer configuration precedence, settings.toml ($DYAK_SETTINGS, else $XDG_CONFIG_HOME/dyak/settings.toml)

### Community 41 - "Env isolation test helpers"
Cohesion: 0.40
Nodes (4): isolate(), Remove every `DYAK_*` variable from `os.environ`; return them to restore later., Undo `isolate()`, including anything a test added., restore()

### Community 43 - "Audit & math-verification philosophy"
Cohesion: 0.50
Nodes (4): Audit as a separate model context that never rewrites (D5), Output verified by something other than its author, Kapitza pendulum sign error: the measured evidence justifying the pipeline, Layered math verification, mechanical layers first (D6)

### Community 44 - "Lint gate documentation"
Cohesion: 0.67
Nodes (4): lint.json (mechanical gate findings before audit), Stage 8 - lint, issue #93 - lint gate has never produced a finding on a real run, stage 8 lint gate (nine mechanical checks)

### Community 45 - "Live log / stdout buffering issue"
Cohesion: 0.67
Nodes (4): CPython block-buffers stdout in 8192-byte chunks when fd 1 is not a tty, issue #100 - the 16-minute silent log stall (run 14), run.log is live (tail -f tracks the run), streams reconfigured line-buffered at the entrypoint

### Community 49 - "ffmpeg dependency checks"
Cohesion: 0.67
Nodes (3): ffmpeg / ffprobe, deps.check_executable probes the real PATH, Install ffmpeg (fixture synthesis only)

## Ambiguous Edges - Review These
- `DYAK settings.toml (XDG config)` → `ADR-0004 D1 — one config file, env overrides`  [AMBIGUOUS]
  docs/stages.md · relation: semantically_similar_to

## Knowledge Gaps
- **68 isolated node(s):** `dyak`, `MediaInfo`, `Path`, `CompletedProcess`, `HTTPError` (+63 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **30 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `DYAK settings.toml (XDG config)` and `ADR-0004 D1 — one config file, env overrides`?**
  _Edge tagged AMBIGUOUS (relation: semantically_similar_to) - confidence is low._
- **Why does `Install ffmpeg (fixture synthesis only)` connect `ffmpeg dependency checks` to `Packaging & code lint tooling`, `Stage fixture tests`?**
  _High betweenness centrality (0.181) - this node is a cross-community bridge._
- **Why does `dyak (lecture recording -> audited Obsidian note)` connect `Provenance & decision docs` to `Bundle artefacts (audit/trace/link)`, `Packaging & code lint tooling`, `ffmpeg dependency checks`, `DYAK_* configuration surface`, `Output & vault path config`, `Frame gate artefacts`?**
  _High betweenness centrality (0.155) - this node is a cross-community bridge._
- **Why does `ffmpeg / ffprobe` connect `ffmpeg dependency checks` to `Provenance & decision docs`?**
  _High betweenness centrality (0.150) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `DYAK_* environment variable table (full configuration reference)` (e.g. with `docs/ index` and `DYAK_GATEWAY_URL is not read anywhere (its check was removed)`) actually correct?**
  _`DYAK_* environment variable table (full configuration reference)` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `WorkDir` (e.g. with `ArgumentParser` and `Bundle`) actually correct?**
  _`WorkDir` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `dyak (lecture recording -> audited Obsidian note)` (e.g. with `every gate writes its decision to the bundle (rejection is a document, not just an exit code)` and `dyak doctor subcommand`) actually correct?**
  _`dyak (lecture recording -> audited Obsidian note)` has 5 INFERRED edges - model-reasoned connections that need verification._