"""Manually send one local video through the production Azure Video Indexer extractor.

Run from the repository root:
    python -m scripts.manual_testing.test_ai_video_indexer path/to/video.mp4
"""

import argparse
import mimetypes
import os
from pathlib import Path
from uuid import uuid4

from app.config.constants import VideoIndexerConstants
from app.config.settings import get_settings
from app.core.models.document_model import DocumentRecord
from app.genai.ingestion.azure.video_indexer import VideoIndexerExtractor
from app.utilities import tls_util as tls
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse the local video path supplied to the manual test.

    Returns:
        Parsed command-line arguments containing the video path.
    """
    parser = argparse.ArgumentParser(description="Test Azure Video Indexer with one local video.")
    parser.add_argument("video", type=Path, help="Path to an MP4, MOV, AVI, or WMV file.")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for this local test only.",
    )
    return parser.parse_args()


def make_record(video_path: Path, mime_type: str) -> DocumentRecord:
    """Build the minimal document metadata required by the extractor.

    Args:
        video_path: Video file being tested.
        mime_type: Detected media type for the video.

    Returns:
        A record representing the local video.
    """
    return DocumentRecord(
        session_id="manual-video-indexer",
        doc_id=str(uuid4()),
        version=1,
        filename=video_path.name,
        mime_type=mime_type,
        sha256="manual-test",
        storage_path=str(video_path),
    )


def main() -> None:
    """Upload a video, wait for indexing, and print the extracted-unit summary.

    Raises:
        FileNotFoundError: If the supplied video does not exist.
        ValueError: If the video MIME type is unsupported.
        RuntimeError: If Video Indexer configuration or processing fails.
    """
    args = parse_args()
    if args.insecure:
        # This local-only opt-out isolates proxy certificate problems from Indexer testing.
        os.environ["TLS_VERIFY"] = "false"
        get_settings.cache_clear()
        tls.reset()
        log.warning("TLS verification disabled for manual Video Indexer test")

    video_path = args.video.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    mime_type, _ = mimetypes.guess_type(video_path.name)
    if mime_type not in VideoIndexerConstants.MIME_TYPES:
        raise ValueError(
            f"Unsupported video type {mime_type!r}. "
            f"Supported types: {', '.join(VideoIndexerConstants.MIME_TYPES)}")

    # Use the same extractor and Settings-backed credentials as the ingestion pipeline.
    record = make_record(video_path, mime_type)
    print("starting manual Video Indexer test")
    units = VideoIndexerExtractor().extract(video_path.read_bytes(), record)

    transcript_count = sum(unit.kind == "transcript" for unit in units)
    ocr_count = sum(unit.kind == "frame_caption" for unit in units)
    print(f"Indexed {video_path.name}: {len(units)} extracted unit(s)")
    print(f"Transcript: {transcript_count}; on-screen text: {ocr_count}")
    for unit in units[:5]:
        print(f"[{unit.kind}] {unit.section}: {unit.text[:160]}")
    log.info("manual Video Indexer test completed", extra={"unit_count": len(units)})


if __name__ == "__main__":
    main()