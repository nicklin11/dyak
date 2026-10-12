"""Entry point and subcommand dispatch (ADR-0001 D1).

The surface is the documentation: a binary name cannot teach another agent what
a tool does, so `dyak --help` and each subcommand's help can.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import audit as dya
from . import deps as dyd
from . import exitcodes as ec
from . import bundle as dyb
from . import link as dylnk
from . import pipeline as dyp
from . import repair as dyr
from . import runner
from . import synth as dysy
from .settings import SOURCE_DEFAULT
from .deps import PROCESS_REQUIRES, Check, run_all, worst_code
from .workspace import WorkDir

# Subcommands that are not built yet. Each names its tracking issue so the failure is a
# pointer, not a dead end. The standalone subcommand is unbuilt; the stage itself runs inside
# `process`, so "stage 9, tracked in #11" would now be a false statement about the pipeline.
PENDING: dict[str, str] = {
    "audit": "the standalone `dyak audit NOTE` entry point, tracked in #11; stage 9 "
    "does run as part of `process`",
}


def _render(checks: list[Check]) -> None:
    width = max((len(c.name) for c in checks), default=4)
    for c in checks:
        if c.ok:
            mark = "ok  "
        elif c.fatal:
            mark = "FAIL"
        else:
            mark = "skip"
        print(f"  [{mark}] {c.name.ljust(width)}  {c.detail}")
        if c.path:
            print(f"           {'path'.ljust(width)}  {c.path}")
        if c.version and c.version != "ok":
            print(f"           {'version'.ljust(width)}  {c.version}")


def _doctor(_args: argparse.Namespace) -> int:
    """Stage 0. Reports what is missing, what is present, and what to do."""
    checks = run_all()
    _render(checks)

    failed = [c for c in checks if c.failed]
    if not failed:
        print("\ndyak: all required dependencies present.")
        return ec.OK

    print("", file=sys.stderr)
    for c in failed:
        print(f"dyak: missing or broken dependency: {c.name}", file=sys.stderr)
        # The remediation is printed for *every* failure, not only for exit 2. It used to
        # be gated on MISSING_DEPENDENCY, which meant a dependency that is installed and
        # unreachable -- the case an endpoint backend introduces -- was named and then
        # abandoned with no indication of what to do about it.
        if c.remediation:
            print(f"dyak:   remediation: {c.remediation}", file=sys.stderr)
        # ADR-0001 D2: the dependency's own stderr, reproduced verbatim.
        if c.stderr:
            print(f"dyak:   {c.name} stderr:", file=sys.stderr)
            for line in c.stderr.splitlines():
                print(f"dyak:     {line}", file=sys.stderr)
    return worst_code(checks)


def _preflight(no_llm: bool = False) -> int:
    """Stage 0 as a gate: return the exit code, or OK if the run may start.

    The stage 0-4 dependencies are always fatal. The LLM endpoint is fatal too, because
    stages 7, 9 and 10 need it and a run without it spends its STT and frame time before
    failing (#126). `no_llm` is the explicit consent to the template skeleton instead. A
    *configured* vision endpoint is fatal for the same reason -- stage 6 needs it, and a dead
    one is otherwise discovered there after the transcription and frame extraction have run
    (#167) -- while an endpoint that was never configured is not: stage 6 marks every frame
    `NO_ENDPOINT` and the pipeline still writes a note. The vault is reported but not enforced.
    """
    required = PROCESS_REQUIRES if no_llm else PROCESS_REQUIRES | {"llm"}
    if dyd.vlm_configured():
        required = required | {"vlm"}
    checks = run_all(required=required)
    failed = [c for c in checks if c.failed]
    degraded = [c for c in checks if not c.ok and not c.failed]

    for c in degraded:
        # Printed before the early return below: a dependency that is down is not a
        # reason to refuse this run, but it is still a fact the user wants before the
        # stage that needs it.
        if c.name == "llm":
            # Reached only under --no-llm: the note is the template skeleton, and the
            # stages that need the endpoint (6 as the vision fallback, 7 and 9) fall back.
            print(
                "dyak: note: no LLM endpoint (--no-llm): the note will be the template "
                "skeleton; stages 6, 7 and 9 use the endpoint",
                file=sys.stderr,
            )
        elif c.name == "vlm":
            # Reached only when neither role is configured, so there is no fallback and
            # stage 6 has nothing to caption with. The generic note below would be false:
            # stage 6 is exactly the stage that uses this role.
            print(
                "dyak: note: no vision endpoint: stage 6 will caption no frame; "
                f"set {dyd.VLM_ENV} and {dyd.VLM_MODEL_ENV}, or {dyd.LLM_ENV} as the fallback",
                file=sys.stderr,
            )
        else:
            print(
                f"dyak: note: {c.name} is unavailable, but stages 0-{dyp.IMPLEMENTED} do not use it",
                file=sys.stderr,
            )
    if not failed:
        return ec.OK

    print("dyak: refusing to start; run `dyak doctor` for detail.", file=sys.stderr)
    for c in failed:
        print(f"dyak: {c.name}: {c.detail}", file=sys.stderr)
        if c.remediation:
            print(f"dyak:   remediation: {c.remediation}", file=sys.stderr)
    return worst_code(checks)


def _pending(name: str) -> int:
    print(
        f"dyak: `{name}` is not implemented yet ({PENDING[name]}).\n"
        f"dyak: run `dyak doctor` for what is available.",
        file=sys.stderr,
    )
    return ec.USAGE


def _process(args: argparse.Namespace) -> int:
    """Stages 1-12: probe -> audio -> stt -> frames -> quality -> caption -> synth -> lint
    -> audit -> repair -> link -> report.

    Exits 0 only when stage 12 has stat'd the filesystem and found every artefact it
    claimed. Exit 0 in ADR-0001 means "all artefacts written and verified", and the
    second half of that is the half every pipeline before this one skipped: ADR-0001 D3 records a
    run that produced zero frames and exited successfully, because ffmpeg wrote an empty
    file and exited 0.

    Content verdicts take precedence over a late vault-export failure: 5 (audit errors) >
    4 (soft frames) > 6 (could not verify artefacts) > 1 (degraded note) > 3 (vault export
    failed) > 0. Every message is printed regardless of which code comes out.
    """
    if not args.path:
        print("dyak: process needs a recording to work on.", file=sys.stderr)
        print("dyak:   usage: dyak process PATH", file=sys.stderr)
        return ec.USAGE

    code = _preflight(no_llm=args.no_llm)
    if code != ec.OK:
        return code

    parent = Path(args.workdir).expanduser() if args.workdir else None
    try:
        work = WorkDir(keep=args.keep_workdir, parent=parent)
    except OSError as exc:
        # --workdir is user-supplied, so a bad parent is a usage error rather
        # than an internal one, and it is far more likely than a real mkdtemp
        # failure. Checked here because nothing exists yet to retain.
        print(f"dyak: cannot use the work directory: {exc}", file=sys.stderr)
        print(
            "dyak:   remediation: --workdir must name an existing writable directory",
            file=sys.stderr,
        )
        return ec.USAGE
    try:
        source = Path(args.path).expanduser()
    except (OSError, RuntimeError) as exc:
        print(f"dyak: cannot expand the input path: {exc}", file=sys.stderr)
        return ec.USAGE
    try:
        output = dyb.Bundle.open(
            source,
            output_dir=getattr(args, "output_dir", None),
            overwrite=getattr(args, "overwrite", False),
        )
    except OSError as exc:
        print(f"dyak: cannot use the output directory: {exc}", file=sys.stderr)
        print(
            "dyak:   remediation: --output-dir must name a writable location",
            file=sys.stderr,
        )
        return ec.USAGE
    try:
        result = dyp.run(
            source,
            work,
            bundle=output,
            glossary_path=getattr(args, "glossary", None),
            terms_dir=getattr(args, "terms_dir", None),
            vault_path=getattr(args, "vault_path", None),
        )
    except runner.DependencyError as exc:
        work.retain(exc.message)
        runner.report(exc)
        print(f"dyak: {work.note()}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        work.retain("interrupted by the user")
        print(f"dyak: {work.note()}", file=sys.stderr)
        return ec.INTERRUPTED
    except Exception as exc:
        work.retain(f"{type(exc).__name__}: {exc}")
        # An unexpected exception reaches the user as a traceback. Without this
        # line the traceback would be the only thing they see, and the retained
        # artefacts -- the reason retention exists -- would be unfindable.
        print(f"dyak: {work.note()}", file=sys.stderr)
        raise
    else:
        work.release()

    print()
    print(f"  source     {result.source}")
    print(f"  transcript {result.transcript.summary()}")
    print(f"  stages {dyp.FIRST_STAGE}-{dyp.IMPLEMENTED} done in {result.seconds:.1f}s")
    print(f"  output     {result.bundle.root}  ({result.bundle.summary()})")
    # "One command" has to mean the note ends up in the vault, not that it ends up somewhere
    # the user has to know to open. The vault resolves the same way doctor resolves it, so
    # `dyak process video.mp4` with nothing configured does what doctor just reported.
    # `--no-vault-export` is the opt-out; the export is verified by size.
    # Its failure is reported now but its exit code is deferred until after the
    # bundle's content verdicts, so it cannot mask audit/quality/verification.
    export_failed = False
    if not getattr(args, "no_vault_export", False):
        vault = dyd.resolve_vault(getattr(args, "vault_path", None))
        subdir = (
            getattr(args, "vault_subdir", None)
            or f"{dyb.DEFAULT_EXPORT_SUBDIR}/{result.bundle.root.name}"
        )
        # A vault that does not exist is reported, not created. `export_to_vault` ends in
        # `mkdir(parents=True)`, so exporting to a mistyped path silently materialises an
        # empty directory -- and then `check_vault` finds it exists, is a directory and is
        # writable, so the next `dyak doctor` reports a healthy vault that is empty.
        # That turns a configuration mistake into a state the tool then certifies.
        if not vault.is_dir():
            print(
                f"dyak: vault does not exist, so the note was not exported: {vault}",
                file=sys.stderr,
            )
            print(
                f"dyak:   the bundle is intact at {result.bundle.root}; set "
                f"{dyd.VAULT_ENV} or pass --vault-path to export it",
                file=sys.stderr,
            )
        else:
            # First run: nothing named a vault, so write the one used down to a file.
            # A default nobody can find is a default nobody will change, and this is meant
            # to be settable rather than compiled in -- which needs somewhere to set it.
            # Nothing named a vault anywhere: not `--vault-path`, not `$DYAK_VAULT`, not the
            # settings file. `resolve` reports which layer won, so the check is a statement
            # about the resolver rather than a second read of the environment.
            unset_everywhere = (
                not dyd.settings_path().is_file()
                and dyd.resolve(dyd.VAULT_ENV, flag=getattr(args, "vault_path", None)).source
                == SOURCE_DEFAULT
            )
            written = None
            if unset_everywhere:
                try:
                    written = dyd.write_settings(vault)
                except OSError as exc:
                    print(
                        f"dyak: could not write {dyd.settings_path()}: {exc}",
                        file=sys.stderr,
                    )
            try:
                copied = result.bundle.export_to_vault(
                    vault,
                    subdir=subdir,
                    images=getattr(args, "vault_images", False),
                    force=getattr(args, "force_export", False),
                )
            except OSError as exc:
                print(f"dyak: vault export failed: {exc}", file=sys.stderr)
                print(
                    "dyak:   remediation: the vault is an export target; the bundle is intact",
                    file=sys.stderr,
                )
                export_failed = True
            else:
                if written is not None:
                    print(f"  vault      {vault / subdir} (first run — written to {written})")
                    print(f"             {len(copied)} files exported")
                else:
                    print(f"  vault      {len(copied)} files exported to {vault / subdir}")
                    print(
                        f"             (change with {dyd.VAULT_ENV}, --vault-path, or "
                        f"{dyd.settings_path()})"
                    )
    if not result.quality.ok:
        # ADR-0001 D4: the note is still written; a bad frame means the crops are soft, not that
        # the pipeline stopped. The quality report is the artefact that explains why.
        print(
            f"dyak: quality gate failed for {len(result.quality.failed)}"
            f"/{len(result.quality.frames)} frames; see {result.bundle.root}",
            file=sys.stderr,
        )
        for line in result.quality.explain().splitlines():
            print(f"dyak: {line.strip()}", file=sys.stderr)
        print(
            "dyak:   remediation: lower quality.decay to tighten the gate, or "
            "re-extract with a higher source bitrate -- interpolation cannot recover "
            "text that was never encoded",
            file=sys.stderr,
        )
    audit_errors = [f for f in result.audit.findings if f.severity == "ERROR"]
    if audit_errors:
        # ADR-0001 D5: the note is written and FLAGGED, never withheld. Exit 5 is that flag.
        # Naming the count and the rule is the whole point -- an exit code with no list
        # attached is a code the reader cannot act on.
        print(
            f"dyak: audit found {len(audit_errors)} error-tier findings; the note is "
            f"written and flagged, not withheld; see {result.bundle.root}",
            file=sys.stderr,
        )
        for finding in audit_errors[:10]:
            print(f"dyak:   line {finding.line} {finding.rule}: {finding.detail}", file=sys.stderr)
        if len(audit_errors) > 10:
            print(
                f"dyak:   ... and {len(audit_errors) - 10} more in "
                f"{result.bundle.root / dya.REPORT_NAME}",
                file=sys.stderr,
            )
        # Built from the constant the stage publishes with and the bundle root the run wrote
        # to, rather than typed as prose. These two strings said `audit/audit.json` and
        # `repair/repair.json` for as long as they existed: `Bundle.publish` ignores the
        # registry key when choosing a filename (`bundle.py:144`), so `audit/audit.json` and
        # `repair/repair.json` are registry keys and the files sit in the bundle root. The
        # bundle has no `audit/` or `repair/` directory. Both branches are exit 5 and exit 1,
        # and thirteen runs have exited 0, so the wrong path was never shown to anyone. #91.
        print(
            "dyak:   remediation: read the quoted line in "
            f"{result.bundle.root / dya.REPORT_NAME} and check it against the recording -- "
            f"stage 10 repaired what was mechanical and declined the rest, by name, in "
            f"{result.bundle.root / dyr.REPORT_NAME}",
            file=sys.stderr,
        )
    print(f"dyak: {work.note()}")
    # Artefact paths are printed only when the directory still exists. Pointing
    # the reader at files that release() just deleted is worse than saying
    # nothing: it looks like an output location and is not one.
    if work.retained:
        for name in sorted(p.name for p in work.path.iterdir()):
            print(f"dyak:   {work.path / name}")

    # Exit code precedence, and why it is this order.
    #
    #   5 -- the note exists and contains errors. This is the most specific statement
    #        about the deliverable, so it outranks the others. When the quality gate also
    #        failed, 5 still wins: the reader is told the note has errors AND, above, that
    #        the frames were soft. Reporting 4 alone would hide the note's own verdict.
    #   4 -- a content frame was soft. The note exists; its inputs were not.
    #   6 -- the pipeline ran but required artefacts could not be verified: stage 12's end-of-run
    #        check, or a run that stopped at the stage which needed the artefact (ADR-0001 D3).
    #   1 -- the note is a template, not a model synthesis.
    #   3 -- the vault export failed, but the bundle is intact and no stronger content verdict
    #        was produced.
    #   0 -- every stage ran, every artefact is on disk, and the note was actually synthesised.
    if audit_errors:
        return ec.AUDIT_FINDINGS
    if not result.caption_outcome.ok:
        # The endpoint was configured and reachable enough to try, and nothing came back.
        # That is a dependency failing, not a content verdict, so it is exit 3 -- and it is
        # checked before the quality gate because a dead vision endpoint would otherwise be
        # reported as soft crops, sending the reader to tune the wrong thing.
        outcome = result.caption_outcome
        print(
            f"dyak: stage 6 captioned 0 of {len(result.caption.considered)} frames "
            f"via {outcome.model} ({outcome.failed} failed); see {result.bundle.root}",
            file=sys.stderr,
        )
        for alignment in result.caption.alignments:
            if alignment.caption_status not in ("OK", "GATED_OUT"):
                print(
                    f"dyak:   {alignment.frame}: {alignment.caption_status} -- {alignment.reason}",
                    file=sys.stderr,
                )
        print(
            "dyak:   remediation: check that the model serves vision requests; "
            "DYAK_VLM_ENDPOINT/DYAK_VLM_MODEL select it, and fall back to the "
            "DYAK_LLM_* pair when unset",
            file=sys.stderr,
        )
        return ec.DEPENDENCY_FAILED
    if not result.quality.ok:
        return ec.QUALITY_GATE_FAILED
    if result.report.low_coverage:
        # Before the degraded check, deliberately. A truncated transcript also makes the note
        # degraded, and the degraded branch would return 7 and hide the reason: that the note
        # is missing part of the lecture (#117).
        print(
            f"dyak: stage 7 sent the model {result.report.coverage:.0%} of the transcript, "
            f"below the {result.report.coverage_threshold:.0%} coverage threshold; the note is "
            f"missing part of the lecture. See {dysy.REPORT_NAME} in the bundle.",
            file=sys.stderr,
        )
        return ec.LOW_COVERAGE
    if result.report.truncated:
        # Also before the degraded check, and for the same reason: a section cut off at the
        # output limit makes the note degraded too, and the degraded branch would return 7 and
        # hide the cause (#128). After the coverage check because a shortened transcript can
        # shorten the sections too, and the input is the thing to report first.
        numbers = ", ".join(str(n) for n in result.report.sections_truncated)
        print(
            f"dyak: {len(result.report.sections_truncated)} of "
            f"{result.report.sections_total} sections hit the model's output limit and end "
            f"mid-sentence (sections {numbers}); the note is missing the end of them. See "
            f"{dysy.REPORT_NAME} in the bundle.",
            file=sys.stderr,
        )
        return ec.TRUNCATED_SECTIONS
    if result.note.degraded:
        for warning in result.note.notes:
            print(f"dyak: synthesis: {warning}", file=sys.stderr)
        if result.note.synthesizer == "template":
            print(
                "dyak: the note was produced by the template synthesizer, not by a model. "
                "Set DYAK_LLM_ENDPOINT and re-run for a synthesised note.",
                file=sys.stderr,
            )
            return ec.USAGE
        print(
            f"dyak: the model-written note from '{result.note.synthesizer}' is incomplete; "
            "review the synthesis warnings above and the note before relying on it.",
            file=sys.stderr,
        )
        return ec.INCOMPLETE_NOTE
    if not result.report.ok:
        for entry in result.report.missing:
            print(
                f"dyak: MISSING {entry['artefact']} ({entry['detail']}): {entry['why']}",
                file=sys.stderr,
            )
        for entry in result.report.inconsistent:
            # Not a missing file: both artefacts are present and disagree about a frame, which
            # is how #82 stayed invisible -- each file was internally consistent (#87).
            where = entry["artefact"] + (f":{entry['frame']}" if entry.get("frame") else "")
            print(
                f"dyak: INCONSISTENT {where} ({entry['detail']}): {entry['why']}",
                file=sys.stderr,
            )
        print(
            "dyak:   this is ADR-0001 D3: stages above reported success, and the filesystem "
            "disagrees -- a required artefact is absent, or two of them contradict each other. "
            "Not exiting 0.",
            file=sys.stderr,
        )
        return ec.ARTEFACT_VERIFICATION_FAILED
    if export_failed:
        return ec.DEPENDENCY_FAILED
    print(f"dyak: all stages complete; artefacts verified in {result.bundle.root}")
    return ec.OK


def _audit(_args: argparse.Namespace) -> int:
    return _pending("audit")


def _terms_dir(args: argparse.Namespace):
    """Resolved terms directory, or None with the reason already printed.

    `_term_*` refuses to run rather than reporting "no terms found": those two look the
    same in the output and only one of them means the vault was read.
    """
    resolved = dylnk.resolve_terms_dir(
        getattr(args, "terms_dir", None), getattr(args, "vault_path", None)
    )
    if resolved is None:
        print(
            f"dyak: no terms directory. Pass --terms-dir DIR, set "
            f"{dylnk.TERMS_ENV}, or pass --vault-path DIR "
            f"(looked for <vault>/{dylnk.COURSES_DIRNAME}/{dylnk.TERMS_DIRNAME}).",
            file=sys.stderr,
        )
    return resolved


def _term_link(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = dylnk.load_terms(terms_dir)
    if not terms:
        print(f"dyak: no term notes in {terms_dir}", file=sys.stderr)
        return ec.USAGE
    targets = dylnk.collect_targets(args.paths, terms_dir, args.vault_path)
    if not targets:
        print("dyak: no target notes to link", file=sys.stderr)
        return ec.USAGE
    report = dylnk.link_paths(targets, terms, write=args.write)
    print(f"dyak: {report.summary()}")
    if report.per_term:
        top = sorted(report.per_term.items(), key=lambda kv: -kv[1])[:10]
        print("dyak:   " + ", ".join(f"{name}x{count}" for name, count in top))
    if not args.write and report.changed:
        print("dyak:   dry run -- pass --write to apply")
    return ec.OK


def _term_index(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = dylnk.load_terms(terms_dir)
    lectures = dylnk.collect_targets([], terms_dir, args.vault_path)
    table = dylnk.index_report(lectures, terms, only_used=args.only_used)
    if not args.write:
        print(table)
        return ec.OK
    dest = terms_dir / "_index.md"
    dest.write_text(
        "---\ntype: generated\n---\n\n"
        "> Сгенерировано `dyak term index --write`. Правь не руками.\n\n" + table + "\n",
        encoding="utf-8",
    )
    print(f"dyak: wrote {dest}")
    return ec.OK


def _term_check(args: argparse.Namespace) -> int:
    terms_dir = _terms_dir(args)
    if terms_dir is None:
        return ec.USAGE
    terms = dylnk.load_terms(terms_dir)
    lectures = dylnk.collect_targets([], terms_dir, args.vault_path)
    print(dylnk.check_report(terms, lectures, terms_dir))
    return ec.OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyak",
        description="Turn a lecture recording into a structured, audited Markdown note. "
        f"Stages {dyp.FIRST_STAGE}-{dyp.IMPLEMENTED} are implemented. process exits 0 "
        f"only when stage 12 has verified the artefacts on disk: {ec.AUDIT_FINDINGS} if "
        f"the audit found errors, {ec.QUALITY_GATE_FAILED} if frames were soft, "
        f"{ec.ARTEFACT_VERIFICATION_FAILED} if artefacts could not be verified, and "
        f"{ec.DEPENDENCY_FAILED} if a dependency or vault export failed.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser(
        "doctor",
        help="report missing/conflicting dependencies",
        description="Check every external dependency and exit with the reason "
        "if one is absent or broken.",
    )
    p_doctor.set_defaults(fn=_doctor)

    p_process = sub.add_parser(
        "process",
        help="run the pipeline on a recording",
        description="Transcribe, extract frames, synthesise, audit, link, report. "
        f"Stages 0-{dyp.IMPLEMENTED} are all implemented. Exits 0 only when the "
        "bundle it wrote has been verified on disk.",
    )
    p_process.add_argument("path", nargs="?", help="video or audio file")
    p_process.add_argument(
        "--workdir",
        metavar="DIR",
        help=(
            "parent directory for the managed work dir "
            "(default: $DYAK_WORKDIR, the `workdir` settings key, or $TMPDIR)"
        ),
    )
    p_process.add_argument(
        "--output-dir",
        metavar="DIR",
        help=(
            "where to write the output bundle (default: $DYAK_OUTPUT_DIR, the `output_dir` "
            "settings key, or $XDG_STATE_HOME/dyak/<lecture> — "
            "~/.local/state/dyak/<lecture> when XDG_STATE_HOME is unset)"
        ),
    )
    p_process.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output bundle instead of writing into it",
    )
    p_process.add_argument(
        "--vault-path",
        metavar="DIR",
        help=(
            "copy the finished bundle into this vault; never a pipeline blocker. "
            "defaults to the configured vault, and the run writes into it either way -- "
            "use --no-vault-export to keep the note in the bundle only"
        ),
    )
    p_process.add_argument(
        "--no-vault-export",
        action="store_true",
        help="finish the bundle without copying it into the vault",
    )
    p_process.add_argument(
        "--vault-images",
        action="store_true",
        help=(
            "also copy images/ into the vault. Off by default: the frames are most of the "
            "bytes (50 of 59 MiB on lecture 1) and the note refers to none of them, so they "
            "stay in the bundle as the pipeline's evidence"
        ),
    )
    p_process.add_argument(
        "--vault-subdir",
        metavar="REL",
        default=None,
        help=(
            "where inside the vault the bundle lands, relative to it "
            f"(default: <vault>/{dyb.DEFAULT_EXPORT_SUBDIR}/<lecture>/, one directory per "
            "lecture, #118). Not optional in practice: a bundle is ~188 files, and the vault "
            "root holds the user's own notes"
        ),
    )
    p_process.add_argument(
        "--force-export",
        action="store_true",
        help=(
            "replace a vault note that was edited by hand after the last export "
            "(default: refuse, and report which file)"
        ),
    )
    p_process.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "run without an LLM endpoint: the note is the template skeleton, not a model's. "
            "Without this flag, `process` refuses to start when no endpoint is configured"
        ),
    )
    p_process.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the work dir even when the run succeeds",
    )
    p_process.add_argument(
        "--glossary",
        metavar="FILE",
        help=(
            "ASR term glossary for stages 9 and 10, in the project's "
            "`| variant | canonical | domain | date |` format. Without it the term-drift "
            "rule finds nothing and the audit says so."
        ),
    )
    p_process.add_argument(
        "--terms-dir",
        metavar="DIR",
        help=(
            "atomic term notes for stage 11. Default: $DYAK_TERMS_DIR, the `terms_dir` "
            "settings key, then <vault-path>/mscs/_terms. Without it the note is written "
            "unlinked and the stage says so."
        ),
    )
    p_process.set_defaults(fn=_process)

    p_audit = sub.add_parser(
        "audit",
        help="audit a note that already exists",
        description="Run the audit stage against an existing note. Not implemented in this build.",
    )
    p_audit.add_argument("note", nargs="?", help="Markdown note to audit")
    p_audit.set_defaults(fn=_audit)

    # `mscs-termlink`'s three subcommands, kept as one group: the ported tool keeps its own
    # vocabulary, and a bare top-level `check` would sit confusingly next to `doctor`.
    p_term = sub.add_parser(
        "term",
        help="term vocabulary: link / index / check the vault's atomic term notes",
        description=(
            "Ported from ~/.local/bin/mscs-termlink (issue #8). The terms directory is "
            "resolved from --terms-dir, then $DYAK_TERMS_DIR, then the `terms_dir` settings "
            "key, then <--vault-path>/mscs/_terms."
        ),
    )
    term_sub = p_term.add_subparsers(dest="term_cmd", required=True)

    t_link = term_sub.add_parser("link", help="inject wikilinks; dry run unless --write")
    t_link.add_argument("paths", nargs="*", help="files or directories (default: lecture notes)")
    t_link.add_argument("--write", action="store_true", help="persist the changes")
    t_link.add_argument("--terms-dir", metavar="DIR")
    t_link.add_argument("--vault-path", metavar="DIR")
    t_link.set_defaults(fn=_term_link)

    t_index = term_sub.add_parser("index", help="report which lectures mention which term")
    t_index.add_argument("--write", action="store_true", help="write _index.md")
    t_index.add_argument("--only-used", action="store_true", help="omit terms used nowhere")
    t_index.add_argument("--terms-dir", metavar="DIR")
    t_index.add_argument("--vault-path", metavar="DIR")
    t_index.set_defaults(fn=_term_index)

    t_check = term_sub.add_parser("check", help="diagnostics on terms and lecture notes")
    t_check.add_argument("--terms-dir", metavar="DIR")
    t_check.add_argument("--vault-path", metavar="DIR")
    t_check.set_defaults(fn=_term_check)

    return parser


def _line_buffer() -> None:
    """Make a redirected log live. `pipeline` flushes exactly once, before stage 3.

    `sys.stdout` is a `TextIOWrapper` over fd 1, and CPython block-buffers that whenever
    fd 1 is not a tty, in 8192-byte chunks. Measured on run 14: with stdout redirected to a
    file, the log stopped at `[3/12] stt starting` for 16m18s while stages 1-8 finished on
    disk and the process sat in stage 9. A log that lags the run cannot be used to tell a
    slow stage from a hung one, which is the only thing it is wanted for.

    Reconfiguring here rather than flushing at each call site fixes every stream the
    pipeline and the stages write to, including the ones they receive as `stream=`.
    `StringIO` and other test doubles have no `reconfigure`, so this is not an error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)


def main(argv: list[str] | None = None) -> int:
    _line_buffer()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 for a usage error, and 2 is MISSING_DEPENDENCY in the
        # ADR table. Without this remap a typo'd subcommand reads as a missing
        # dependency, and a script branching on the code takes the wrong action.
        code = exc.code
        if code in (0, None):
            return ec.OK
        return ec.USAGE
    # #156: the settings file invites hand-editing, so a key it holds that no setting reads
    # (or a value dropped for not being a string) is reported once here, before the command
    # runs. `read_settings` cannot do it -- `resolve_vault` calls it several times per run,
    # and the same warning three times is noise. Silent when the file is absent or correct.
    dyd.report_settings()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
