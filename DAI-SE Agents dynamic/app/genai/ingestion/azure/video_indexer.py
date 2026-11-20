"""Azure Video Indexer: upload -> poll -> transcript / on-screen-text units.

Replaces the local frame+Whisper pipeline for video MIME types when
USE_VIDEO_INDEXER is set (see app/genai/ingestion/registry.py).
"""

import time

from app.config.constants import VideoIndexerConstants as VI
from app.config.settings import get_settings
from app.core.models.document_model import DocumentRecord, ExtractedUnit
from app.utilities import tls_util as tls
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _location(region: str) -> str:
    """"Central US" -> "centralus": the API path takes the region without spaces."""
    return region.replace(" ", "").lower()


def _seconds(value: str) -> float:
    """Video Indexer timestamps are "h:mm:ss(.fff)"."""
    try:
        parts = [float(p) for p in str(value).split(":")]
    except ValueError:
        return 0.0
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _span(block: dict) -> tuple[float, float]:
    instance = (block.get("instances") or [{}])[0]
    return _seconds(instance.get("start", "")), _seconds(instance.get("end", ""))


def _to_units(payload: dict, video_id: str) -> list[ExtractedUnit]:
    insights = (payload.get("videos") or [{}])[0].get("insights") or {}
    base_meta = {"source": "video_indexer", "video_id": video_id}
    units: list[ExtractedUnit] = []

    for block in insights.get("transcript") or []:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        start, end = _span(block)
        units.append(ExtractedUnit(
            text=text, section=f"transcript {_ts(start)}-{_ts(end)}", kind="transcript",
            meta={**base_meta, "start_sec": start, "end_sec": end}))

    for block in insights.get("ocr") or []:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        start, _ = _span(block)
        units.append(ExtractedUnit(
            text=text, section=f"on-screen text @ {_ts(start)}", kind="frame_caption",
            meta={**base_meta, "timestamp_sec": start, "timestamp": _ts(start)}))

    labels = [k.get("text", "") for k in insights.get("keywords") or []]
    labels += [t.get("name") or t.get("text", "") for t in insights.get("topics") or []]
    labels = [label for label in labels if label]
    if labels:
        units.append(ExtractedUnit(
            text="Key topics: " + ", ".join(dict.fromkeys(labels)),
            section="video topics", kind="text", meta=dict(base_meta)))

    return units


class VideoIndexerExtractor:
    name = "video_indexer_extractor"
    mime_types = VI.MIME_TYPES

    def __init__(self) -> None:
        s = get_settings()
        self._account_id = s.video_indexer_account_id
        self._token = s.video_indexer_access_token
        location = _location(s.video_indexer_region)
        if not (self._account_id and self._token and location):
            raise RuntimeError(
                "VIDEO_INDEXER_ACCOUNT_ID, VIDEO_INDEXER_REGION and "
                "VIDEO_INDEXER_ACCESS_TOKEN must all be configured")
        self._base = f"{VI.API_BASE}/{location}/Accounts/{self._account_id}"

    def extract(self, data: bytes, record: DocumentRecord) -> list[ExtractedUnit]:
        video_id = self._upload(data, record)
        units = _to_units(self._wait_for_insights(video_id), video_id)
        log.info("video indexed", extra={
            "doc_id": record.doc_id, "video_id": video_id,
            "transcript_segments": sum(1 for u in units if u.kind == "transcript"),
            "ocr_segments": sum(1 for u in units if u.kind == "frame_caption")})
        return units

    def _upload(self, data: bytes, record: DocumentRecord) -> str:
        resp = tls.http_client().post(
            f"{self._base}/Videos",
            params={"name": record.filename[:80], "privacy": VI.PRIVACY,
                    "indexingPreset": VI.INDEXING_PRESET, "accessToken": self._token},
            files={"file": (record.filename, data, record.mime_type)},
            timeout=VI.UPLOAD_TIMEOUT_SECONDS)
        self._check(resp, "upload")
        video_id = resp.json().get("id")
        if not video_id:
            raise RuntimeError("Video Indexer upload returned no video id")
        return video_id

    def _wait_for_insights(self, video_id: str) -> dict:
        deadline = time.monotonic() + VI.POLL_TIMEOUT_SECONDS
        while True:
            resp = tls.http_client().get(f"{self._base}/Videos/{video_id}/Index",
                                         params={"accessToken": self._token})
            self._check(resp, "index")
            payload = resp.json()
            state = payload.get("state")
            if state == VI.STATE_PROCESSED:
                return payload
            if state == VI.STATE_FAILED:
                raise RuntimeError(f"Video Indexer failed to process video {video_id}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Video Indexer still '{state}' after "
                                   f"{VI.POLL_TIMEOUT_SECONDS:.0f}s (video {video_id})")
            time.sleep(VI.POLL_INTERVAL_SECONDS)

    @staticmethod
    def _check(resp, stage: str) -> None:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Video Indexer {stage} rejected the access token (HTTP {resp.status_code}). "
                "VIDEO_INDEXER_ACCESS_TOKEN is short-lived and must be refreshed")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Video Indexer {stage} failed (HTTP {resp.status_code}): {resp.text[:200]}")
