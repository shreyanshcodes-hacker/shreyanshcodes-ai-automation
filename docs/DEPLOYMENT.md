# Deployment, Worker, OAuth & Troubleshooting Guide

## Prerequisites
- Python 3.11+
- Node.js 20+ (for React Studio UI)
- FFmpeg & FFprobe installed and on `PATH`
- Optional: n8n for scheduled workflows

## 1. Backend Setup
```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env
# Set APP_ENV=production, a >=32-char AUTH_SECRET, HTTPS FRONTEND_ORIGINS,
# GEMINI_API_KEY, ELEVENLABS_* (with ELEVENLABS_VOICE_CONSENT_VERIFIED=true and non-empty ELEVENLABS_AUTHORIZED_VOICE_IDS),
# REPLICATE_*, and YOUTUBE_* (with HTTPS YOUTUBE_REDIRECT_URI).
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2. Background Worker Setup
The FastAPI app runs an embedded durable database-backed `WorkerManager` loop (`studio.db` with atomic `BEGIN IMMEDIATE` job claiming and lease heartbeats). For dedicated multi-worker deployments, run:
```bash
cd backend
python -m app.worker
```
Interrupted jobs whose worker lease expires are automatically recovered on restart.

## 3. Frontend Setup
```bash
cd frontend
npm install
npm run build   # or `npm run dev` for local development on :5173
```

## 4. YouTube OAuth Setup
1. Register an OAuth 2.0 Web Client in Google Cloud Console with `YOUTUBE_REDIRECT_URI` (HTTPS in production).
2. Sign in as the `owner` user in the Studio UI (the first registered user is assigned `owner`).
3. Call `GET /api/youtube/oauth/url` (authenticated) and open the returned URL. A single-use, session-bound `state` token is stored for 10 minutes.
4. On callback (`/api/youtube/oauth/callback`), tokens are saved server-side to `YOUTUBE_TOKEN_FILE` (`0600`).
5. Keep `YOUTUBE_PUBLIC_SCHEDULING_ENABLED=false` until private uploads and QA checks are verified.

## 5. Troubleshooting
- **Startup fails with `AUTH_SECRET` or `HTTPS` error**: In `APP_ENV=production`, `validate_production_settings()` enforces a >=32-char `AUTH_SECRET`, `ALLOW_DEV_AUTH_BYPASS=false`, HTTPS `FRONTEND_ORIGINS`, and HTTPS `YOUTUBE_REDIRECT_URI`.
- **Voiceover returns 403**: Ensure `ELEVENLABS_VOICE_CONSENT_VERIFIED=true` and `ELEVENLABS_AUTHORIZED_VOICE_IDS` is non-empty and includes the target voice ID.
- **FFmpeg errors**: Ensure `ffmpeg` and `ffprobe` are installed and on `PATH`.
