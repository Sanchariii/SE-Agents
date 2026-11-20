# Locally-processed workloads

Everything in this folder runs **inside the service** because the managed Azure
equivalent is not wired up yet. It is isolated here so the switch is a one-file change.

| Workload | Today (local)                                              | Planned Azure service   |
| -------- | ---------------------------------------------------------- | ----------------------- |
| Video    | `video/` — ffmpeg segmenting, OpenCV keyframes, Whisper ASR | Azure Video Indexer     |

## Operational cost of running locally

- Needs `ffmpeg` and OpenCV native libraries in the image (see `helm/Dockerfile`).
- CPU-bound and memory-hungry; runs in a worker thread, never on the event loop.
- Tuned by `VideoConstants` in `app/config/constants.py`; the Whisper backend and
  deployment come from `app/config/settings.py`.

## Migrating a workload to Azure

1. Add an extractor under `app/genai/ingestion/azure/` that implements
   `ExtractorTool` (`name`, `mime_types`, `extract`) and returns the same
   `ExtractedUnit` shapes — for video that is `transcript` and `frame_caption` units.
2. Register it in `_azure_tier()` in `app/genai/ingestion/registry.py`, gated on its
   endpoint setting so an unconfigured environment falls back here automatically.
3. Nothing else changes: the pipeline, chunking, indexing and deck writer all consume
   `ExtractedUnit`, not the extractor that produced it.
