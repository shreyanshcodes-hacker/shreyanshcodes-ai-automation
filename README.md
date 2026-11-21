# Shreyansh Codes AI YouTube Automation Studio — v2

Faceless Hinglish YouTube production studio powered by **Google Gemini**, **ElevenLabs**, **Replicate**, **FFmpeg**, **FastAPI**, **React**, and **n8n**.

## Implemented Capabilities
1. **Structured ScenePlan (`POST /api/scene-plan`)**: Strict Pydantic schema with Gemini structured output (`scene_index`, `narration`, `visual_prompt`, `caption_text`, `scene_duration`, `source_notes`, `synthetic_disclosure`).
2. **End-to-End Orchestration (`POST /api/production/orchestrate` & `POST /api/jobs`)**: Script/ScenePlan → Hinglish Voiceover → Scene Images → Upgraded FFmpeg Render → QA Gate.
3. **Replicate Image Adapter**: Model-schema adaptation (`flux`, `sdxl`, `imagen`, `prompt_only`) + MIME & dimension validation.
4. **Hinglish Voiceover Adapter**: Sentence-boundary chunking, 429/5xx exponential backoff, audio concatenation, Hinglish pronunciation normalization, and authorized-voice allowlist/consent enforcement.
5. **Upgraded FFmpeg Renderer**: Per-scene durations, `xfade` transitions, `.srt` subtitle timing & safe-margin burn-in, `loudnorm` audio normalization, 9:16 Shorts & 16:9 Long templates.
6. **Async Job Queue & Asset Deduplication**: SQLite persistence (`studio.db`) with progress state, retry/backoff, cooperative cancellation, and SHA-256 asset deduplication.
7. **Preview, Download & React Studio UI**: `frontend/` React + Vite dashboard for job creation, live progress, video preview, download, QA reports, and YouTube kill switch.
8. **Comprehensive QA (`POST /api/qa/check`)**: Dimensions, duration range, audio stream, black frames, loudness/clipping, subtitle safe margins, duplicate hash, factual-source & provenance checks.
9. **n8n Schedules (`n8n/`)**: Daily topic research, 2 Shorts/day, 2 Long/week, publishing scheduler, weekly analytics.
10. **YouTube OAuth 2.0 & Safety Gates**: Resumable upload, private-first upload, `publishAt` scheduling only after QA + owner opt-in, idempotency, quota-aware retry, `containsSyntheticMedia` disclosure, and kill switch.

## Quick Start
- Backend: `uvicorn app.main:app --reload --app-dir backend` (visit `http://localhost:8000/docs`)
- Frontend: `cd frontend && npm install && npm run dev` (visit `http://localhost:5173`)
- Tests: `pytest backend/tests`
- Docs: [DEPLOYMENT.md](docs/DEPLOYMENT.md), [SECURITY_CHECKLIST.md](docs/SECURITY_CHECKLIST.md), [VIDEO_PIPELINE.md](docs/VIDEO_PIPELINE.md)
