"""Stage 4 — frames: two-pass extraction of distinct on-screen states (ADR-0001 D3).

The screen of a lecture changes slowly; the cursor moves constantly. Sampling every N
minutes yields ~99% duplicates, and scoring a raw frame for change catches the cursor
— tens of frames per second. Hence the split:

  1. `detect()` — full decode at reduced width, blur, then a single `select` carrying both
     selection conditions. The blur here is **only** a detector pre-filter.
  2. `extract_at()` — seek the **original source** per timestamp, emit one frame.

The blur physically cannot reach the output files, because pass 1 writes only timestamps
into a temp dir that is deleted immediately. That is the bug this design exists to prevent:
an earlier version blurred on the way *out*, and every emitted frame was mush — measured
Laplacian variance 1–3.6 against 686–3483 for the correct path.

Ported from `lecture-frames` (294 lines, previously untracked in `~/.local/bin`). It is a
move, not a rewrite: the working code was the only place the ADR-0001 D3 bug fixes existed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import exitcodes as ec
from . import runner

REMEDIATION = "install: pacman -S ffmpeg"

# On this host ffmpeg fails to open an existing, readable webm in roughly 1 run in 5, and an
# immediate retry succeeds. Cause not established; retried rather than diagnosed.
RETRIES = 4
RETRY_BACKOFF = 1.0

# Stage 4 decodes the whole container, so it gets a much larger budget than DEFAULT_TIMEOUT
# (120 s). Lecture 1 is 4396 s and takes minutes, not seconds.
DETECT_TIMEOUT = 60 * 60.0
EXTRACT_TIMEOUT = 60.0

MANIFEST = "manifest.tsv"
MANIFEST_HEADER = "pts_ms\ttime\tfile"
FRAME_PREFIX = "f_"
#: Output frames are lossless. They are the input to stage 5's measurement, and a q=2 JPEG
#: encode biases the Laplacian variance by -1.26% (mean over 16 frames, range -1.02% to
#: -1.53%). Small, but a gate should not carry a known signed bias. The detector pass in
#: `detect()` stays JPEG: those frames are throwaway change-detection rasters.
FRAME_SUFFIX = ".png"


@dataclass(frozen=True)
class Settings:
    """Detector and encoder knobs. Defaults are the measured-good ones from the port."""

    mode: str = "dedupe"  # "dedupe" | "interval"
    every: float = 30.0  # seconds, interval mode only
    max_gap: float = 180.0  # guarantee a frame at least this often, 0 disables
    blur: int = 16  # boxblur radius at 640 px, 0 disables
    detect_width: int = 640  # detector width; does not affect output
    scene: float = 0.3  # `select`'s scene score threshold, 0 disables dedup
    width: int = 1600  # output frame width


def detect_filter(settings: Settings) -> str:
    """The detector filter chain. Applied only in `detect()`, never to output frames.

    The two selection conditions live in **one** `select` expression, and that is the whole
    point of this function (#70). It used to be `mpdecimate` followed by `select`: a frame
    had to survive `mpdecimate` before `select` could apply the `max_gap` guarantee to it,
    and `mpdecimate` drops every frame of a static stretch. Nine minutes of unchanged slide
    therefore produced zero frames for nine minutes, against a field documented as
    "guarantee a frame at least this often" -- measured worst gap 580.9 s on lecture 1.

    Reordering is not the fix either: `select` then `mpdecimate` measured 1080 s, because
    `mpdecimate` then discards exactly the frames `select` guaranteed. ORing `scene` into
    the same expression means neither condition can suppress the other.
    """
    parts = [f"scale={settings.detect_width}:-2:flags=lanczos"]
    if settings.mode == "interval":
        parts.append(f"fps=1/{settings.every:g}")
        return ",".join(parts)

    # A blur before `scene` so the score reacts to layout changes, not to video noise. It
    # is a scene-detection aid here, not a dedup aid.
    blur = max(1, round(settings.blur * settings.detect_width / 640)) if settings.blur else 0
    if blur:
        parts.append(f"boxblur={blur}:{max(1, blur // 4)}")

    terms = ["isnan(prev_selected_t)"]
    if settings.max_gap:
        # `isnan(prev_selected_t)` is not optional. prev_selected_t initialises to NaN,
        # `t - NaN` is NaN, `gte(NaN, GAP)` is false -- so without it **zero** frames are
        # selected, and ffmpeg does not fail: it writes an empty file and exits 0.
        terms.append(f"gte(t-prev_selected_t\\,{settings.max_gap:g})")
    if settings.scene:
        terms.append(f"gt(scene\\,{settings.scene:g})")
    parts.append("select='" + "+".join(terms) + "'")
    return ",".join(parts)


def hhmmss(ms: int) -> str:
    seconds, remainder = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{remainder // 100:02d}"


def _run_ffmpeg(args: list[str], *, timeout: float, source: Path) -> None:
    """`runner.run`, with a retry for the host's intermittent input-open failure.

    Only the "Error opening input" class is retried; any other failure is raised at once,
    because a real error retried four times is just four times slower.

    The source's existence is re-checked between attempts. The ported script retried
    unconditionally, and today that masked a different failure entirely: ffmpeg reported
    `Error opening input` five times in a row because the source's **parent directory had
    been renamed**, and the script's own error message then blamed ffmpeg for what was
    really a missing input. A retry must not convert "your path is wrong" into "ffmpeg is
    flaky".
    """
    last: runner.DependencyError | None = None
    for attempt in range(1, RETRIES + 1):
        if not source.is_file():
            raise runner.DependencyError(
                name="ffmpeg",
                code=ec.USAGE,
                message=f"input does not exist: {source}",
                remediation="check the path; a parent directory may have been renamed",
                argv=args,
            )
        try:
            runner.run("ffmpeg", args, remediation=REMEDIATION, timeout=timeout)
            return
        except runner.DependencyError as exc:
            stderr = exc.stderr.decode("utf-8", "replace")
            last_line = stderr.strip().splitlines()[-1] if stderr.strip() else ""
            if "Error opening input" not in last_line:
                raise
            last = exc
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    assert last is not None
    raise runner.DependencyError(
        name="ffmpeg",
        code=ec.DEPENDENCY_FAILED,
        message=(
            f"ffmpeg could not open an existing, readable input after {RETRIES} attempts; "
            "the source was verified present before each attempt"
        ),
        stderr=last.stderr,
        remediation="this is a host-level ffmpeg fault, not a missing file; retry the run",
        argv=last.argv,
    )


def detect(source: Path, settings: Settings = Settings()) -> list[int]:
    """Pass 1: full decode, timestamps only. Returns PTS in milliseconds."""
    tmp = Path(tempfile.mkdtemp(prefix="dyak-detect-"))
    try:
        _run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(Path(source).resolve()),
                "-an",
                "-sn",
                "-vf",
                detect_filter(settings),
                "-fps_mode",
                "passthrough",
                # `-frame_pts 1` writes the PTS **in the output timebase**. After
                # `-fps_mode passthrough` that is the input's frame rate -- 1/30 for a
                # 30 fps lecture -- so without this flag the filename is a frame *index* and
                # every number below is 30x too small. Pass 2 then seeks to index/1000
                # seconds and every frame comes from the first thirtieth of the lecture.
                # Measured: last file `f_0000135437` (135.4 s) as shipped, `d_0004514567`
                # (4514.6 s) with this flag. #70.
                "-enc_time_base",
                "1/1000",
                "-q:v",
                "6",
                "-frame_pts",
                "1",
                str(tmp / "d_%010d.jpg"),
            ],
            timeout=DETECT_TIMEOUT,
            source=Path(source),
        )
        return [int(p.stem.split("_")[1]) for p in sorted(tmp.glob("d_*.jpg"))]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract_at(source: Path, pts_ms: int, out: Path, settings: Settings = Settings()) -> Path:
    """Pass 2: seek the original source, emit one frame with no detector processing."""
    dest = out / f"{FRAME_PREFIX}{pts_ms:010d}{FRAME_SUFFIX}"
    _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{pts_ms / 1000:.3f}",
            "-i",
            str(Path(source).resolve()),
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-vf",
            f"scale={settings.width}:-2:flags=lanczos",
            # PNG cannot hold a YUV plane; the decoder must hand ffmpeg RGB to encode it.
            "-pix_fmt",
            "rgb24",
            str(dest),
        ],
        timeout=EXTRACT_TIMEOUT,
        source=Path(source),
    )
    return dest


def read_manifest(manifest: Path) -> list[int]:
    """Timestamps from an existing manifest. Empty rows and the header are skipped."""
    if not manifest.is_file():
        return []
    return [
        int(row.split("\t")[0])
        for row in manifest.read_text(encoding="utf-8").splitlines()[1:]
        if row.strip() and not row.startswith("#")
    ]


def provenance(out: Path, settings: Settings, reused: bool) -> None:
    """Record what produced this frame set.

    Found the hard way: a manifest written on 2026-10-02 lists 17 states while the same
    ffmpeg on the same source lists 16, with only 9 in common. Same binary, same input,
    28 hours apart — so the difference was the *settings*, not the environment. Without a
    record of them, such a manifest is not reproducible and cannot be used as a golden
    file, and nobody can tell later whether the frames or the code changed.
    """
    payload = {
        "filter": detect_filter(settings),
        "settings": asdict(settings),
        "reused_manifest": reused,
        "ffmpeg": ffmpeg_version(),
    }
    (out / "detect.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def ffmpeg_version() -> str:
    try:
        raw = runner.run("ffmpeg", ["-version"], remediation=REMEDIATION, timeout=30.0)
        return raw.decode("utf-8", "replace").splitlines()[0]
    except Exception:
        return "unknown"


def write_manifest(rows: list[tuple[int, Path]], manifest: Path) -> None:
    manifest.write_text(
        MANIFEST_HEADER + "\n" + "".join(f"{p}\t{hhmmss(p)}\t{f.name}\n" for p, f in rows),
        encoding="utf-8",
    )


def run(
    source: Path,
    out: Path,
    *,
    settings: Settings = Settings(),
    reuse_manifest: bool = False,
) -> tuple[Path, list[Path]]:
    """Run both passes. Returns `(manifest, frames)`.

    ADR-0001 D3: a run that emits zero frames is a **failure**, not a success with nothing in it.
    The ported script wrote a header-only manifest and exited 0 — which is how a
    27-minute extraction produced nothing and still reported success. The artefact count is
    checked against the directory, not against the exit code.
    """
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / MANIFEST

    if reuse_manifest and manifest.is_file():
        times = read_manifest(manifest)
        reused = True
    else:
        times = detect(source, settings)
        reused = False

    if not times:
        where = f" from {manifest.name}" if reused else ""
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=f"frame detection produced no states{where}",
            remediation=(
                "no on-screen change was detected in the video; the source may be a static "
                "image or a screen recording with no visible changes"
            ),
        )

    for old in out.glob(f"{FRAME_PREFIX}*{FRAME_SUFFIX}"):
        old.unlink()

    rows = [(pts, extract_at(source, pts, out, settings)) for pts in times]
    write_manifest(rows, manifest)
    provenance(out, settings, reused)

    # ADR-0001 D3 again, one level deeper: the extract pass can also exit 0 having written nothing.
    missing = [p for _, p in rows if not p.is_file() or p.stat().st_size == 0]
    if missing:
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=f"{len(missing)} of {len(rows)} frames were not written to disk",
            remediation="the source may be truncated or unreadable past the last timestamp",
            stderr="\n".join(str(p) for p in missing).encode(),
        )

    return manifest, [p for _, p in rows]


def summary(manifest: Path, frames: list[Path]) -> str:
    """One line for the stage report."""
    size = sum(p.stat().st_size for p in frames) / 1_048_576
    gaps = []
    times = read_manifest(manifest)
    gaps = [(times[i] - times[i - 1]) / 1000 for i in range(1, len(times))]
    spacing = f", median gap {sorted(gaps)[len(gaps) // 2]:.0f}s" if gaps else ""
    return f"{len(frames)} frames, {size:.1f} MiB{spacing}"
