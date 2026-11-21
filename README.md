# Shreyansh Codes AI YouTube Automation Studio — v2

Faceless Hinglish YouTube production studio powered by **Google Gemini**, **ElevenLabs**, **Replicate**, **FFmpeg**, **FastAPI**, **React**, and **n8n**.

> **Note on Video Rendering:** The current renderer (`renderer_mode: "still_image_slideshow"`) assembles AI-generated still images with voiceover, transitions, and burned-in subtitles. A `MotionVideoProvider` interface is defined in `backend/app/providers.py` for future motion-video integration.

## Security & Reliability Highlights
- **Auth, RBAC, CSRF & Rate Limiting**: Salted `scrypt` password hashing, HMAC-SHA256 JWT/cookie sessions, CSRF double-submit protection, user-isolated jobs/assets, and sliding-window rate limits.
- **Durable Database-Backed Worker Queue**: Atomic job claiming (`BEGIN IMMEDIATE`), worker leases/heartbeats, crash recovery, and provider concurrency semaphores.
- **Fail-Closed Voice & Replicate Guards**: Mandatory non-empty `ELEVENLABS_AUTHORIZED_VOICE_IDS` + consent flag; HTTPS-only `api.replicate.com` polling URL validation.
- **Derived Synthetic Disclosure & Structured Provenance**: `ClaimProvenance` verification gates public scheduling while allowing private previews; `containsSyntheticMedia` is derived from assets and only marked confirmed when YouTube API confirms it.

## Quick Start
- Backend: `uvicorn app.main:app --reload --app-dir backend` (visit `http://localhost:8000/docs`)
- Standalone Worker (optional): `python -m app.worker` (from `backend/`)
- Frontend: `cd frontend && npm install && npm run dev` (visit `http://localhost:5173`)
- Tests: `python -m pytest backend/tests -v`
- Docs: [DEPLOYMENT.md](docs/DEPLOYMENT.md), [SECURITY_CHECKLIST.md](docs/SECURITY_CHECKLIST.md), [VIDEO_PIPELINE.md](docs/VIDEO_PIPELINE.md)
