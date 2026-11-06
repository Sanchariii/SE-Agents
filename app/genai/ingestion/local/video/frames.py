"""Frame pipeline: extract -> scene change -> dedupe -> score -> keep best.

Pure OpenCV/numpy, no LLM calls. Frame objects carry timestamp + score
metadata for the captioner and blob upload steps.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.config.constants import VideoConstants
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@dataclass
class Frame:
    path: Path
    timestamp: float
    score: float = 0.0
    scene_index: int = 0
    dhash: int = field(default=0, repr=False)


def extract_frames(video_path: Path, out_dir: Path) -> list[Frame]:
    """Extract one JPEG frame every FRAME_INTERVAL_SEC from the video."""
    interval = VideoConstants.FRAME_INTERVAL_SEC
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    log.info("extracting frames from %s (1 frame every %ss)", video_path.name, interval)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vf", f"fps=1/{interval}",
         "-q:v", "3", str(frames_dir / "f_%05d.jpg")],
        capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg frame extraction failed for %s (rc=%s): %s",
                  video_path.name, result.returncode, result.stderr[-500:].strip())
        raise RuntimeError(f"ffmpeg frame extraction failed: {result.stderr[-300:]}")
    frames = [Frame(path=p, timestamp=(i - 1) * interval)
              for i, p in enumerate(sorted(frames_dir.glob("f_*.jpg")), start=1)]
    log.info("%s raw frame(s) extracted from %s", len(frames), video_path.name)
    return frames


def _hist(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def mark_scenes(frames: list[Frame]) -> list[Frame]:
    """Assign a scene index to each frame based on histogram correlation."""
    threshold = VideoConstants.SCENE_THRESHOLD
    scene, prev = 0, None
    for frame in frames:
        hist = _hist(cv2.imread(str(frame.path)))
        if prev is not None and cv2.compareHist(prev, hist, cv2.HISTCMP_CORREL) < (1.0 - threshold):
            scene += 1
        frame.scene_index = scene
        prev = hist
    log.info("scene detection complete: %s frame(s) across %s scene(s)",
             len(frames), scene + 1 if frames else 0)
    return frames


def _dhash(img: np.ndarray, size: int = 8) -> int:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (size + 1, size))
    diff = resized[:, 1:] > resized[:, :-1]
    return int("".join("1" if b else "0" for b in diff.flatten()), 2)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe(frames: list[Frame]) -> list[Frame]:
    """Remove near-duplicate frames using perceptual hashing (dHash)."""
    max_dist = VideoConstants.DEDUPE_HAMMING
    kept: list[Frame] = []
    for frame in frames:
        frame.dhash = _dhash(cv2.imread(str(frame.path)))
        if any(_hamming(frame.dhash, k.dhash) <= max_dist for k in kept):
            continue
        kept.append(frame)
    log.info("deduplication: %s → %s frame(s) (removed %s near-duplicates)",
             len(frames), len(kept), len(frames) - len(kept))
    return kept


def score(frames: list[Frame]) -> list[Frame]:
    for frame in frames:
        gray = cv2.cvtColor(cv2.imread(str(frame.path)), cv2.COLOR_BGR2GRAY)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
        exposure = 1.0 - abs(gray.mean() - 127.5) / 127.5
        frame.score = float(sharpness * max(exposure, 0.05))
    return frames


def select_best(frames: list[Frame]) -> list[Frame]:
    """Pick the sharpest frame per scene, then fill up to MAX_FRAMES by score."""
    cap = VideoConstants.MAX_FRAMES
    by_scene: dict[int, list[Frame]] = {}
    for f in frames:
        by_scene.setdefault(f.scene_index, []).append(f)
    selected = [max(group, key=lambda f: f.score) for group in by_scene.values()]
    if len(selected) < cap:
        rest = sorted((f for f in frames if f not in selected),
                      key=lambda f: f.score, reverse=True)
        selected.extend(rest[: cap - len(selected)])
    result = sorted(selected, key=lambda f: f.timestamp)[:cap]
    log.info("selected %s best keyframe(s) from %s candidate(s) (cap=%s)",
             len(result), len(frames), cap)
    return result


def run_frame_pipeline(video_path: Path, workdir: Path) -> list[Frame]:
    frames = extract_frames(video_path, workdir)
    if not frames:
        return []
    return select_best(score(dedupe(mark_scenes(frames))))