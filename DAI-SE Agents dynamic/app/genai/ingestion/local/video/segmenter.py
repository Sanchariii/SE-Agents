"""Split long videos into time segments (ffmpeg stream-copy, lossless)."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config.constants import VideoConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@dataclass
class Segment:
    path: Path
    start_offset: float          # seconds from the start of the original video


def probe_duration(video_path: Path) -> float:
    """Return video duration in seconds via ffprobe; returns 0.0 on failure."""
    log.info("probing video duration: %s", video_path.name)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("ffprobe failed for %s (rc=%s): %s",
                    video_path.name, result.returncode, result.stderr[-300:].strip())
        return 0.0
    raw = result.stdout.strip()
    if not raw:
        log.warning("ffprobe returned empty duration for %s", video_path.name)
        return 0.0
    try:
        duration = float(raw)
        log.info("video duration: %.1f s (%s)", duration, video_path.name)
        return duration
    except (TypeError, ValueError):
        log.warning("ffprobe returned non-numeric duration %r for %s", raw, video_path.name)
        return 0.0


def segment_video(video_path: Path, out_dir: Path) -> list[Segment]:
    """Split a video into fixed-length segments using ffmpeg stream-copy."""
    seg_len = VideoConstants.SEGMENT_LENGTH_SEC
    suffix = video_path.suffix or ".mp4"
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    log.info("segmenting video %s into %ss clips", video_path.name, seg_len)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_time", str(seg_len),
         "-reset_timestamps", "1", str(seg_dir / f"seg_%03d{suffix}")],
        capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg segmentation failed for %s (rc=%s): %s",
                  video_path.name, result.returncode, result.stderr[-500:].strip())
        raise RuntimeError(f"ffmpeg segmentation failed: {result.stderr[-300:]}")

    segments: list[Segment] = []
    offset = 0.0
    for path in sorted(seg_dir.glob(f"seg_*{suffix}")):
        segments.append(Segment(path=path, start_offset=offset))
        offset += probe_duration(path)          # keyframe cuts only approximate seg_len
    log.info("video segmented into %s clip(s)", len(segments))
    return segments