"""Stage 5: crop to the document when there is one, then gate on what the source encodes.

Three things changed after the first version of this stage was measured on real frames. All
three are recorded here because the previous docstring asserted things that turned out to be
wrong, and a stage that documents its own numbers is the only thing standing between this
pipeline and another confident mistake.

## The metric is MEGE, not Laplacian variance

Laplacian variance was measuring content density. Five lecture-1 frames whose *per-edge*
Laplacian sharpness is constant (0.036-0.045) were selected, so their stroke sharpness is
equal by construction:

| frame | edge pixels | Laplacian variance |
|---|---|---|
| f_0000000000 | 8 183 | 294.8 |
| f_0004514556 | 22 692 | 1 021.0 |
| f_0003799751 | 42 514 | 1 741.7 |
| f_0004075763 | 43 181 | 1 927.4 |
| f_0002825607 | 48 860 | 2 124.4 |

**7.21x spread at equal stroke sharpness.** Pearson r(edge count, variance) = 0.841. A
threshold on that number separates slides by how much text is on them, not by whether it is
readable.

The replacement is MEGE, the mean of `Gx^2 + Gy^2` over pixels above a noise floor. The edge
population divides density out: ten times the text gives ten times the edge pixels and ten
times the gradient sum, so the mean is unchanged. Measured over the same five frames, MEGE
spreads **1.66x** where Laplacian variance spreads 7.21x. It is not a perfect fix -- stroke
*width* still moves the value, because a 72 pt header and 12 px body text have genuinely
different gradient slopes at identical focus -- but it is a 4.3x reduction, and the residue is
content rather than focus.

## The gate reads the frame before this stage touches it

Same argument as before, and it still holds: the post-enhancement number is a property of this
stage's parameters and of the encoder, not only of the image. Order is
crop -> measure (the gate) -> upscale -> unsharp -> measure again (recorded, never gated).

Through the written artefact the enhancement amplification is modest -- x1.04 for a 2x
lanczos upscale, x1.24 with unsharp, x1.38 cropped and sharpened. Piping the same chain to
`rawvideo` instead gives **x12.1**, because DCT quantisation at q=2 removes most of the
energy the upscale created. That gap is the reason this module refuses to be measured through
a proxy.

## Stage 4 hands over PNG, and it matters less than it looks

Re-extracting every lecture-1 frame as lossless PNG and measuring both through the real
analysis path gives a Laplacian variance delta of **-1.02% to -1.53%, mean -1.26%**. The
640-wide rescale that preceded the measurement destroys block-edge energy before the
Laplacian operator sees it, so the predicted "compression artifacts dominate the metric"
failure is real in mechanism and roughly one percent in magnitude. Stage 4 writes PNG anyway
because a known signed bias does not belong in a gate, but this is hygiene, not a rescue.

## The threshold is derived from the decay curve, not from a gap in a sample

The old threshold of 300 was picked from the gap between 298 and 1014 across 16 frames of one
lecture. Blur injection says where the boundary belongs: MEGE falls to **half** its
unblurred value at sigma 0.8-1.0 on every one of the 16 frames, tightly. So the gate is
placed at `decay * median(MEGE over the run's own frames)`, with `decay = 0.43` taken from that
sigma=1.0 half-value point.

Taking the *minimum* across frames instead, at a fixed sigma, was proposed and rejected by
measurement: it evaluates to 24 025, and all 16 frames score above it, so the gate would pass
everything. A minimum is the wrong order statistic for a gate -- it is dragged down by the
worst frame you happened to capture.

The normalisation is what makes the number portable. An absolute MEGE threshold would not be:
MEGE depends on stroke width, contrast and resolution, so it is not a unit that transfers
between lecture series. The ratio does.

## A frame is never rejected for not being a white page

The previous version returned `passed=False` whenever no bright region was found, which turned
a layout heuristic into a quality gate. It threw away two frames scoring 85 580 and 91 629 --
middle of the passing distribution, ~200 000 edge pixels each -- because their canvas is dark.
Dark-mode Beamer themes, MATLAB canvases, terminals and blackboards are valid content for
this lecture.

Now the outcome is three orthogonal measurements rather than one verdict:

- `quality_gate`: focus only, the thing this stage exists to decide.
- `content_type`: `white_document` | `dark_canvas` | `unknown`.
- `crop_state`: `cropped` | `full_frame_fallback`.

and a bbox that cannot be found falls back to the full frame. A frame that is genuinely blank
fails for that reason -- `f_0000304132` has luma std 10.2 and a 99th percentile of 45, and no
sharpness number is meaningful there -- which is a different failure from failing for blur.

## What this stage cannot fix

The source is VP8 at 1.6 Mbps. Interpolation adds no information. Small on-screen text is
already gone and cannot be recovered by resampling or sharpening -- only made more
plausible-looking. A frame below the threshold is reported with its measured value, not
silently dropped and not silently passed through.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from . import parallel

REPORT_NAME = "quality.json"
PROVENANCE_NAME = "quality-provenance.json"

#: Written when a bbox source is configured but not usable. The frame still gets measured.
CROP_CROPPED = "cropped"
CROP_FALLBACK = "full_frame_fallback"

CONTENT_WHITE = "white_document"
CONTENT_DARK = "dark_canvas"
CONTENT_UNKNOWN = "unknown"

REASON_BLANK = "EMPTY_CANVAS"
REASON_NO_EDGES = "NO_EDGES"
REASON_SOFT = "SOFT"


class NotConfiguredError(RuntimeError):
    """A stage dependency exists as an interface but has no configured implementation.

    Distinct from a missing binary: the interface is present, the credentials or endpoint
    are not. Callers map this to exit 2 rather than pretending the stage does not exist.
    """


@runtime_checkable
class BBoxEstimator(Protocol):
    """Locates the document pane in a frame.

    Returning `None` is not an error. It means "no document pane here", and the caller
    measures the full frame. An implementation that raises on a dark slide is wrong.
    """

    name: str

    def estimate(self, source: Path, settings: Settings) -> Box | None: ...


#: Stage 4 hands over PNG. This stage writes JPEG, and the suffix says so: ffmpeg picks the
#: muxer from the extension, so `-q:v 2` into a `.png` path is silently discarded and you get
#: a PNG with a lossy flag that did nothing.
ENHANCED_SUFFIX = ".jpg"


@dataclass(frozen=True)
class Settings:
    """Every number here is visible in the report, so nothing is a hidden constant."""

    #: Noise floor for the edge mask, on `Gx^2 + Gy^2`. With the 3x3 Sobel used here a step
    #: of one grey level gives 16 and two levels gives 64, so 30 sits between them: below
    #: sensor and codec noise, above nothing that carries signal.
    tau: float = 30.0
    #: Fraction of pixels allowed to participate in an edge response before the ratio is
    #: meaningful. A fraction, not a count, for the same reason the raster is native: an
    #: absolute edge count means something different on a 640x360 crop and a 1600x900 frame.
    #: Measured on lecture 1: content frames 11.3%-31.0% of pixels, the blank frame 2.9%.
    min_edge_fraction: float = 0.05
    #: Threshold as a fraction of the run's median MEGE. Derived from the measured sigma=1.0
    #: half-value point, not chosen for a separation on one lecture.
    decay: float = 0.43
    #: Floor on the derived threshold, so a run that is mostly blank cannot normalise its
    #: own way to "everything passes".
    floor: float = 20_000.0
    #: A frame this dark with this little contrast carries nothing to gate on.
    blank_std: float = 20.0
    blank_p99: int = 64
    dark_luma: float = 128.0
    #: Enhancement, applied after the measurement and never gated on.
    scale: int = 2
    unsharp_amount: float = 1.0
    unsharp_radius: int = 5
    #: Geometric bbox detection.
    grid: int = 96
    bright: int = 128
    min_region_fraction: float = 0.10
    margin: float = 0.02


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class Observation:
    """Everything measurable about a frame, before any pass or fail is attached."""

    name: str
    source: Path
    box: Box | None
    crop_state: str
    content_type: str
    bbox_source: str
    #: MEGE, the gate's input.
    mege: float
    edges: int
    luma_mean: float
    luma_std: float
    luma_p99: int
    width: int
    height: int


@dataclass
class FrameQuality:
    name: str
    source: Path
    passed: bool
    #: The gate. Measured before any enhancement, on the crop or the full frame.
    mege: float
    threshold: float
    crop_state: str
    content_type: str
    bbox_source: str
    edges: int
    luma_mean: float
    luma_std: float
    box: Box | None = None
    #: Recorded, never gated on: includes this stage's own unsharp mask.
    mege_enhanced: float | None = None
    reason: str = ""

    @property
    def fatal(self) -> bool:
        """Whether this frame failing costs the run its exit 0.

        A `dark_canvas` is an uninformative screen -- measured at luma_mean 35 on lecture
        1 -- and on real recordings it is almost always the projection before the lecturer
        gets a slide up. Rejecting it discards nothing, so it is measured, reported as
        SOFT, and left out of the exit decision.

        A `white_document` or `unknown` frame failing is different in kind: that is
        material the pipeline could not read, and it is a real loss. Gating on all frames
        equally made `process` unable to return 0 on nearly any recording, because nearly
        every recording has one dark frame at the head.
        """
        return self.content_type != CONTENT_DARK


@dataclass
class Report:
    frames: list[FrameQuality] = field(default_factory=list)
    threshold: float = 0.0

    @property
    def passed(self) -> list[FrameQuality]:
        return [f for f in self.frames if f.passed]

    @property
    def failed(self) -> list[FrameQuality]:
        return [f for f in self.frames if not f.passed]

    @property
    def failed_fatal(self) -> list[FrameQuality]:
        """Failures that cost the run its exit 0. See `FrameQuality.fatal`."""
        return [f for f in self.frames if not f.passed and f.fatal]

    @property
    def ok(self) -> bool:
        return not self.failed_fatal and bool(self.frames)

    def summary(self) -> str:
        if not self.frames:
            return "no frames measured"
        values = [f.mege for f in self.frames]
        cropped = sum(1 for f in self.frames if f.crop_state == CROP_CROPPED)
        soft = len(self.failed) - len(self.failed_fatal)
        tail = f", {soft} dark-canvas not fatal" if soft else ""
        return (
            f"{len(self.passed)}/{len(self.frames)} frames pass "
            f"(MEGE {min(values):.0f}-{max(values):.0f}, threshold {self.threshold:.0f}, "
            f"{cropped} cropped{tail})"
        )

    def explain(self) -> str:
        lines = []
        for f in self.failed:
            lines.append(
                f"  {f.name}: MEGE {f.mege:.0f} < {f.threshold:.0f}, {f.crop_state}, "
                f"{f.content_type} ({f.reason})"
            )
        return "\n".join(lines)


def _run_ffmpeg(args: list[str], *, source: Path, timeout: float = 120.0) -> None:
    """Run ffmpeg, or explain why it could not. Never returns a partial file as success."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"ffmpeg timed out after {timeout}s on {source.name}") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        raise OSError(f"ffmpeg exited {done.returncode} on {source.name}: {tail[0]}")


def _raw_gray(source: Path, width: int, height: int, *, pre: str = "") -> bytes:
    """Decode one frame to a raw 8-bit grayscale raster of an exact size.

    `pre` is applied *before* the scale, and that ordering is load-bearing: a crop applied
    after `scale=W:H` changes the raster's aspect, so the output is no longer WxH and the
    byte count no longer matches.
    """
    chain = f"scale={width}:{height}:flags=area,format=gray"
    if pre:
        chain = f"{pre},{chain}"
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            chain,
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if raw.returncode != 0 or len(raw.stdout) != width * height:
        raise OSError(
            f"ffmpeg did not produce a {width}x{height} raster from {source.name} "
            f"(got {len(raw.stdout)} bytes)"
        )
    return raw.stdout


def sobel_mege(raster: bytes, width: int, height: int, tau: float) -> tuple[float, int]:
    """MEGE and the edge population behind it.

    `Gx` and `Gy` are the standard 3x3 Sobel responses, edge-padded so the border does not
    darken the result. Returns the mean of `Gx^2 + Gy^2` over pixels at or above `tau`, and
    how many pixels those are.

    The count is returned alongside the mean on purpose: a metric whose value tracks the
    number of edges it consumed is a metric reading content density, and the count is what
    makes that visible to a test rather than to whoever reads the report.
    """
    if width < 3 or height < 3:
        return 0.0, 0
    a = np.frombuffer(raster, dtype=np.uint8).reshape(height, width).astype(np.float64)
    pad = np.pad(a, 1, mode="edge")
    gx = (
        pad[:-2, 2:]
        + 2 * pad[1:-1, 2:]
        + pad[2:, 2:]
        - pad[:-2, :-2]
        - 2 * pad[1:-1, :-2]
        - pad[2:, :-2]
    )
    gy = (
        pad[2:, :-2]
        + 2 * pad[2:, 1:-1]
        + pad[2:, 2:]
        - pad[:-2, :-2]
        - 2 * pad[:-2, 1:-1]
        - pad[:-2, 2:]
    )
    mag = gx * gx + gy * gy
    mask = mag >= tau
    count = int(mask.sum())
    if count == 0:
        return 0.0, 0
    return float(mag[mask].mean()), count


def _dimensions(source: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    parts = (out.stdout or "").strip().split("x")
    if out.returncode != 0 or len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise OSError(f"cannot read the frame size of {source.name}")
    width, height = int(parts[0]), int(parts[1])
    # ffprobe reports 0x0 for a file it cannot decode rather than failing, and the crop
    # arithmetic below divides by these. A zero must surface here as the unusable input it
    # is, not as a ZeroDivisionError three frames later.
    if width <= 0 or height <= 0:
        raise OSError(f"{source.name} decoded to {width}x{height}")
    return width, height


@dataclass(frozen=True)
class GeometricEstimator:
    """The largest bright region, by area, on a coarse grid.

    Row/column occupancy rather than connected components: at a 96-cell grid the document
    pane is a solid block and a union-find over 9 216 cells buys nothing. Unioning the
    qualifying rows and columns separately also makes the result tolerant of a single dark
    band inside the document, which a largest-component search would split on.

    ADR-0001 D4 assigns this to a VLM. The vision client stage 6 now uses (#67) exists, but
    it captions; it does not produce boxes, so the geometric substitute stands until a bbox
    source is written. A plausible-looking box from a stub would be worse than none: stage 6
    would caption the wrong region and the error would be invisible. `name` lands in the
    report so a real VLM source is distinguishable in provenance rather than silently
    assumed.
    """

    name: str = "geometric"

    def estimate(self, source: Path, settings: Settings) -> Box | None:
        width, height = _dimensions(source)
        grid = settings.grid
        raster = _raw_gray(source, grid, max(1, round(grid * height / width)))

        gx = grid
        gy = len(raster) // grid
        rows = [False] * gy
        cols = [False] * gx
        lit = 0
        for y in range(gy):
            base = y * grid
            for x in range(grid):
                if raster[base + x] >= settings.bright:
                    rows[y] = True
                    cols[x] = True
                    lit += 1

        if lit == 0:
            return None
        ys = [i for i, v in enumerate(rows) if v]
        xs = [i for i, v in enumerate(cols) if v]
        if not ys or not xs:
            return None

        # Fraction of the frame the box covers, in grid cells rather than pixels: the box is
        # quantised to the grid anyway, so measuring it any finer is self-deception.
        coverage = (len(ys) * len(xs)) / float(grid * gy)
        if coverage < settings.min_region_fraction:
            return None

        mx = max(1, round(settings.margin * grid))
        my = max(1, round(settings.margin * gy))
        x0 = max(0, xs[0] - mx)
        x1 = min(grid, xs[-1] + 1 + mx)
        y0 = max(0, ys[0] - my)
        y1 = min(gy, ys[-1] + 1 + my)

        sx = width / grid
        sy = height / gy
        return Box(
            x=round(x0 * sx),
            y=round(y0 * sy),
            width=max(1, round((x1 - x0) * sx)),
            height=max(1, round((y1 - y0) * sy)),
        )


@dataclass(frozen=True)
class NullEstimator:
    """Never crops. Measures the whole frame.

    Not a stub: for a lecture that is screen-to-screen with no document pane, measuring the
    full frame is the correct answer and the crop would only throw content away.
    """

    name: str = "null"

    def estimate(self, source: Path, settings: Settings) -> Box | None:
        return None


#: `vlm` was registered here until stage 6 gained a vision client (#67). It raised
#: `NotConfiguredError` on both branches and was never reachable by a run that worked, so
#: it was a name that promised a capability and could not deliver one: `DYAK_BBOX_SOURCE=vlm`
#: failed at the crop instead of being rejected up front as the unknown name it is.
#:
#: A vision bbox source is still the right idea -- ADR-0001 D4 asked for one, and
#: `GeometricEstimator` documents at length why it is the substitute. It is not built, and
#: an unbuilt option should be absent from the registry rather than present and broken.
ESTIMATORS: dict[str, BBoxEstimator] = {
    "geometric": GeometricEstimator(),
    "null": NullEstimator(),
}


def resolve_estimator(name: str) -> BBoxEstimator:
    try:
        return ESTIMATORS[name]
    except KeyError:
        raise KeyError(
            f"unknown bbox_source {name!r}; choose one of {sorted(ESTIMATORS)}"
        ) from None


def _analysis_raster(source: Path, box: Box | None) -> tuple[bytes, int, int]:
    """The raster the gate reads: the crop if there is one, else the whole frame.

    At 1:1. The previous version scaled every crop to a fixed 640-wide raster, which was
    defended as "constant raster so the threshold is comparable". It is comparable, but not
    because of that: scaling 1920->640 and 960->640 apply 3x and 1.5x different anti-aliasing,
    so two crops get different low-pass treatment and their scores are not the same
    measurement. The three crops in lecture 1 have scale factors 0.457, 0.480 and 0.582.
    Measuring natively removes the discrepancy instead of standardising around it.
    """
    if box is None:
        width, height = _dimensions(source)
        return _raw_gray(source, width, height), width, height
    raster = _raw_gray(
        source,
        box.width,
        box.height,
        pre=f"crop={box.width}:{box.height}:{box.x}:{box.y}",
    )
    return raster, box.width, box.height


def observe(source: Path, settings: Settings, estimator: BBoxEstimator) -> Observation:
    """Measure one frame. No verdict, no enhancement, no side effects."""
    box = estimator.estimate(source, settings)
    raster, width, height = _analysis_raster(source, box)
    mege, edges = sobel_mege(raster, width, height, settings.tau)

    pixels = np.frombuffer(raster, dtype=np.uint8)
    mean = float(pixels.mean())
    std = float(pixels.std())
    p99 = int(np.percentile(pixels, 99))

    if box is not None:
        crop_state = CROP_CROPPED
    else:
        crop_state = CROP_FALLBACK
    if mean >= settings.dark_luma:
        content = CONTENT_WHITE
    elif box is None:
        content = CONTENT_DARK
    else:
        content = CONTENT_UNKNOWN

    return Observation(
        name=source.name,
        source=source,
        box=box,
        crop_state=crop_state,
        content_type=content,
        bbox_source=estimator.name,
        mege=mege,
        edges=edges,
        luma_mean=mean,
        luma_std=std,
        luma_p99=p99,
        width=width,
        height=height,
    )


def is_blank(observation: Observation, settings: Settings) -> bool:
    """Nothing to gate on: too little contrast and too dark a top percentile to be content.

    A separate predicate from the threshold because a blank canvas is not a soft one. No
    sharpness number is meaningful on it, and reporting it as "soft" hides which failure
    occurred.
    """
    return observation.luma_std < settings.blank_std and observation.luma_p99 < settings.blank_p99


def has_edges(observation: Observation, settings: Settings) -> bool:
    """Enough of the raster is participating in edges for MEGE to mean something."""
    return observation.edges >= settings.min_edge_fraction * observation.width * observation.height


def threshold_for(observations: list[Observation], settings: Settings) -> float:
    """The gate, placed on the run's own decay curve rather than a fixed constant.

    `decay * median(MEGE)` over frames that carry content. Blank canvases are excluded: they
    would drag the median down and loosen the gate for everything else. The absolute floor
    then stops a run that is mostly blank from normalising its way to "everything passes".

    A minimum across frames, at a fixed blur, was the alternative and it was measured: at
    sigma 1.2 it evaluates to 24 025, and all 16 lecture-1 frames score above it, so the gate
    would admit everything. A minimum is dragged down by the worst frame you happened to
    capture.
    """
    usable = [o.mege for o in observations if has_edges(o, settings) and not is_blank(o, settings)]
    if not usable:
        return settings.floor
    usable.sort()
    middle = len(usable) // 2
    if len(usable) % 2:
        median = usable[middle]
    else:
        median = (usable[middle - 1] + usable[middle]) / 2.0
    return max(settings.floor, settings.decay * median)


def enhanced_name(frame_name: str) -> str:
    return f"{Path(frame_name).stem}_q{ENHANCED_SUFFIX}"


def _enhance(source: Path, box: Box | None, destination: Path, settings: Settings) -> None:
    # Assembled as a list and joined once. Building this by appending to a string is how
    # "flags=lanczosunsharp=5:5:1.0" happens: the branch that supplies the separator is
    # exactly the branch that does not run when there is no crop, and the failure only
    # appears on full-frame fallbacks.
    parts = []
    if box is not None:
        parts.append(f"crop={box.width}:{box.height}:{box.x}:{box.y}")
    parts.append(f"scale=iw*{settings.scale}:ih*{settings.scale}:flags=lanczos")
    parts.append(
        f"unsharp={settings.unsharp_radius}:{settings.unsharp_radius}:"
        f"{settings.unsharp_amount}:{settings.unsharp_radius}:"
        f"{settings.unsharp_radius}:0.0"
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            ",".join(parts),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(destination),
        ],
        source=source,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise OSError(f"ffmpeg reported success but wrote no file for {source.name}")


def evaluate(
    observation: Observation,
    enhanced_dir: Path,
    settings: Settings,
    threshold: float,
) -> FrameQuality:
    """Attach a verdict to a measurement, then enhance. Never raises for a soft frame."""
    if is_blank(observation, settings):
        passed, reason = False, REASON_BLANK
    elif not has_edges(observation, settings):
        passed, reason = False, REASON_NO_EDGES
    else:
        passed = observation.mege >= threshold
        reason = "" if passed else REASON_SOFT

    mege_enhanced = None
    if passed:
        enhanced = enhanced_dir / enhanced_name(observation.name)
        _enhance(observation.source, observation.box, enhanced, settings)
        enhanced_raster, ew, eh = _analysis_raster(enhanced, None)
        mege_enhanced, _ = sobel_mege(enhanced_raster, ew, eh, settings.tau)

    return FrameQuality(
        name=observation.name,
        source=observation.source,
        passed=passed,
        mege=observation.mege,
        threshold=threshold,
        crop_state=observation.crop_state,
        content_type=observation.content_type,
        bbox_source=observation.bbox_source,
        edges=observation.edges,
        luma_mean=observation.luma_mean,
        luma_std=observation.luma_std,
        box=observation.box,
        mege_enhanced=mege_enhanced,
        reason=reason,
    )


def run(
    frames: list[Path],
    work_dir: Path,
    *,
    settings: Settings | None = None,
    bbox_source: str = "geometric",
    stream=None,
) -> Report:
    """Measure every frame, derive the gate, then decide. A bad frame is a failed frame, not a crash.

    Two passes, deliberately. The threshold is a function of the run, so it cannot be known
    until every frame has been measured; deciding as we went would gate the first frames
    against a threshold derived from fewer frames than the last.
    """
    settings = settings or Settings()
    estimator = resolve_estimator(bbox_source)
    out = stream if stream is not None else sys.stdout
    enhanced_dir = work_dir / "enhanced"
    enhanced_dir.mkdir(parents=True, exist_ok=True)

    observations: list[Observation] = []
    errors: dict[str, str] = {}
    began = time.monotonic()
    jobs = parallel.ffmpeg_jobs()

    def measure(source: Path) -> Observation | OSError:
        try:
            return observe(source, settings, estimator)
        except OSError as exc:
            return exc

    # Each frame is measured by its own ffmpeg calls and nothing else, so the pass fans out
    # (#355). Results come back in frame order; the threshold still waits for all of them.
    for source, measured in zip(frames, parallel.map_ordered(measure, frames, jobs)):
        if isinstance(measured, OSError):
            errors[source.name] = str(measured)
        else:
            observations.append(measured)

    threshold = threshold_for(observations, settings)

    by_name = {o.name: o for o in observations}

    def judge(observation: Observation) -> FrameQuality | OSError:
        try:
            return evaluate(observation, enhanced_dir, settings, threshold)
        except OSError as exc:
            return exc

    # The enhance pass is per-frame ffmpeg too, and now that the threshold is known it is
    # independent per frame as well.
    verdicts = dict(
        zip(
            by_name,
            parallel.map_ordered(judge, list(by_name.values()), jobs),
        )
    )

    report = Report(threshold=threshold)
    for source in frames:
        if source.name in errors:
            report.frames.append(
                FrameQuality(
                    name=source.name,
                    source=source,
                    passed=False,
                    mege=0.0,
                    threshold=threshold,
                    crop_state=CROP_FALLBACK,
                    content_type=CONTENT_UNKNOWN,
                    bbox_source=estimator.name,
                    edges=0,
                    luma_mean=0.0,
                    luma_std=0.0,
                    reason=errors[source.name],
                )
            )
            continue
        observation = by_name[source.name]
        verdict = verdicts[source.name]
        if isinstance(verdict, FrameQuality):
            report.frames.append(verdict)
        else:
            exc = verdict
            report.frames.append(
                FrameQuality(
                    name=source.name,
                    source=source,
                    passed=False,
                    mege=observation.mege,
                    threshold=threshold,
                    crop_state=observation.crop_state,
                    content_type=observation.content_type,
                    bbox_source=estimator.name,
                    edges=observation.edges,
                    luma_mean=observation.luma_mean,
                    luma_std=observation.luma_std,
                    box=observation.box,
                    reason=str(exc),
                )
            )

    fallbacks = sum(1 for f in report.frames if f.crop_state == CROP_FALLBACK)
    if report.failed:
        for line in report.explain().splitlines():
            out.write(f"         {line.strip()}\n")
    out.write(
        f"         gate = {settings.decay:g} x median MEGE over content frames; "
        f"{fallbacks} frame(s) measured full-frame\n"
        f"         bbox_source={estimator.name}; gated on the source, "
        f"enhancement after ({time.monotonic() - began:.1f}s)\n"
    )
    return report


def write(report: Report, target: Path, settings: Settings) -> None:
    target.write_text(
        json.dumps(
            {
                "threshold": report.threshold,
                "threshold_rule": f"{settings.decay} x median(MEGE over content frames)",
                "tau": settings.tau,
                "min_edge_fraction": settings.min_edge_fraction,
                "scale": settings.scale,
                "unsharp_amount": settings.unsharp_amount,
                "frames": [
                    {
                        "name": f.name,
                        # The three orthogonal measurements, not one verdict.
                        "quality_gate": "PASS" if f.passed else "FAIL",
                        "content_type": f.content_type,
                        "crop_state": f.crop_state,
                        "bbox_source": f.bbox_source,
                        # The gate's input. Measured before any enhancement, at native 1:1.
                        "mege": round(f.mege, 1),
                        "threshold": round(f.threshold, 1),
                        "edges": f.edges,
                        "luma_mean": round(f.luma_mean, 1),
                        "luma_std": round(f.luma_std, 1),
                        # Recorded, never gated on: this includes this stage's own unsharp
                        # mask, so it cannot speak to the source.
                        "mege_enhanced": (round(f.mege_enhanced, 1) if f.mege_enhanced else None),
                        "bbox": f.box.as_dict() if f.box else None,
                        "reason": f.reason,
                    }
                    for f in report.frames
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def provenance(settings: Settings, bbox_source: str = "geometric") -> str:
    return json.dumps(
        {
            "metric": "MEGE",
            "metric_definition": "mean(Gx^2 + Gy^2) over pixels with Gx^2 + Gy^2 >= tau",
            "tau": settings.tau,
            "raster": "native 1:1, crop or full frame, never rescaled",
            "threshold_rule": f"{settings.decay} x median(MEGE over content frames)",
            "threshold_floor": settings.floor,
            "min_edge_fraction": settings.min_edge_fraction,
            "blank_std": settings.blank_std,
            "blank_p99": settings.blank_p99,
            "bbox_source": bbox_source,
            "grid": settings.grid,
            "bright": settings.bright,
            "margin": settings.margin,
            "scale": settings.scale,
            "unsharp": f"{settings.unsharp_radius}:{settings.unsharp_amount}",
            "gate_reads": "source, before enhancement, at native resolution",
        },
        indent=2,
    )
