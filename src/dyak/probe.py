"""Stage 1: probe the input, and decide what the pipeline can do with it.

ffprobe is asked for the stream table and the container duration, and the answer
is reduced to the two facts the rest of the pipeline branches on:

  * is there an audio stream to transcribe at all;
  * is this a video source (so stage 4 has frames to extract) or audio-only.

The second fact has a consequence worth stating plainly. ADR-0001 lists
"Audio-only input path beyond what the video probe naturally produces" under *Not
doing*, so an audio-only input is **rejected** rather than quietly accepted: the
MVP supports video files, and an audio file handed to `process` is a usage error
that says so. Treating it as supported would mean promising stage 4's frames and
then having no source for them.

ffprobe exits 0 and prints JSON for a well-formed file; it exits non-zero with a
message on stderr for anything it cannot demux, and that message reaches the user
verbatim through `runner.run`. The only failure mode handled here rather than
there is ffprobe exiting 0 with unparseable output, which is a broken ffprobe and
is reported as a dependency failure.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import exitcodes as ec
from . import runner

REMEDIATION = "install: pacman -S ffmpeg"


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    # #94: the input's own fingerprint. Appended after the existing defaults so every
    # construction that predates them stays valid. `None` means this record cannot speak to
    # the value -- a hand-built `MediaInfo`, or a stat/hash that failed -- and `provenance`
    # omits the key rather than writing `null`, which would claim the run measured nothing.
    size_bytes: int | None = None
    sha256: str | None = None

    @property
    def video_only(self) -> bool:
        """A source with frames but no sound: nothing stage 2 or 3 can consume."""
        return self.has_video and not self.has_audio

    def summary(self) -> str:
        parts: list[str] = []
        if self.has_video:
            size = f"{self.width}x{self.height}" if self.width and self.height else "?"
            parts.append(f"video {self.video_codec or '?'} {size}")
        if self.has_audio:
            rate = f"{self.sample_rate}Hz" if self.sample_rate else "?"
            chans = f"{self.channels}ch" if self.channels else "?"
            parts.append(f"audio {self.audio_codec or '?'} {rate} {chans}")
        parts.append(f"{self.duration:.1f}s")
        return ", ".join(parts)


PROVENANCE_NAME = "source-provenance.json"


def provenance(info: MediaInfo, audio_artefact=None, *, ffmpeg_version: str = "") -> str:
    """What the input was, for the bundle. Covers stages 1 and 2.

    Both stages record only a run-log line today: `[1/12] probe 0.1s video av1 1920x1080,
    audio aac 48000Hz 2ch, 4520.1s`. Those are measurements of the *input*, not constants, and
    they change when the input does -- re-encoding the lecture moves `duration`, which moves
    every frame timestamp stage 4 extracts, which moves every word boundary stage 6 aligns.
    A bundle that cannot say what it was given cannot be reasoned about offline.

    One document rather than two. Neither stage has a parameter a user can vary, so
    `probe-provenance.json` and `audio-provenance.json` would each be a handful of facts about
    one input; that is a single fact. The `stages` list says which stage measured what.

    `ffmpeg_version` is here rather than left to `detect.json`, because stages 1 and 2 call
    ffprobe/ffmpeg before stage 4 does, and #70 was a filter-chain bug -- the chain and the
    binary that ran it belong together.

    #94 added the input's fingerprint: the absolute `path`, the `size_bytes` and the
    `sha256` of the file that was read. The issue's framing was that a bundle could prove
    "this note came from this transcript" (#79 hashes the transcript) but only *asserted*
    "this note came from this lecture" by filename. The two files for this lecture are the
    case: `1_lecture_OCS.mp4` and `1_lecture_OCS.webm` slugify to the same bundle and differ
    only in `duration`, so a re-encode at the same duration is invisible in the record
    without a size and a digest.

    Nothing verifies the record. The input is not in the bundle -- it can be moved, deleted,
    or live on a USB disk that is gone by the time anyone reads this -- so its digest can
    only be recorded, while stage 12 keeps verifying the bundle's own files by stat. The
    keys are therefore absent when unset rather than `null`: an absent key says this
    document cannot speak to the value, `null` would claim the run looked and found nothing.
    """
    payload: dict = {
        "stages": [1, 2],
        "tool": "dyak-probe-v1",
        "requires_model": False,
        "source": {
            "name": info.path.name,
            # Absolute, and never unresolved: `probe` resolves before it stats, so this is
            # the file the run opened rather than the string the user typed (#94).
            "path": str(info.path),
            "duration": round(info.duration, 1),
            "has_video": info.has_video,
            "video_codec": info.video_codec,
            "width": info.width,
            "height": info.height,
            "has_audio": info.has_audio,
            "audio_codec": info.audio_codec,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
        },
    }
    if info.size_bytes is not None:
        payload["source"]["size_bytes"] = info.size_bytes
    if info.sha256 is not None:
        payload["source"]["sha256"] = info.sha256
    if ffmpeg_version:
        payload["ffmpeg"] = ffmpeg_version
    if audio_artefact is not None:
        payload["extracted_audio"] = {
            "name": audio_artefact.path.name,
            "duration": round(audio_artefact.duration, 1),
            "sample_rate": audio_artefact.sample_rate,
            "channels": audio_artefact.channels,
            "size_bytes": audio_artefact.size_bytes,
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def probe(path: Path) -> MediaInfo:
    """Describe a media file. Raises runner.DependencyError if ffprobe fails."""
    # Resolved here rather than at the ffprobe argv below (#94): every message the checks
    # print and `info.path` itself must name the file the run actually opened, not the
    # string the user typed -- a relative path, a `~`, or a symlink into the library.
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise _no_such_file(src)
    if not src.is_file():
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.USAGE,
            message=f"input is not a file: {src}",
            remediation="pass a recording file, not a directory",
        )
    if not os_accessible(src):
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.USAGE,
            message=f"input is not readable: {src}",
            remediation="check the file's ownership and permissions",
        )

    # Resolved absolute path, because ffprobe has no `--` end-of-options marker:
    # a relative path beginning with "-" would be read as an option. Resolving
    # also normalises the path in every message the user sees afterwards.
    argv = [
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(src.resolve()),
    ]
    raw = runner.run("ffprobe", argv, remediation=REMEDIATION)

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.DEPENDENCY_FAILED,
            message=f"ffprobe printed output that is not JSON ({exc})",
            stderr=raw,
            remediation="ffprobe exited 0 with unparseable output; check the install",
        ) from exc

    # json.loads happily returns a list, a number, a string or null. Treating
    # those as the stream table raises AttributeError, which escapes as a
    # traceback with exit 1 -- so a broken ffprobe would be reported to the user
    # as a typo in their own command line.
    if not isinstance(data, dict):
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.DEPENDENCY_FAILED,
            message=f"ffprobe returned a JSON {type(data).__name__}, expected an object",
            stderr=raw,
            remediation="ffprobe exited 0 with an unexpected payload shape; check the install",
        )

    return _reduce(src, data)


def _reduce(src: Path, data: dict) -> MediaInfo:
    # A *present* key of the wrong type means ffprobe is broken. Silently
    # degrading it to "no streams" would make require_supported report "no audio
    # or video stream in your file", which blames the user's recording for the
    # tool's defect. Absent keys are fine: a container really can have no
    # stream list.
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.DEPENDENCY_FAILED,
            message=f"ffprobe returned `streams` as {type(streams).__name__}, expected a list",
            stderr=json.dumps(data).encode()[:2000],
            remediation="ffprobe exited 0 with an unexpected payload shape; check the install",
        )
    fmt = data.get("format", {})
    if not isinstance(fmt, dict):
        raise runner.DependencyError(
            name="ffprobe",
            code=ec.DEPENDENCY_FAILED,
            message=f"ffprobe returned `format` as {type(fmt).__name__}, expected an object",
            stderr=json.dumps(data).encode()[:2000],
            remediation="ffprobe exited 0 with an unexpected payload shape; check the install",
        )

    def pick(codec_type: str) -> dict | None:
        for item in streams:
            if isinstance(item, dict) and item.get("codec_type") == codec_type:
                return item
        return None

    video = pick("video")
    audio = pick("audio")

    duration = _number(fmt.get("duration"))
    if duration <= 0 and video is not None:
        # Some containers carry the duration on the stream, not the format.
        duration = _number(video.get("duration"))

    return MediaInfo(
        path=src,
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=(video or {}).get("codec_name"),
        width=_integer((video or {}).get("width")),
        height=_integer((video or {}).get("height")),
        audio_codec=(audio or {}).get("codec_name"),
        sample_rate=_integer((audio or {}).get("sample_rate")),
        channels=_integer((audio or {}).get("channels")),
        # Filled here rather than in `probe`, which is where the existence and readability
        # checks are (#94): an unreadable source stays the named DependencyError it already
        # is, and the hash is never attempted on a file that failed those checks. Both are
        # `None` if the stat or the read fails afterwards, which `provenance` records by
        # omitting the key -- a run whose media ffprobe just described is not a failed run.
        size_bytes=_size_of(src),
        sha256=_digest_of(src),
    )


def require_supported(info: MediaInfo) -> None:
    """Reject inputs the MVP does not handle, with the reason rather than a shrug.

    Usage errors (exit 1), not dependency failures: the dependencies worked, the
    input is what is wrong.
    """
    if not info.has_audio and not info.has_video:
        raise runner.DependencyError(
            name="dyak",
            code=ec.USAGE,
            message=f"no audio or video stream in {info.path}",
            remediation="pass a lecture recording, not a data file",
        )
    if not info.has_video:
        raise runner.DependencyError(
            name="dyak",
            code=ec.USAGE,
            message=(
                f"{info.path} is audio-only; the MVP pipeline expects a video source "
                "because stage 4 extracts frames from it (ADR-0001, Not doing)"
            ),
            remediation="pass the video recording of this lecture",
        )
    if not info.has_audio:
        raise runner.DependencyError(
            name="dyak",
            code=ec.USAGE,
            message=f"{info.path} has video but no audio stream; nothing to transcribe",
            remediation="pass a recording that contains the lecture audio",
        )


def _no_such_file(path: Path) -> runner.DependencyError:
    return runner.DependencyError(
        name="dyak",
        code=ec.USAGE,
        message=f"input does not exist: {path}",
        remediation="check the path; `dyak process --help` shows the expected form",
    )


def _number(value: object) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # ffprobe emits the literal string "NAN" for an unmeasurable duration.
    return out if out == out and out not in (float("inf"), float("-inf")) else 0.0


def _integer(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _size_of(path: Path) -> int | None:
    """The input's size, or `None` if it cannot be stat'd."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _digest_of(path: Path) -> str | None:
    """The input's sha256, or `None` if it cannot be read.

    Streamed rather than `read_bytes()`: the existing `_digest` helpers (`stt/core.py`,
    `bundle.py`) hash a transcript, where reading whole is nothing, while a lecture
    recording is hundreds of MB and can be tens of GB.
    """
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def os_accessible(path: Path) -> bool:
    return os.access(path, os.R_OK)
