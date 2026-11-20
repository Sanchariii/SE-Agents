"""Audio track -> transcript (Azure Whisper by default, faster-whisper if local)."""

import subprocess
from pathlib import Path

from app.config.constants import VideoConstants
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def extract_audio(video_path: Path, out_dir: Path) -> Path | None:
    """Extract mono WAV from video; returns None if the track is absent or too short."""
    audio_path = out_dir / "audio.wav"
    log.info("extracting audio from %s", video_path.name)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1",
         "-ar", str(VideoConstants.AUDIO_SAMPLE_RATE), "-f", "wav", str(audio_path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("ffmpeg audio extraction failed for %s (rc=%s): %s",
                    video_path.name, result.returncode, result.stderr[-300:].strip())
        return None
    if not audio_path.exists() or audio_path.stat().st_size < VideoConstants.MIN_AUDIO_BYTES:
        log.warning("no usable audio track in %s (file missing or too small)",
                    video_path.name)
        return None
    size_kb = audio_path.stat().st_size // 1024
    log.info("audio extracted from %s (%s KB)", video_path.name, size_kb)
    return audio_path


def transcribe(audio_path: Path) -> list[dict]:
    """Transcribe audio via Azure Whisper or local faster-whisper, per settings."""
    backend = get_settings().whisper_backend
    log.info("transcribing audio (%s) using backend=%s", audio_path.name, backend)
    if backend == VideoConstants.WHISPER_BACKEND_LOCAL:
        return _transcribe_local(audio_path)
    # Azure Whisper (default for hosted)
    return _transcribe_azure(audio_path)


def _transcribe_azure(audio_path: Path) -> list[dict]:
    """Transcribe using Azure OpenAI Whisper; returns timestamped segments."""
    from app.utilities.clients_util import get_aoai_client
    s = get_settings()
    deployment = s.azure_openai_whisper_deployment
    log.info("calling Azure Whisper deployment=%s for %s", deployment, audio_path.name)
    with audio_path.open("rb") as f:
        result = get_aoai_client().audio.transcriptions.create(
            model=deployment, file=f,
            response_format="verbose_json", timestamp_granularities=["segment"])
    segments = getattr(result, "segments", None) or []
    parsed = [{"start": float(s_.start), "end": float(s_.end), "text": s_.text.strip()}
              for s_ in segments if s_.text.strip()]
    if parsed:
        log.info("Azure Whisper returned %s segment(s) for %s", len(parsed), audio_path.name)
        return parsed
    text = getattr(result, "text", "").strip()
    if text:
        log.info("Azure Whisper returned flat text (%s chars) for %s", len(text), audio_path.name)
        return [{"start": 0.0, "end": 0.0, "text": text}]
    log.warning("Azure Whisper returned no transcript for %s", audio_path.name)
    return []


def _transcribe_local(audio_path: Path) -> list[dict]:
    """Transcribe using faster-whisper on CPU; loads model on first call (can be slow)."""
    from faster_whisper import WhisperModel
    model_name = get_settings().whisper_local_model
    log.info("loading local Whisper model=%s (CPU int8) — may take a moment", model_name)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    log.info("transcribing %s with local Whisper", audio_path.name)
    segments_iter, info = model.transcribe(str(audio_path))
    log.info("detected language: %s (probability %.2f) for %s",
             info.language, info.language_probability, audio_path.name)
    results = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
               for s in segments_iter if s.text.strip()]
    log.info("local Whisper returned %s segment(s) for %s", len(results), audio_path.name)
    return results