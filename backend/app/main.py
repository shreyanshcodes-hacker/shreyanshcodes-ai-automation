"""FastAPI application for Shreyansh Codes AI YouTube Automation Studio (hardened)."""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import db, orchestrator, production, qa, youtube
from .ai import generate_scene_plan, generate_text
from .config import settings, validate_cors_config, validate_production_settings
from .schemas import (
    ImageRequest,
    JobCreateRequest,
    KillSwitchRequest,
    OrchestrateRequest,
    QACheckRequest,
    RenderRequest,
    ScenePlanRequest,
    ScriptRequest,
    UserLoginRequest,
    UserRegisterRequest,
    VisualRequest,
    VoiceRequest,
    YouTubeUploadRequest,
)
from .security import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    assert_job_access,
    create_access_token,
    get_current_user,
    hash_password,
    rate_limit,
    require_owner_or_admin,
    resolve_safe_storage_path,
    verify_password,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_settings()
    db.init_db()
    orchestrator.worker_manager.start()
    try:
        yield
    finally:
        await orchestrator.worker_manager.stop()


app = FastAPI(
    title="Shreyansh Codes AI YouTube Automation",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=validate_cors_config(settings),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)


# ---------------------------------------------------------------------------
# Health & Auth
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": settings.app_env,
        "kill_switch": youtube.is_kill_switch_enabled(),
    }


@app.post("/api/auth/register")
def register(req: UserRegisterRequest, response: Response):
    if db.get_user_by_username(req.username):
        raise HTTPException(409, "Username already exists")
    role = "owner" if db.count_users() == 0 else "user"
    user = db.create_user(req.username, hash_password(req.password), role=role)
    token = create_access_token(user["id"], user["username"], user["role"])
    csrf = secrets.token_urlsafe(24)
    secure_cookie = settings.app_env == "production"
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", secure=secure_cookie
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf, httponly=False, samesite="lax", secure=secure_cookie
    )
    return {"user": user, "access_token": token, "csrf_token": csrf}


@app.post("/api/auth/login")
def login(req: UserLoginRequest, response: Response):
    user = db.get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    token = create_access_token(user["id"], user["username"], user["role"])
    csrf = secrets.token_urlsafe(24)
    secure_cookie = settings.app_env == "production"
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", secure=secure_cookie
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf, httponly=False, samesite="lax", secure=secure_cookie
    )
    return {
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        "access_token": token,
        "csrf_token": csrf,
    }


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return {"logged_out": True}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(get_current_user)):
    return {"user": user}


# ---------------------------------------------------------------------------
# Settings (Owner/Admin only)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings(_user: dict[str, Any] = Depends(require_owner_or_admin)):
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


# ---------------------------------------------------------------------------
# AI & Production Endpoints (Authenticated + Rate-limited + Path-safe)
# ---------------------------------------------------------------------------

@app.post("/api/script", dependencies=[Depends(rate_limit("ai_gen", limit=20))])
def script(req: ScriptRequest, _user: dict[str, Any] = Depends(get_current_user)):
    duration = "30–60 seconds" if req.format == "short" else f"about {req.target_minutes} minutes"
    prompt = f"""Write an original {req.format} {req.language} YouTube script for Shreyansh Codes about {req.topic}.
Target {duration}. Faceless production: narration, numbered scenes, AI visual prompts,
on-screen Hinglish captions, practical example and CTA. Flag facts needing verification.
Avoid copied wording, invented facts and filler."""
    try:
        return {"script": generate_text(prompt)}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/scene-plan", dependencies=[Depends(rate_limit("ai_gen", limit=20))])
def scene_plan(req: ScenePlanRequest, _user: dict[str, Any] = Depends(get_current_user)):
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


@app.post("/api/production/visual-prompt", dependencies=[Depends(rate_limit("ai_gen", limit=30))])
def visual_prompt(req: VisualRequest, _user: dict[str, Any] = Depends(get_current_user)):
    try:
        return {"prompt": production.generate_visual_prompt(req.topic, req.scene, req.format)}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/voiceover", dependencies=[Depends(rate_limit("tts", limit=15))])
def voiceover(req: VoiceRequest, _user: dict[str, Any] = Depends(get_current_user)):
    safe_out = resolve_safe_storage_path(f"assets/{Path(req.output_name).name}", allowed_exts={".mp3"})
    try:
        return {
            "audio_path": production.create_voiceover(
                req.text, safe_out.name, voice_id=req.voice_id
            )
        }
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/generate-image", dependencies=[Depends(rate_limit("image_gen", limit=20))])
def image(req: ImageRequest, _user: dict[str, Any] = Depends(get_current_user)):
    safe_out = resolve_safe_storage_path(
        f"assets/{Path(req.output_name).name}", allowed_exts={".png", ".jpg", ".jpeg", ".webp"}
    )
    try:
        return {
            "image_path": production.generate_image(
                req.prompt, safe_out.name, fmt=req.format
            )
        }
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/production/render", dependencies=[Depends(rate_limit("render", limit=10))])
def render(req: RenderRequest, _user: dict[str, Any] = Depends(get_current_user)):
    safe_images = [
        str(
            resolve_safe_storage_path(
                p, allowed_exts={".png", ".jpg", ".jpeg", ".webp"}, must_exist=True, max_bytes=50_000_000
            )
        )
        for p in req.image_paths
    ]
    safe_audio = str(
        resolve_safe_storage_path(
            req.audio_path, allowed_exts={".mp3", ".wav", ".m4a"}, must_exist=True, max_bytes=100_000_000
        )
    )
    safe_out = resolve_safe_storage_path(f"renders/{Path(req.output_name).name}", allowed_exts={".mp4"})
    try:
        return production.render_slideshow(
            safe_images,
            safe_audio,
            req.format,
            safe_out.name,
            scene_durations=req.scene_durations,
            captions=req.captions,
            transition_seconds=req.transition_seconds,
            burn_subtitles=req.burn_subtitles,
            normalize_audio=req.normalize_audio,
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/production/orchestrate", dependencies=[Depends(rate_limit("orchestrate", limit=10))])
def orchestrate(req: OrchestrateRequest, user: dict[str, Any] = Depends(get_current_user)):
    try:
        return orchestrator.run_pipeline(
            topic=req.topic,
            fmt=req.format,
            language=req.language,
            target_minutes=req.target_minutes,
            output_prefix=req.output_prefix,
            run_qa=req.run_qa,
            owner_user_id=user["id"],
        )
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Jobs, Assets, Preview & Download (Authenticated + User-Isolated + Path-Safe)
# ---------------------------------------------------------------------------

@app.post("/api/jobs", dependencies=[Depends(rate_limit("jobs_create", limit=15))])
def create_job(req: JobCreateRequest, user: dict[str, Any] = Depends(get_current_user)):
    return db.create_job(
        topic=req.topic,
        fmt=req.format,
        payload=req.model_dump(),
        max_attempts=req.max_attempts,
        owner_user_id=user["id"],
    )


@app.get("/api/jobs")
def list_jobs(limit: int = 50, user: dict[str, Any] = Depends(get_current_user)):
    owner_filter = None if user.get("role") in ("owner", "admin") else user["id"]
    return {"jobs": db.list_jobs(limit=limit, owner_user_id=owner_filter)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    return assert_job_access(db.get_job(job_id), user)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    assert_job_access(db.get_job(job_id), user)
    return db.request_cancel(job_id)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    job = assert_job_access(db.get_job(job_id), user)
    if job["status"] == "running":
        raise HTTPException(409, "Job is currently running")
    return db.update_job(
        job_id,
        status="queued",
        stage="queued",
        progress_pct=0,
        attempts=0,
        cancel_requested=False,
        error="",
        worker_id="",
        lease_expires_at="",
    )


@app.get("/api/jobs/{job_id}/preview")
def preview_job_video(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    job = assert_job_access(db.get_job(job_id), user)
    if not job.get("result"):
        raise HTTPException(404, "Rendered video not available for this job")
    video_path = resolve_safe_storage_path(
        job["result"]["render"]["file"], allowed_exts={".mp4"}, must_exist=True
    )
    return FileResponse(str(video_path), media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download")
def download_job_video(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    job = assert_job_access(db.get_job(job_id), user)
    if not job.get("result"):
        raise HTTPException(404, "Rendered video not available for this job")
    video_path = resolve_safe_storage_path(
        job["result"]["render"]["file"], allowed_exts={".mp4"}, must_exist=True
    )
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        filename=video_path.name,
    )


@app.get("/api/assets")
def list_assets(
    job_id: str | None = None,
    limit: int = 100,
    user: dict[str, Any] = Depends(get_current_user),
):
    owner_filter = None if user.get("role") in ("owner", "admin") else user["id"]
    return {"assets": db.list_assets(job_id=job_id, owner_user_id=owner_filter, limit=limit)}


# ---------------------------------------------------------------------------
# QA & YouTube Publishing (Authenticated + Owner/Admin Gates)
# ---------------------------------------------------------------------------

@app.post("/api/qa/check")
def check_qa(req: QACheckRequest, _user: dict[str, Any] = Depends(get_current_user)):
    safe_video = resolve_safe_storage_path(
        req.video_path, allowed_exts={".mp4", ".mov", ".mkv"}, must_exist=True
    )
    safe_sub = (
        resolve_safe_storage_path(req.subtitle_path, allowed_exts={".srt", ".ass"}, must_exist=True)
        if req.subtitle_path
        else None
    )
    try:
        res = qa.run_qa_checks(
            video_path=safe_video,
            fmt=req.format,
            scene_plan=req.scene_plan,
            subtitle_path=safe_sub,
            asset_ids=req.asset_ids,
            provenance_records=req.provenance_records,
        )
        return res.model_dump()
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/status")
def youtube_status(user: dict[str, Any] = Depends(get_current_user)):
    tokens = youtube.load_tokens()
    owner_filter = None if user.get("role") in ("owner", "admin") else user["id"]
    return {
        "connected": bool(tokens and (tokens.get("access_token") or tokens.get("refresh_token"))),
        "kill_switch": youtube.is_kill_switch_enabled(),
        "public_scheduling_enabled": settings.youtube_public_scheduling_enabled,
        "recent_publishes": db.list_publish_records(limit=20, owner_user_id=owner_filter),
    }


@app.get("/api/youtube/oauth/url")
def youtube_oauth_url(user: dict[str, Any] = Depends(require_owner_or_admin)):
    try:
        return {"url": youtube.get_oauth_authorization_url(user_id=user["id"])}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/oauth/callback")
def youtube_oauth_callback(
    code: str,
    state: str,
    user: dict[str, Any] = Depends(require_owner_or_admin),
):
    try:
        return youtube.exchange_oauth_code(code=code, state=state, user_id=user["id"])
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/youtube/kill-switch")
def toggle_kill_switch(
    req: KillSwitchRequest,
    _user: dict[str, Any] = Depends(require_owner_or_admin),
):
    state = youtube.set_kill_switch(req.enabled)
    return {"kill_switch": state}


@app.post("/api/youtube/upload", dependencies=[Depends(rate_limit("yt_upload", limit=10))])
def youtube_upload(
    req: YouTubeUploadRequest,
    user: dict[str, Any] = Depends(require_owner_or_admin),
):
    assert_job_access(db.get_job(req.job_id), user)
    try:
        return youtube.upload_job_video(
            job_id=req.job_id,
            title=req.title,
            description=req.description,
            tags=req.tags,
            publish_at=req.publish_at,
            idempotency_key=req.idempotency_key,
            owner_user_id=user["id"],
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
