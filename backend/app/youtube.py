"""YouTube OAuth 2.0, resumable private-first upload, scheduling, idempotency, and kill switch."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from . import db
from .config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

_RUNTIME_KILL_SWITCH: bool | None = None


def is_kill_switch_enabled() -> bool:
    if _RUNTIME_KILL_SWITCH is not None:
        return _RUNTIME_KILL_SWITCH
    env_val = os.getenv("YOUTUBE_KILL_SWITCH", str(settings.youtube_kill_switch)).lower()
    return env_val in ("1", "true", "yes")


def set_kill_switch(enabled: bool) -> bool:
    global _RUNTIME_KILL_SWITCH
    _RUNTIME_KILL_SWITCH = bool(enabled)
    return _RUNTIME_KILL_SWITCH


def _token_path() -> Path:
    p = Path(os.getenv("YOUTUBE_TOKEN_FILE", settings.youtube_token_file)).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _save_tokens(tokens: dict[str, Any]) -> None:
    p = _token_path()
    p.write_text(json.dumps(tokens), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_tokens() -> dict[str, Any] | None:
    p = _token_path()
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def get_oauth_authorization_url(state: str = "studio") -> str:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", settings.youtube_client_id)
    redirect_uri = os.getenv("YOUTUBE_REDIRECT_URI", settings.youtube_redirect_uri)
    if not client_id:
        raise RuntimeError("Configure YOUTUBE_CLIENT_ID in .env")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_oauth_code(code: str) -> dict[str, Any]:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", settings.youtube_client_id)
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret)
    redirect_uri = os.getenv("YOUTUBE_REDIRECT_URI", settings.youtube_redirect_uri)
    if not client_id or not client_secret:
        raise RuntimeError("Configure YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env")

    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()
    tokens["obtained_at"] = int(time.time())
    _save_tokens(tokens)
    return {"connected": True, "scope": tokens.get("scope", "")}


def _get_valid_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("YouTube OAuth not connected. Complete /api/youtube/oauth/url flow first.")
    obtained = int(tokens.get("obtained_at", 0))
    expires_in = int(tokens.get("expires_in", 3600))
    if time.time() < obtained + expires_in - 60 and tokens.get("access_token"):
        return str(tokens["access_token"])

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("YouTube access token expired and no refresh_token is stored.")
    client_id = os.getenv("YOUTUBE_CLIENT_ID", settings.youtube_client_id)
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret)
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    refreshed = resp.json()
    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_in"] = refreshed.get("expires_in", 3600)
    tokens["obtained_at"] = int(time.time())
    _save_tokens(tokens)
    return str(tokens["access_token"])


def _is_public_scheduling_enabled() -> bool:
    val = os.getenv(
        "YOUTUBE_PUBLIC_SCHEDULING_ENABLED",
        str(settings.youtube_public_scheduling_enabled),
    ).lower()
    return val in ("1", "true", "yes")


def _resumable_upload(
    *,
    access_token: str,
    video_path: Path,
    metadata: dict[str, Any],
    chunk_size: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    file_size = video_path.stat().st_size
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }
    init_resp = requests.post(
        f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
        headers=init_headers,
        json=metadata,
        timeout=60,
    )
    if init_resp.status_code == 403 and "quotaExceeded" in init_resp.text:
        raise RuntimeError("YouTube API daily quota exceeded; upload paused.")
    init_resp.raise_for_status()
    upload_url = init_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube resumable init did not return a Location header.")

    with open(video_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk = f.read(chunk_size)
            end = offset + len(chunk) - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{file_size}",
            }
            for attempt in range(4):
                resp = requests.put(upload_url, headers=headers, data=chunk, timeout=180)
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 308:
                    break
                if resp.status_code == 403 and "quotaExceeded" in resp.text:
                    raise RuntimeError("YouTube API daily quota exceeded during chunk upload.")
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
            offset += len(chunk)
    raise RuntimeError("Resumable upload ended without completion response.")


def upload_job_video(
    *,
    job_id: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    publish_at: str | None = None,
    idempotency_key: str | None = None,
    contains_synthetic_media: bool = True,
) -> dict[str, Any]:
    """Private-first YouTube resumable upload with strict QA, provenance, and kill-switch gates."""
    if is_kill_switch_enabled():
        raise PermissionError("YouTube publishing blocked: kill switch is enabled.")

    idem_key = idempotency_key or f"yt_{job_id}"
    existing = db.get_publish_by_idempotency(idem_key)
    if existing and existing.get("status") == "uploaded":
        return {**existing, "idempotent_replay": True}

    job = db.get_job(job_id)
    if not job or job.get("status") != "completed" or not job.get("result"):
        raise ValueError(f"Job '{job_id}' is not completed or has no render result.")

    result = job["result"]
    video_path = Path(result["render"]["file"])
    if not video_path.is_file():
        raise FileNotFoundError(f"Rendered video missing: {video_path}")

    qa_report = result.get("qa_report") or {}
    qa_passed = bool(qa_report.get("passed"))
    has_sources = bool(qa_report.get("checks", {}).get("factual_sources_present"))

    # Safety & Policy Gate (Milestone 11):
    # Private-first always; only schedule publishAt if QA passed, sources present, and owner enabled public scheduling.
    can_schedule_public = (
        qa_passed
        and has_sources
        and _is_public_scheduling_enabled()
        and bool(publish_at)
    )
    privacy_status = "private"
    effective_publish_at = publish_at if can_schedule_public else None

    metadata: dict[str, Any] = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": "28",  # Science & Technology
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(contains_synthetic_media),
        },
    }
    if effective_publish_at:
        metadata["status"]["publishAt"] = effective_publish_at

    access_token = _get_valid_access_token()
    yt_resp = _resumable_upload(
        access_token=access_token,
        video_path=video_path,
        metadata=metadata,
    )
    yt_video_id = yt_resp.get("id")
    record = db.upsert_publish_record(
        job_id=job_id,
        idempotency_key=idem_key,
        youtube_video_id=yt_video_id,
        privacy_status=privacy_status,
        publish_at=effective_publish_at,
        qa_passed=qa_passed,
        qa_report=qa_report,
        synthetic_disclosed=contains_synthetic_media,
        status="uploaded",
    )
    return {
        **record,
        "idempotent_replay": False,
        "kept_private_reason": (
            None
            if effective_publish_at
            else "Private-first safety gate active (QA/source verification or owner public-scheduling flag)."
        ),
    }
