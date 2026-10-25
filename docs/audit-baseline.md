# The audit baseline — the regression check from #20

This is the document #20 was missing: the note has been re-audited, and its finding set has
been compared against the 21-finding hand-written baseline. The comparison is a **procedure
with a recorded first result**, not a one-off sentence. The result is negative on recall,
and that is the point of writing it down.

## What was run

Both runs used `dyak audit` (the standalone entry point added by #132) against a fixed note
and a fixed transcript, so a re-run does not pay for STT and does not inherit whisper.cpp's
run-to-run variance (#49).

| | hand-written baseline | generated note |
|---|---|---|
| note | `_archive/old_Проектирование оптимальных систем управления/Лекция 1. 01.10.26.md` (428 lines) | `~/.local/state/glimpse/1_lecture_OCS/note.linked.md` (run 7) |
| transcript | `~/.local/state/glimpse/1_lecture_OCS/transcript.txt` | same |
| model | `opencode-zen/glm-5.3-flash` | same |
| `DYAK_LLM_CONCURRENCY` | `8` | `8` |
| `--tier2-limit` | `100` | `100` |
| `--section-chars` | `200000` | `200000` |
| prompt digest | `82d66f51eb574958ea98b2bb55b06e2610f8c94d2656af3621ae5f257a506ee2` | same |
| tree | `0de3960` plus the P0 branch (the standalone CLI; the stage-9 prompt is unchanged) | same |
| findings | **60** (53 tier-2 + 7 tier-1), 0 ERROR | **40** (34 tier-2 + 6 tier-1), 0 ERROR |
| sections reviewed | 9/9 | 8/8 |

The hand-written run's `audit.json` is frozen at
`~/.local/state/dyak/baselines/lecture1-handwritten-2026-10-25.audit.json`; the generated
note's at `~/.local/state/dyak/baselines/lecture1-generated-2026-10-25.audit.json`. They stay
outside the repository because an audit carries verbatim lecture text.

The configured model in `~/.config/dyak/settings.toml` is still `opencode-go/glm-5.3-flash`,
which the gateway now answers with `model_not_found`; the runs above override
`DYAK_LLM_MODEL`. `dyak doctor` probes the endpoint, not the model id, so it does not catch
this drift today.

## The comparison against the 21 human findings

The baseline is a hand-written audit, not a model's. "Found" means the machine audit names
the same defect; "Partial" means it flags the same passage for a different (usually
transcript-fidelity) reason; "Missed" means nothing in the machine finding set addresses it.

| # | baseline finding | machine result |
|---|---|---|
| 1.1 | sign of the gravitational term flips the equilibrium table | **Found** — high: the sign of `(2,1)` is tied to `sin θ` and contradicts the note's own equation |
| 1.2 | "positive definite `A` at the lower equilibrium" is false for both matrices | **Found** — medium: no matrix in the note is positive definite |
| 1.3 | `xᵀAx + uᵀBu` called a vector function | **Missed** |
| 1.4 | `δx` · `B` dimension claim | **Missed** — tier 1's dimension check did not fire |
| 1.5 | `Q ⪰ 0` ⇒ unique global minimum (⪰/≻ substitution) | **Partial** — low: the engineering note is absent from the transcript; the ⪰/≻ error is not named |
| 1.6 | "positive definite matrix is a stability sign", citing §7.3 | **Partial** — high: the stability/saddle classification is not in the transcript; the sign confusion itself is not named |
| 1.7 | tensor with three indices collapsed by one sum | **Missed** |
| 1.8 | "DP was useless in the 1930s" | **Missed** |
| 2.1 | scalar Taylor formula applied to a vector field | **Missed** |
| 2.2 | autodiff "exact … but error accumulates per layer" | **Found** — high: the transcript attributes accumulation to numerical differentiation |
| 2.3 | missing terminal term and control constraint in the problem statement | **Missed** |
| 2.4 | positive definiteness defined without symmetry | **Missed** |
| 2.5 | "choice of plant changes `B`" / rank argument | **Partial** — medium: the input choice is not in the transcript; the rank error is not named |
| 2.6 | `O(N^k)` described as a cost in the number of discretisation steps | **Partial** — high: the formulas are absent from the transcript; the mechanism error is not named |
| 2.7 | LQR and the "three sources of constraints" | **Partial** — medium: the passage is flagged against the transcript; "boundary conditions exist in LQR too" is not the finding |
| 2.8 | "linearisation point" vs "linearisation region" vs "working point" | **Missed** |
| 3.1 | "Kapitsa pendulum" used for an ordinary pendulum model | **Missed** |
| 3.2 | MPC placed in the 1990s under "what appeared" | **Partial** — medium: "state of the art" is attributed to iLQR/DDP, not MPC; the date is not addressed |
| 3.3 | Jacobian table only in the square `n → n` form | **Found** — high: the general `n → m` Jacobian is not square |
| 3.4 | "the only family that gives feedback out of the box" (DP) | **Missed** — the baseline itself records this as uncertain |
| 3.5 | minor notation issues (`5I`, `x ∈ ℝ` vs `x ∈ ℝⁿ`) | **Missed** |

**Counts: 4 found, 6 partial, 11 missed.** Of the 8 high/very-high-confidence baseline
findings: 3 found, 2 partial, 3 missed.

On the generated note the machine audit found 40 warnings and no errors. The generated note
still contains the passages the baseline flagged at 1.2, 1.3, 2.1, 2.2, 2.6 and 3.1; the
audit returned a finding on 1.2 and 2.2 only. That is the same recall gap, observed on the
note the pipeline actually produces.

## What this means

The tier-2 critic is a **grounding** critic. Its prompt asks whether a statement follows from
the transcript, and its example is a grounding example; it is not a mathematical proofreader.
Tier 1 covers rank/dimension on a declared subset, unsourced numbers and empty math. The
human baseline covers a different and larger class: sign errors, an incorrect definition, a
dropped terminal term, a rank argument, a historical date.

So:

- **The regression check exists and is repeatable.** Fixed note, fixed transcript, recorded
  model, prompt digest and parameters; `tools/audit_baseline.py` diffs two `audit.json`
  files and treats a disappeared finding as loudly as a new one.
- **The first result is not a pass.** "Independently audited" currently means tier 1 plus
  transcript grounding. It does not mean the human baseline's math review has been
  reproduced. #20 is closed on "the comparison was made", not on "recall was achieved".
- **The next measurement is a planted-error benchmark**, not another run on this lecture:
  known errors with measured recall and precision across candidate models. That is the open
  question #11 already names, and this comparison is the reason it should be scheduled
  before the audit's trust claim is widened.

## Re-running the check

```bash
# 1. Audit the fixed note and transcript again.
DYAK_LLM_MODEL=opencode-zen/glm-5.3-flash \
DYAK_LLM_CONCURRENCY=8 \
dyak audit "$NOTE" --transcript "$TRANSCRIPT" \
    --output-dir /tmp/audit-now \
    --tier2-limit 100 --section-chars 200000

# 2. Diff the finding sets.
tools/audit_baseline.py \
    ~/.local/state/dyak/baselines/lecture1-handwritten-2026-10-25.audit.json \
    /tmp/audit-now/audit.json
```

Add `--strict` only when the model, prompt digest and parameters are the same as the
baseline's. A model audit is not byte-deterministic; a difference is a question, not
automatically a regression.
