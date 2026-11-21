"""FastAPI application for Shreyansh Codes AI YouTube Automation Studio."""
from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import db, orchestrator, production, qa, youtube
from .ai import generate_scene_plan, generate_text
from .config import settings
from .schemas import (
    ImageRequest,
    JobCreateRequest,
    KillSwitchRequest,
    OrchestrateRequest,
    QACheckRequest,
    RenderRequest,
    ScenePlanRequest,
    ScriptRequest,
    VisualRequest,
    VoiceRequest,
    YouTubeUploadRequest,
)

app = FastAPI(title="Shreyansh Codes AI YouTube Automation", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "publish_mode": settings.publish_mode,
        "kill_switch": youtube.is_kill_switch_enabled(),
    }


@app.get("/api/settings")
def get_settings():
    return {
        "presentation": "faceless",
        "voice": "AI-generated",
        "visuals": "AI-generated",
        "language": settings.default_language,
        "shorts_per_day": settings.shorts_per_day,
        "long_videos_per_week": settings.long_videos_per_week,
        "publish_mode": settings.publish_mode,
        "public_scheduling_enabled": settings.youtube_public_scheduling_enabled,
        "kill_switch": youtube.is_kill_switch_enabled(),
    }


@app.post("/api/script")
def script(req: ScriptRequest):
    duration = "30–60 seconds" if req.format == "short" else f"about {req.target_minutes} minutes"
    prompt = f"""Write an original {req.format} {req.language} YouTube script for Shreyansh Codes about {req.topic}.
Target {duration}. Faceless production: narration, numbered scenes, AI visual prompts,
on-screen Hinglish captions, practical example and CTA. Flag facts needing verification.
Avoid copied wording, invented facts and filler."""
    try:
        return {"script": generate_text(prompt)}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/scene-plan")
def scene_plan(req: ScenePlanRequest):
    try:
        plan = generate_scene_plan(
            topic=req.topic,
            fmt=req.format,
            language=req.language,
            target_minutes=req.target_minutes,
            scene_count=req.scene_count,
        )
        return plan.model_dump()
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/visual-prompt")
def visual_prompt(req: VisualRequest):
    try:
        return {"prompt": production.generate_visual_prompt(req.topic, req.scene, req.format)}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/voiceover")
def voiceover(req: VoiceRequest):
    try:
        return {
            "audio_path": production.create_voiceover(
                req.text, req.output_name, voice_id=req.voice_id
            )
        }
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/generate-image")
def image(req: ImageRequest):
    try:
        return {
            "image_path": production.generate_image(
                req.prompt, req.output_name, fmt=req.format
            )
        }
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/render")
def render(req: RenderRequest):
    try:
        return production.render_slideshow(
            req.image_paths,
            req.audio_path,
            req.format,
            req.output_name,
            scene_durations=req.scene_durations,
            captions=req.captions,
            transition_seconds=req.transition_seconds,
            burn_subtitles=req.burn_subtitles,
            normalize_audio=req.normalize_audio,
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/production/orchestrate")
def orchestrate(req: OrchestrateRequest):
    try:
        return orchestrator.run_pipeline(
            topic=req.topic,
            fmt=req.format,
            language=req.language,
            target_minutes=req.target_minutes,
            output_prefix=req.output_prefix,
            run_qa=req.run_qa,
        )
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Async Jobs, Assets, Preview & Download (Milestones 6 & 7)
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(req: JobCreateRequest):
    job = db.create_job(
        topic=req.topic,
        fmt=req.format,
        payload=req.model_dump(),
        max_attempts=req.max_attempts,
    )
    orchestrator.enqueue_job(job["id"])
    return job


@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    return {"jobs": db.list_jobs(limit=limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = db.request_cancel(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] == "running":
        raise HTTPException(409, "Job is currently running")
    updated = db.update_job(
        job_id,
        status="queued",
        stage="queued",
        progress_pct=0,
        attempts=0,
        cancel_requested=False,
        error="",
    )
    orchestrator.enqueue_job(job_id)
    return updated


@app.get("/api/jobs/{job_id}/preview")
def preview_job_video(job_id: str):
    job = db.get_job(job_id)
    if not job or not job.get("result"):
        raise HTTPException(404, "Rendered video not available for this job")
    video_path = Path(job["result"]["render"]["file"])
    if not video_path.is_file():
        raise HTTPException(404, "Video file missing on disk")
    return FileResponse(str(video_path), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download")
def download_job_video(job_id: str):
    job = db.get_job(job_id)
    if not job or not job.get("result"):
        raise HTTPException(404, "Rendered video not available for this job")
    video_path = Path(job["result"]["render"]["file"])
    if not video_path.is_file():
        raise HTTPException(404, "Video file missing on disk")
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        filename=video_path.name,
    )


@app.get("/api/assets")
def list_assets(job_id: str | None = None, limit: int = 100):
    return {"assets": db.list_assets(job_id=job_id, limit=limit)}


# ---------------------------------------------------------------------------
# QA & YouTube Publishing (Milestones 8, 10, 11)
# ---------------------------------------------------------------------------

@app.post("/api/qa/check")
def check_qa(req: QACheckRequest):
    try:
        res = qa.run_qa_checks(
            video_path=req.video_path,
            fmt=req.format,
            scene_plan=req.scene_plan,
            subtitle_path=req.subtitle_path,
            asset_ids=req.asset_ids,
        )
        return res.model_dump()
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/status")
def youtube_status():
    tokens = youtube.load_tokens()
    return {
        "connected": bool(tokens and (tokens.get("access_token") or tokens.get("refresh_token"))),
        "kill_switch": youtube.is_kill_switch_enabled(),
        "public_scheduling_enabled": settings.youtube_public_scheduling_enabled,
        "recent_publishes": db.list_publish_records(limit=20),
    }


@app.get("/api/youtube/oauth/url")
def youtube_oauth_url(state: str = "studio"):
    try:
        return {"url": youtube.get_oauth_authorization_url(state=state)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/oauth/callback")
def youtube_oauth_callback(code: str):
    try:
        return youtube.exchange_oauth_code(code)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/youtube/kill-switch")
def toggle_kill_switch(req: KillSwitchRequest):
    state = youtube.set_kill_switch(req.enabled)
    return {"kill_switch": state}


@app.post("/api/youtube/upload")
def youtube_upload(req: YouTubeUploadRequest):
    try:
        return youtube.upload_job_video(
            job_id=req.job_id,
            title=req.title,
            description=req.description,
            tags=req.tags,
            publish_at=req.publish_at,
            idempotency_key=req.idempotency_key,
            contains_synthetic_media=req.contains_synthetic_media,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
