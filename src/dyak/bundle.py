"""The output bundle: what a run leaves behind.

Separate from `workspace.py` because the two have opposite lifecycles. The work dir is
scratch: it is removed on success unless asked otherwise. The bundle is the deliverable
and must survive. Folding them together would mean either keeping 137.9 MiB of extracted
audio on every successful run, or deleting the note.

**The default is `$XDG_STATE_HOME/dyak/<lecture>`, not `./output/<lecture>`.** ADR-0001 D9
exists because artefacts written into a working directory end up in whatever repository
the user happened to be standing in. A CWD-relative default reintroduces that one level
down: run it from a git checkout and a 137.9 MiB wav plus a 3.7 MB transcript land in the
tree. XDG_STATE_HOME is per-user, per-purpose, and the conventional home for exactly this.

The vault is an **export target**, not the root. `export_to_vault` copies finished files
and verifies each one landed. Two write paths with different semantics is how a tool ends
up with half a note in the vault, so there is only ever one writer and the destination is
checked.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

NOTE_NAME = "note.md"
AUDIT_NAME = "audit.md"
IMAGES = "images"

#: Every filename a run can leave in the bundle root, as literals rather than imports.
#:
#: The stage modules define most of these as constants, but `report.py` and `pipeline.py`
#: import each other and `stt` is a package, so importing the whole set here would close a
#: cycle. Literals drift the moment a stage adds an artefact, so `tests/test_claims.py`
#: asserts this set against the files a run actually produces: a name added to one and not
#: the other fails that check. The set is what the test reads, which is why the duplication
#: is tolerable -- and `clear_root` is safe without it, because it only ever removes names
#: from here, never anything else in the directory.
RUN_ARTEFACTS: frozenset[str] = frozenset(
    {
        # stages 1-2 -- what the source is
        "source-provenance.json",
        "detect.json",
        # stage 3 -- transcription
        "transcript.json",
        "transcript.raw.json",
        "transcript.txt",
        "stt-provenance.json",
        # stage 4 -- frames
        "manifest.tsv",
        # stages 5-6 -- the frame gates
        "quality.json",
        "quality-provenance.json",
        "captions.json",
        "caption-provenance.json",
        # stage 7 -- the note itself
        "note.md",
        "note.synth.md",
        "synth.json",
        "synth-provenance.json",
        "synth-llm-transcript.json",
        # stages 8-9 -- the gates over the note
        "lint.json",
        "lint-provenance.json",
        "audit.json",
        "audit-provenance.json",
        "audit-llm-transcript.json",
        # stages 10-11 -- what was changed and linked
        "note.repaired.md",
        "repair.json",
        "repair-provenance.json",
        "note.linked.md",
        "link.json",
        "link-provenance.json",
        # stage 12, and the run's own timings
        "report.json",
        "report-provenance.json",
        "timings.json",
    }
)

#: Where a bundle lands inside a vault unless told otherwise. Its own directory, because the
#: alternative is writing ~188 files into the directory the user's own notes live in.
DEFAULT_EXPORT_SUBDIR = "dyak"
#: digests of what the last export wrote, so a hand edit in the vault is detectable (#118).
EXPORT_LEDGER = "vault-export.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_ledger(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def state_root() -> Path:
    """`$XDG_STATE_HOME/dyak`, or `~/.local/state/dyak`."""
    configured = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".local/state"
    return base / "dyak"


def slugify(name: str) -> str:
    """A filesystem-safe directory name.

    Cyrillic is kept: the lectures here are `Оптимальные СУ`, and transliterating them to
    `Optimal Control Systems` would mean the directory does not match its contents. Only
    characters that are genuinely unusable are replaced.
    """
    cleaned = "".join(c if (c.isalnum() or c in " .-_") else "-" for c in name.strip())
    cleaned = "-".join(cleaned.split())
    return cleaned.strip("-.") or "lecture"


@dataclass
class Bundle:
    """One run's output directory."""

    root: Path
    created: bool = False
    artefacts: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        source: Path | str | None = None,
        *,
        output_dir: str | None = None,
        overwrite: bool = False,
    ) -> Bundle:
        """Resolve the bundle root and create it.

        Precedence: `--output-dir` > `DYAK_OUTPUT_DIR` > `$XDG_STATE_HOME/dyak`.
        The name comes from the source file's stem unless an explicit directory was given,
        in which case that directory *is* the bundle root -- `--output-dir /tmp/x` must not
        produce `/tmp/x/1_lecture_OCS`, which is a different path than the user asked for.
        """
        explicit = output_dir or os.environ.get("DYAK_OUTPUT_DIR", "").strip() or None
        if explicit:
            root = Path(explicit).expanduser().resolve()
        else:
            stem = slugify(Path(source).stem) if source else "lecture"
            root = (state_root() / stem).resolve()

        existed = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        bundle = cls(root=root, created=not existed)
        if overwrite and existed:
            # #119: the directory may be a user's own (`--output-dir ~/notes`). Remove only what
            # a run writes -- the same set `clear_root` uses -- and the `images/` directory.
            # A user file alongside them survives, and so does the directory itself.
            bundle.clear_root()
            if (root / IMAGES).is_dir():
                bundle.clear_images()
        return bundle

    @property
    def images(self) -> Path:
        target = self.root / IMAGES
        target.mkdir(parents=True, exist_ok=True)
        return target

    def clear_images(self) -> int:
        """Empty `images/` before this run's frames land in it. Returns files removed.

        The directory accumulated across runs. `Bundle.open` is called with
        `overwrite=False` and `images` only ever `mkdir(exist_ok=True)`, so nothing removed
        what a previous run left. Measured on lecture 1 after three runs: `images/` held 73
        PNGs where the manifest listed 58, and the 15 extras were all at timestamps below
        135.5 s -- exactly the first run's range, before the `-frame_pts` timebase fix made
        every frame come from the opening minutes.

        Three consequences, none of them visible from any artefact:

        - `images/` is not a record of this run. A frame the current gate never saw sits
          beside the frames it did, indistinguishable by name.
        - stage 12 verifies the registered artefacts, so those 15 were never checked and
          never failed.
        - `export_to_vault` copies by registry, so they were skipped there too -- but the
          bundle's own `summary()` count disagreed with the directory by the same 32 files.

        `frames.run` clears its own output directory every run; this is the same rule for the
        directory the frames end up in.
        """
        target = self.images
        removed = 0
        for stale in target.iterdir():
            if stale.is_file():
                stale.unlink()
                removed += 1
        return removed

    def clear_root(self) -> list[str]:
        """Remove the previous run's artefacts from the root. Returns what was removed.

        `clear_images` fixed `images/` and left the root with the same defect one level up.
        Measured on lecture 1, mid-run: at 06:20 with run 14 in stage 9 the root held **12
        files written by run 13** at 05:50 alongside the 17 run 14 had just written. Two of
        those twelve are on the `REQUIRED` set -- `audit.json` and `repair.json` -- and
        `_check` accepts any file that exists and is non-empty. So a run whose stage 9
        produced no audit still had `audit.json` recorded as verified, and exited 0. The
        stage that exists to catch that was reading the previous run's file.

        Why it did not show up in thirteen runs: `tests/test_stages.py` builds a fresh `tmp`
        directory per test, so the bundle is never dirty when the tests look at it.

        Only names in `RUN_ARTEFACTS` are removed. `--output-dir DIR` makes `DIR` the bundle
        root, and a user who points it at a directory holding anything of their own keeps it:
        this is deliberately narrower than `overwrite=True`, which `rmtree`s the whole thing.
        Directories are never touched, so `images/` is left to `clear_images`.
        """
        if not self.root.is_dir():
            return []
        removed: list[str] = []
        for stale in sorted(self.root.iterdir()):
            if stale.is_file() and stale.name in RUN_ARTEFACTS:
                stale.unlink()
                removed.append(stale.name)
        return removed

    def path(self, name: str) -> Path:
        return self.root / name

    def record(self, key: str, path: Path) -> Path:
        self.artefacts[key] = Path(path)
        return Path(path)

    def publish(self, key: str, path: Path, *, name: str | None = None) -> Path:
        """Move a work-dir artefact into the bundle and remember it.

        Raises `FileNotFoundError` when the source is not there. It used to return the
        dangling source path, record nothing and raise nothing, and that is the mechanism
        that shipped #82: stage 5 published `quality.json`, stage 6 read the work-dir path,
        `align` read the missing file as "no gate information", so stage 5's gate was a
        no-op for eight runs and no run reported it (#89).

        A caller whose artefact may legitimately be absent calls `publish_optional` instead.

        `shutil.move` across filesystems is a copy plus an unlink; if the source is on
        another mount the copy can fail halfway, which is why the destination is verified
        afterwards rather than assumed. A 3.7 MB transcript is cheap to re-run; a
        half-written note is not something to discover later.
        """
        source_path = Path(path)
        if not source_path.exists():
            raise FileNotFoundError(
                f"cannot publish '{key}': {source_path} does not exist. If the artefact is "
                "legitimately optional, publish it with `publish_optional`."
            )
        destination = self.root / (name or source_path.name)
        try:
            shutil.move(str(source_path), str(destination))
        except OSError:
            # Different mount, or a rename that is not permitted. Fall back to a copy.
            shutil.copy2(str(source_path), str(destination))
        self.record(key, destination)
        return destination

    def publish_optional(self, key: str, path: Path, *, name: str | None = None) -> Path | None:
        """`publish` for an artefact a run may legitimately not have written.

        Returns None and records nothing when the source is absent, so a caller can tell the
        two outcomes apart without stat-ing a path that may be dangling (#89).
        `synth-llm-transcript.json` is the standing example: the template synthesizer makes
        no calls, so there is no transcript to record.
        """
        if not Path(path).exists():
            return None
        return self.publish(key, path, name=name)

    def summary(self) -> str:
        """Size of what is registered, which is not the same as what is on disk.

        #90: this counted the registry while reading like a directory listing, and the two
        disagreed by however many files a run writes without `Bundle.record`. It said
        "141 artefacts" beside a directory holding 144. The wording now names the registry,
        because the difference is deliberate and one file wide: `timings.json` is a sidecar
        (see `TIMINGS_NAME` in `pipeline.py`) and is not registered.
        """
        if not self.artefacts:
            return "bundle empty"
        total = 0
        for path in self.artefacts.values():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return f"{len(self.artefacts)} artefacts registered, {total / 1_048_576:.1f} MiB"

    def export_to_vault(
        self,
        vault: Path,
        *,
        subdir: str | None = None,
        images: bool = False,
        force: bool = False,
    ) -> list[Path]:
        """Copy finished files into the vault, verifying each one arrived.

        A copy, never a move: the bundle stays the single writer, and a failed export
        leaves an intact bundle rather than a half-consumed one.

        `subdir` is not decoration. A lecture bundle is ~138 artefacts, 23 of which land in
        the bundle root and 115 in `images/`, so exporting with `subdir=None` scatters ~138
        files into the vault root -- which is the directory holding the user's own notes. The
        CLI therefore always passes a subdir; this default is the
        fallback for direct callers, and it is a directory of its own for the same reason.

        `images` defaults to **False**, and the frames are 84% of the bytes. Measured on
        lecture 1 exported to the vault: 59 MiB total, of which 50 MiB was `images/` and
        9.5 MiB everything else. And the note refers to no frame at all -- zero `![[...]]`
        embeds, zero frame filenames, only time ranges like `### Фрагмент 1 (0–414 с)`.

        So by default the export was copying 50 MiB per lecture into the notes directory,
        which an indexer or a sync tool would pick up, for files nothing reads. The
        frames are the pipeline's evidence and they stay in the bundle, which is where the
        audit and a later re-run both look for them. `--vault-images` copies them too, for
        someone who wants the frames beside the note.
        """
        # #118: the default is one directory per lecture. A shared `dyak/` made every
        # lecture write `dyak/note.md`, so the second export silently replaced the first.
        subdir = subdir or f"{DEFAULT_EXPORT_SUBDIR}/{self.root.name}"
        root = Path(vault).expanduser() / subdir
        ledger_path = self.root / EXPORT_LEDGER
        ledger = _read_ledger(ledger_path)
        copied: list[Path] = []
        for key, source in sorted(self.artefacts.items()):
            if not source.is_file():
                continue
            is_image = source.parent.name == IMAGES
            if is_image and not images:
                continue
            target_dir = root / IMAGES if is_image else root
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            # #118: a vault file edited by hand since our last export is not ours to replace.
            # The ledger holds the digest we wrote; a different digest on disk means an edit.
            recorded = ledger.get(str(target))
            if (
                not force
                and target.is_file()
                and recorded is not None
                and _digest(target) != recorded
            ):
                raise OSError(
                    f"refusing to overwrite {target}: it was edited after the last export "
                    "(pass --force to replace it)"
                )
            shutil.copy2(str(source), str(target))
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                raise OSError(
                    f"export of {key} did not verify: {target} is missing or a different size"
                )
            ledger[str(target)] = _digest(target)
            copied.append(target)
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
        return copied
