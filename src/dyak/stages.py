"""How many stages exist, how many are built, and the one continuation-line renderer.

Its own module because three others need it. `pipeline` owns the list of stage calls,
`cli` owns the exit codes, and each stage module writes its own progress line -- and a
stage module that imports `pipeline` to ask "how many stages are there?" is a circular
import, since `pipeline` imports every stage module. The same reason puts
`progress_line`, the renderer those modules share, here rather than in `pipeline`.

Keeping this in one place is what stops the progress output from claiming stages that do
not exist. `[6/12] caption` on a six-stage pipeline is a claim about the software, not a
formatting preference.
"""

from __future__ import annotations

#: Stage 0 is `doctor`, run by the CLI. The twelve-stage list is ADR-0001 § *The pipeline*
#: -- it is not any numbered decision; `D3` is about reading frames from the source.
IMPLEMENTED = 12

#: Stages ADR-0001 § *The pipeline* assigns to the pipeline. The count in every progress
#: line is IMPLEMENTED, not the 12 that section describes: a line reading "[6/12] caption"
#: next to "[4/6] frames" tells the reader two different things about how much of this
#: software exists.
FIRST_STAGE = 1

REMAINING_NOTE = "every stage in ADR-0001 § The pipeline is built"


def progress_line(stream, unit: str, done: int, total: int) -> None:
    """One continuation line: `unit done/total`, written before that unit's blocking call.

    The one renderer for every stage with a unit it can observe (#197): stage 3's hosted chunk
    counter and the per-unit loops of stages 6, 7 and 9. One shape, so it cannot drift between
    them, and one place where the flush-and-swallow policy is pinned.

    Indented eleven spaces and deliberately without a stage prefix: the stage's own start line
    already carries `[n/12]`, and a second stage-prefixed literal in the package would break
    `tests/test_claims.py`'s count of them.

    `done` is the units completed so far -- the loop's own 0-based index -- so a loop's first
    line is `unit 0/N`: the stage has begun and N units are ahead. That is the honest minimum,
    because nothing in this tree can say how long the units will take: `llm.chat` sends
    `stream: false` (`llm.py:253`), retries internally and returns a `Reply` after the call,
    the STT backends report nothing mid-call, and the only measured rate in the package is
    whisper.cpp's (`stt/core.py:46-52`). An interval for the vision or text endpoint would be
    invented, which is exactly what #186 removed from stage 3's line (#210 owns re-measuring).

    Written *before* the call rather than after it: a completed count needs a write on every
    exit path -- a skipped, cached, capped or failed unit leaves its loop by `continue`, and a
    missed path freezes the counter silently. The cost is stated: the last line of a loop is
    `(N-1)/N`, and the stage's own result line carries completion. (Stage 3 still reaches
    `chunks N/N` because its backend fires the callback after the final chunk.)

    `OSError`/`ValueError` are swallowed: a progress line must not move a run's outcome
    (`deps.py:280`'s rule for warnings, applied to the stream), and `stream` may be a closed
    file or one that went away mid-run.
    """
    try:
        stream.write(f"           {unit} {done}/{total}\n")
        stream.flush()
    except (OSError, ValueError):
        return
