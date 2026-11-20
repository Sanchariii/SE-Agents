"""Video -> keyframes(caption+blob) + audio->transcript -> ExtractedUnits.

Audio transcription is currently disabled (see _process_clip); only the keyframe
caption path is active. The audio imports/code are kept for future re-integration.
"""

import json
import tempfile
from pathlib import Path

from app.config.constants import VideoConstants
from app.core.models.document_model import DocumentRecord, ExtractedUnit
# Audio transcription disabled for now — kept for future integration.
# from app.genai.ingestion.local.video.audio import extract_audio, transcribe
from app.genai.ingestion.local.video.captioner import caption_frame
from app.genai.ingestion.local.video.frames import run_frame_pipeline
from app.genai.ingestion.local.video.segmenter import Segment, probe_duration, segment_video
from app.utilities import storage_util as storage
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _base_dir(record: DocumentRecord) -> str:
    return record.storage_path.rsplit("/", 1)[0]


def _process_clip(video_path: Path, workdir: Path, record: DocumentRecord,
                  offset: float, frame_start: int,
                  units: list[ExtractedUnit], manifest: list[dict]) -> int:
    """Process one video clip: run the keyframe pipeline (audio is disabled).

    Args:
        video_path: path to the clip file.
        workdir: scratch directory for this clip's intermediates.
        record: the originating DocumentRecord (for storage paths).
        offset: start time of this clip in the original video (seconds).
        frame_start: frame index offset so numbering is global across all clips.
        units: accumulated ExtractedUnit list to append to.
        manifest: accumulated frame manifest list to append to.

    Returns:
        Number of keyframes processed in this clip.
    """
    log.info("processing clip %s (offset=%.1fs, frame_start=%s)",
             video_path.name, offset, frame_start)

    # --- audio transcription (DISABLED) ---
    # Whisper transcription (local + Azure) is temporarily turned off: neither backend
    # is reliable in this environment, and a failure here must not block the rest of
    # ingestion. Frames/captions still run. Re-enable for future audio integration.
    log.info("audio transcription is disabled; skipping audio for %s", video_path.name)
    # audio_path = extract_audio(video_path, workdir)
    # if audio_path is not None:
    #     try:
    #         transcript_segs = transcribe(audio_path)
    #         log.info("transcript: %s segment(s) from %s", len(transcript_segs), video_path.name)
    #         for seg in transcript_segs:
    #             start, end = seg["start"] + offset, seg["end"] + offset
    #             units.append(ExtractedUnit(
    #                 text=seg["text"], section=f"transcript {_ts(start)}-{_ts(end)}",
    #                 kind="transcript", meta={"start_sec": start, "end_sec": end}))
    #     except Exception as exc:                    # noqa: BLE001
    #         log.warning("transcription failed for %s: %s", video_path.name, exc, exc_info=True)
    # else:
    #     log.info("no audio extracted from %s; skipping transcription", video_path.name)

    # --- frame pipeline (extract → scene → dedupe → score → select) ---
    log.info("running frame pipeline for %s", video_path.name)
    frames = run_frame_pipeline(video_path, workdir)
    log.info("%s keyframe(s) selected for captioning from %s", len(frames), video_path.name)

    for j, frame in enumerate(frames):
        idx = frame_start + j + 1
        timestamp = frame.timestamp + offset
        frame_rel = (f"{_base_dir(record)}/frames/"
                     f"frame_{idx:03d}_{_ts(timestamp).replace(':', '')}.jpg")
        log.info("saving keyframe %s of %s (t=%.1fs) to blob", idx, frame_start + len(frames), timestamp)
        storage.save_bytes(frame_rel, frame.path.read_bytes())
        # caption_frame handles its own errors and returns "" on failure
        caption = caption_frame(frame.path)
        meta = {"frame_path": frame_rel, "timestamp_sec": timestamp,
                "timestamp": _ts(timestamp), "scene": frame.scene_index,
                "score": round(frame.score, 2), "visual_description": caption}
        manifest.append({**meta, "caption": caption})
        if caption:
            units.append(ExtractedUnit(text=caption, section=f"keyframe @ {_ts(timestamp)}",
                                       kind="frame_caption", meta=meta))

    log.info("clip %s done: %s captioned frame(s) (audio disabled)",
             video_path.name,
             sum(1 for u in units if u.kind == "frame_caption"))
    return len(frames)


def extract_video(data: bytes, record: DocumentRecord) -> list[ExtractedUnit]:
    """Full video extraction pipeline: segment → keyframe captions.

    Audio transcription is currently disabled (see _process_clip); only captioned
    keyframes are produced. Kept structured so audio can be re-enabled later.

    Args:
        data: raw video bytes (already downloaded from blob).
        record: DocumentRecord used for storage path construction and logging context.

    Returns:
        List of ExtractedUnits (captioned keyframes).
    """
    units: list[ExtractedUnit] = []
    manifest: list[dict] = []
    size_mb = len(data) / (1024 * 1024)
    log.info("starting video extraction for %s (%.1f MB, doc_id=%s)",
             record.filename, size_mb, record.doc_id)

    with tempfile.TemporaryDirectory(prefix="video_") as tmp:
        workdir = Path(tmp)
        video_path = workdir / record.filename
        log.info("writing video bytes to temp file: %s", video_path)
        video_path.write_bytes(data)

        seg_len = VideoConstants.SEGMENT_LENGTH_SEC
        duration = probe_duration(video_path)
        if duration and duration > seg_len:
            log.info("video is %.1fs > segment length %ss; splitting into clips",
                     duration, seg_len)
            segments = segment_video(video_path, workdir)
        else:
            log.info("video is %.1fs (<= %ss); processing as single clip",
                     duration, seg_len)
            segments = [Segment(path=video_path, start_offset=0.0)]

        log.info("processing %s clip(s) for %s", len(segments), record.filename)
        frame_count = 0
        for i, segment in enumerate(segments):
            log.info("clip %s/%s: %s (offset=%.1fs)",
                     i + 1, len(segments), segment.path.name, segment.start_offset)
            clipdir = workdir / f"clip_{i:03d}"
            clipdir.mkdir(exist_ok=True)
            frame_count += _process_clip(segment.path, clipdir, record,
                                         segment.start_offset, frame_count, units, manifest)

        if manifest:
            log.info("saving frame manifest (%s entries) to blob", len(manifest))
            storage.save_bytes(
                f"{_base_dir(record)}/frames/manifest.json",
                json.dumps({"doc_id": record.doc_id, "version": record.version,
                            "filename": record.filename, "frames": manifest},
                           indent=2).encode())

    n_frames = sum(1 for u in units if u.kind == "frame_caption")
    log.info("video extraction complete for %s: %s clip(s), %s captioned keyframe(s), "
             "%s total unit(s) (audio transcription disabled)",
             record.filename, len(segments), n_frames, len(units))
    return units