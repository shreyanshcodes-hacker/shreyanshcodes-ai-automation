# Deployment Guide — Shreyansh Codes AI YouTube Automation Studio

## Prerequisites
- Python 3.11+
- Node.js 20+ (for React Studio UI)
- FFmpeg & FFprobe installed and on `PATH`
- Optional: n8n (self-hosted or cloud) for scheduled workflows

## 1. Backend Setup
```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env
# Fill in GEMINI_API_KEY, ELEVENLABS_*, REPLICATE_*, and YOUTUBE_* credentials
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2. Frontend Studio UI
```bash
cd frontend
npm install
npm run build   # or `npm run dev` for local development on :5173
```

## 3. n8n Schedules
Import the 5 workflow templates from `n8n/` into your n8n instance and set `STUDIO_API_URL`.

## 4. First-Time YouTube OAuth Connection
1. Start the backend.
2. Call `GET /api/youtube/oauth/url` and open the returned Google OAuth URL.
3. Authorize the channel; tokens are stored server-side at `YOUTUBE_TOKEN_FILE`.
4. Leave `YOUTUBE_PUBLIC_SCHEDULING_ENABLED=false` until you have verified private uploads and QA results.
