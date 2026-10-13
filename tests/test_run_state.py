#!/usr/bin/env python3
"""#271: a failed run records the reason, not only the exception's class name.

`run-state.json` is the one durable record a failed run leaves: the bundle travels to the vault
and to whoever reads the bug report, while the terminal scrollback is gone and the work dir lives
under `$TMPDIR`. Before this, the record carried `detail` -- the exception's *class* -- and
nothing else (`pipeline.py:529-537`), so two `PermissionError`s, a glossary and a source file,
produced byte-identical records with only `stage_reached` to separate them.

The decision (`#271`, decision comment 6145094845) is one additive field: `error` carries
`str(exc) or None` verbatim beside a `detail` that stays the category, and *no traceback*, because
a real traceback for this failure is 784 bytes and four absolute paths of the machine that ran it
(measured in the PR that lands this) -- home layout and temp paths in a file that is copied into a
vault and may be shared.

These checks are the decision's acceptance, each one red on the pre-fix tree:

    new keyword    `write_run_state(..., error=...)` -- TypeError there
    run level      the reason is absent from the bundle a failing run leaves
    interrupt      the key is absent (KeyError); with the fix it is present and null, because
                   `str(KeyboardInterrupt())` is `''` and `''` reads as a missing field

The `KeyboardInterrupt` pin in `tests/test_stages.py` (the `#92` checks, `detail ==
"KeyboardInterrupt"`) is deliberately not edited by this work: the category is the contract there,
and nothing here changes `detail`.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()

from dyak import bundle as dyb  # noqa: E402
from dyak import pipeline as dyp  # noqa: E402
from dyak import workspace as dyw  # noqa: E402

failures: list[str] = []

# The tree the suite is running from. A recorded reason must name the failing *user* path and
# nothing of this checkout: that is the traceback question, checked rather than asserted.
CHECKOUT = str(Path(dyp.__file__).resolve().parent)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


class _Stub:
    """Stands in for a stage module, carrying only the names `pipeline.run` reads."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def _raises(exc: BaseException):
    def _f(*_a, **_k):
        raise exc

    return _f


def _failed_run(root: Path, exc: BaseException) -> tuple[dict, list[Path]]:
    """A real run whose stage 1 raises `exc`; returns its `run-state.json` and its work dirs.

    No `--keep-workdir`: the scratch directory is left behind because the run failed, which is
    the case #271 is about. The stage-1 stub is the suite's own technique -- `pipeline.run`
    reaches stages through the module names it imports, so replacing `probe.probe` fails the run
    inside the real `run`/`write_run_state` path rather than beside it.
    """
    src = root / "lecture.mp4"
    root.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 2048)
    out = root / "bundle"
    out.mkdir(parents=True, exist_ok=True)
    work_parent = root / "work"
    prev_probe = dyp.probe
    dyp.probe = _Stub(probe=_raises(exc))
    try:
        try:
            dyp.run(
                src,
                dyw.WorkDir(keep=False, parent=work_parent),
                bundle=dyb.Bundle.open(src, output_dir=str(out), overwrite=False),
                stream=io.StringIO(),
            )
        except BaseException:
            pass
    finally:
        dyp.probe = prev_probe
    state = json.loads((out / dyp.RUN_STATE_NAME).read_text(encoding="utf-8"))
    work_dirs = (
        sorted(p for p in work_parent.iterdir() if p.is_dir()) if work_parent.is_dir() else []
    )
    return state, work_dirs


# --- function level: the new keyword, and the keys that were already there -------------------
# Eight completed reports is the decision's own arrangement: it makes `stage_reached` the stage
# that was *running* (9) rather than the last one that finished, so the check would notice a
# change to that computation riding along with the new field.
with tempfile.TemporaryDirectory() as _fn_td:
    _fn_root = Path(_fn_td)
    _fn_src = _fn_root / "lecture.mp4"
    _fn_src.write_bytes(b"x")
    (_fn_root / "bundle").mkdir()
    _fn_reason = "Permission denied: '/x/glossary.md'"
    _fn_reports = [
        dyp.StageReport(i, f"stage {i}", 1.0, f"summary of stage {i}") for i in range(1, 9)
    ]
    _fn_state: dict = {}
    _fn_raised = ""
    try:
        dyp.write_run_state(
            dyb.Bundle.open(_fn_src, output_dir=str(_fn_root / "bundle"), overwrite=False),
            source=_fn_src,
            run_id="r",
            started_at="t",
            reports=_fn_reports,
            outcome="failed",
            detail="PermissionError",
            error=_fn_reason,
        )
        _fn_state = json.loads(
            (_fn_root / "bundle" / dyp.RUN_STATE_NAME).read_text(encoding="utf-8")
        )
    except TypeError as _fn_exc:
        _fn_raised = repr(_fn_exc)

check(
    "#271: write_run_state takes the reason and records it verbatim beside the category",
    _fn_state.get("error") == _fn_reason and _fn_state.get("detail") == "PermissionError",
    f"error={_fn_state.get('error')!r} detail={_fn_state.get('detail')!r} "
    f"raised={_fn_raised or 'nothing'}",
)
check(
    "#271: and the fields that were already there keep their values and meaning",
    _fn_state.get("outcome") == "failed"
    and _fn_state.get("stages_completed") == [1, 2, 3, 4, 5, 6, 7, 8]
    and _fn_state.get("stage_reached") == 9,
    str({k: _fn_state.get(k) for k in ("outcome", "stages_completed", "stage_reached")}),
)


# --- run level: what the bundle a failing run leaves actually says --------------------------
# The failure is a `PermissionError` on a glossary, the shape the issue measured: the run stops
# in stage 1, so `stages_completed` is `[]` and the scratch directory holds no artefacts at all.
with tempfile.TemporaryDirectory() as _rf_td:
    _rf_root = Path(_rf_td)
    _rf_bad = _rf_root / "glossary-chmod000.md"
    _rf_bad.write_text("x", encoding="utf-8")
    _rf_bad.chmod(0o000)
    _rf_state, _rf_work = _failed_run(
        _rf_root, PermissionError(13, "Permission denied", str(_rf_bad))
    )
    _rf_reason = f"[Errno 13] Permission denied: '{_rf_bad}'"
    _rf_blob = json.dumps(_rf_state, ensure_ascii=False)

check(
    "#271: a failed run leaves the reason in the bundle, not only on the terminal",
    _rf_state.get("error") == _rf_reason
    and "Permission denied" in _rf_blob
    and str(_rf_bad) in _rf_blob,
    f"error={_rf_state.get('error')!r} stage_reached={_rf_state.get('stage_reached')}",
)
check(
    "#271: and the recorded reason is the reason alone -- no traceback, no path of this checkout",
    str(_rf_state.get("error")) == _rf_reason
    and "Traceback" not in str(_rf_state.get("error"))
    and CHECKOUT not in str(_rf_state.get("error")),
    f"error={_rf_state.get('error')!r} bytes={len(_rf_blob)} checkout_in_record={CHECKOUT in _rf_blob}",
)
check(
    "#271: the scratch directory is still empty, so the bundle is what has to carry it",
    len(_rf_work) == 1 and not [p for p in _rf_work[0].rglob("*")],
    f"work dirs={[p.name for p in _rf_work]} files={[p.name for p in _rf_work[0].rglob('*')][:3] if _rf_work else []}",
)

# The justification itself: two failures that used to be one record. Same class, same empty
# `stages_completed`, same stage -- different file, and before this only the terminal knew.
with tempfile.TemporaryDirectory() as _two_td:
    _two_root = Path(_two_td)
    _two_a = _two_root / "glossary-a.md"
    _two_b = _two_root / "source-b.mp4"
    _two_a.write_text("x", encoding="utf-8")
    _two_b.write_bytes(b"x" * 2048)
    _two_state_a, _ = _failed_run(
        _two_root / "one", PermissionError(13, "Permission denied", str(_two_a))
    )
    _two_state_b, _ = _failed_run(
        _two_root / "two", PermissionError(13, "Permission denied", str(_two_b))
    )

check(
    "#271: two PermissionErrors that used to be one record are now distinguishable",
    _two_state_a.get("detail") == _two_state_b.get("detail") == "PermissionError"
    and _two_state_a.get("error") != _two_state_b.get("error")
    and str(_two_a) in str(_two_state_a.get("error"))
    and str(_two_b) in str(_two_state_b.get("error")),
    f"a={_two_state_a.get('error')!r} b={_two_state_b.get('error')!r}",
)


# --- the interrupt: a record that says "it did not say why" ----------------------------------
# `str(KeyboardInterrupt())` is `''`, so the verbatim reason is empty. The decision records
# `None`, not `''`: a null value says the run ended without a message, while an empty string
# reads as a field that was never filled in. `detail` stays the class, which is the pin in
# tests/test_stages.py that this work does not touch.
with tempfile.TemporaryDirectory() as _ri_td:
    _ri_root = Path(_ri_td)
    _ri_state, _ = _failed_run(_ri_root, KeyboardInterrupt())
    try:
        _ri_error = _ri_state["error"]
    except KeyError as _ri_exc:
        _ri_error = f"KeyError: {_ri_exc}"

check(
    "#271: an interrupt with no message records None, and detail stays the class name",
    _ri_error is None
    and _ri_state.get("detail") == "KeyboardInterrupt"
    and _ri_state.get("outcome") == "interrupted",
    f"str(KeyboardInterrupt())={str(KeyboardInterrupt())!r} error={_ri_error!r} "
    f"detail={_ri_state.get('detail')!r} outcome={_ri_state.get('outcome')!r}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all run-state checks passed")
