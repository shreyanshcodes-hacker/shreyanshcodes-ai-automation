"""Durable database-backed production orchestrator and worker manager."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ai, db, production, qa
from .schemas import ScenePlan

logger = logging.getLogger(__name__)


class JobCancelledError(RuntimeError):
    """Raised when a job is cancelled cooperatively."""


def _check_cancelled(job_id: str | None) -> None:
    if job_id and db.is_cancel_requested(job_id):
        raise JobCancelledError(f"Job {job_id} was cancelled")


def compute_job_disclosure(scene_plan: ScenePlan, asset_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive synthetic media disclosure requirements strictly from generated assets and ScenePlan."""
    has_ai_visuals = any(
        a.get("asset_type") == "image" and a.get("provenance", {}).get("synthetic", True)
        for a in asset_records
    )
    has_ai_voice = any(
        a.get("asset_type") == "audio" and a.get("provenance", {}).get("synthetic", True)
        for a in asset_records
    )
    has_other = bool(scene_plan.synthetic_disclosure)
    return {
        "has_ai_visuals": has_ai_visuals,
        "has_ai_voice": has_ai_voice,
        "has_other_synthetic": has_other,
        "requires_disclosure": bool(has_ai_visuals or has_ai_voice or has_other),
    }


def run_pipeline(
    *,
    topic: str,
    fmt: str = "short",
    language: str = "Hinglish",
    target_minutes: int = 8,
    output_prefix: str | None = None,
    run_qa: bool = True,
    job_id: str | None = None,
    owner_user_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Run script/scene-plan -> voiceover -> scene images -> render -> QA with lease heartbeats."""
    prefix = output_prefix or (job_id or uuid.uuid4().hex[:10])
    asset_ids: list[str] = []
    asset_records: list[dict[str, Any]] = []

    def _heartbeat(stage: str, pct: int) -> None:
        if job_id:
            db.update_job(job_id, status="running", stage=stage, progress_pct=pct)
            if worker_id:
                db.renew_job_lease(job_id, worker_id)

    _heartbeat("scene_plan", 10)
    _check_cancelled(job_id)

    scene_plan: ScenePlan = ai.generate_scene_plan(
        topic=topic,
        fmt=fmt,
        language=language,
        target_minutes=target_minutes,
    )
    _heartbeat("voiceover", 30)
    _check_cancelled(job_id)

    audio_path = production.create_voiceover(
        scene_plan.full_narration,
        output_name=f"{prefix}_voiceover.mp3",
    )
    audio_asset = db.register_asset(
        job_id=job_id,
        owner_user_id=owner_user_id,
        asset_type="audio",
        file_path=audio_path,
        mime_type="audio/mpeg",
        provenance={
            "provider": "elevenlabs",
            "consent_verified": True,
            "synthetic": True,
        },
    )
    asset_ids.append(audio_asset["id"])
    asset_records.append(audio_asset)

    _heartbeat("images", 50)

    image_paths: list[str] = []
    total_scenes = len(scene_plan.scenes)
    for idx, scene in enumerate(scene_plan.scenes):
        _check_cancelled(job_id)
        img_path = production.generate_image(
            scene.visual_prompt,
            output_name=f"{prefix}_scene_{scene.scene_index:02d}.png",
            fmt=fmt,
        )
        mime, w, h = production.inspect_image_bytes(Path(img_path).read_bytes())
        img_asset = db.register_asset(
            job_id=job_id,
            owner_user_id=owner_user_id,
            asset_type="image",
            file_path=img_path,
            mime_type=mime,
            width=w,
            height=h,
            provenance={
                "provider": "replicate",
                "scene_index": scene.scene_index,
                "synthetic": True,
                "source_notes": scene.source_notes,
            },
        )
        asset_ids.append(img_asset["id"])
        asset_records.append(img_asset)
        image_paths.append(img_asset["file_path"])
        pct = 50 + int(((idx + 1) / max(1, total_scenes)) * 25)
        _heartbeat("images", pct)

    _check_cancelled(job_id)
    _heartbeat("render", 80)

    render_result = production.render_slideshow(
        image_paths=image_paths,
        audio_path=audio_asset["file_path"],
        fmt=fmt,
        output_name=f"{prefix}_final.mp4",
        scene_durations=[s.scene_duration for s in scene_plan.scenes],
        captions=[s.caption_text for s in scene_plan.scenes],
    )
    disclosure = compute_job_disclosure(scene_plan, asset_records)
    video_asset = db.register_asset(
        job_id=job_id,
        owner_user_id=owner_user_id,
        asset_type="video",
        file_path=render_result["file"],
        mime_type="video/mp4",
        width=render_result.get("width"),
        height=render_result.get("height"),
        duration_seconds=render_result.get("duration_seconds"),
        provenance={
            "provider": "ffmpeg",
            "renderer_mode": render_result.get("renderer_mode", "still_image_slideshow"),
            "disclosure": disclosure,
            "source_assets": asset_ids.copy(),
        },
        deduplicate=False,
    )
    asset_ids.append(video_asset["id"])

    qa_report: dict[str, Any] | None = None
    if run_qa:
        _check_cancelled(job_id)
        _heartbeat("qa", 92)
        qa_res = qa.run_qa_checks(
            video_path=render_result["file"],
            fmt=fmt,
            scene_plan=scene_plan,
            subtitle_path=render_result.get("subtitle_file"),
            asset_ids=asset_ids,
            check_duplicate_hash=False,
        )
        qa_report = qa_res.model_dump()

    provenance_dump = [r.model_dump() for r in scene_plan.provenance_records]
    result = {
        "job_id": job_id,
        "scene_plan": scene_plan.model_dump(),
        "audio_path": audio_asset["file_path"],
        "image_paths": image_paths,
        "render": render_result,
        "qa_report": qa_report,
        "disclosure": disclosure,
        "provenance_records": provenance_dump,
        "asset_ids": asset_ids,
    }
    if job_id:
        db.update_job(
            job_id,
            status="completed",
            stage="completed",
            progress_pct=100,
            result=result,
            disclosure=disclosure,
            provenance=provenance_dump,
            error="",
            worker_id="",
            lease_expires_at="",
        )
    return result


def process_claimed_job(job: dict[str, Any], worker_id: str) -> None:
    """Execute one atomically-claimed job with retry/backoff and crash recovery."""
    job_id = job["id"]
    payload = job["payload"]
    attempts = int(job.get("attempts", 1))
    max_attempts = int(job.get("max_attempts", 3))

    if db.is_cancel_requested(job_id):
        db.update_job(job_id, status="cancelled", stage="cancelled", error="Cancelled by user")
        return

    try:
        run_pipeline(
            topic=payload["topic"],
            fmt=payload.get("format", "short"),
            language=payload.get("language", "Hinglish"),
            target_minutes=int(payload.get("target_minutes", 8)),
            output_prefix=job_id,
            run_qa=True,
            job_id=job_id,
            owner_user_id=job.get("owner_user_id"),
            worker_id=worker_id,
        )
    except JobCancelledError:
        db.update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            error="Cancelled by user",
            worker_id="",
            lease_expires_at="",
        )
    except Exception as exc:
        logger.warning("Job %s attempt %d/%d failed: %s", job_id, attempts, max_attempts, exc)
        if attempts >= max_attempts:
            db.update_job(
                job_id,
                status="failed",
                stage="failed",
                error=str(exc),
                worker_id="",
                lease_expires_at="",
            )
        else:
            backoff_sec = min(2.0 * (2 ** (attempts - 1)), 60.0)
            next_run = (datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)).isoformat()
            db.update_job(
                job_id,
                status="queued",
                stage="retrying",
                error=f"Attempt {attempts} failed: {exc}",
                worker_id="",
                lease_expires_at="",
                next_run_at=next_run,
            )


class WorkerManager:
    """Durable background worker loop that recovers stale leases and claims queued jobs atomically."""

    def __init__(self, poll_interval: float = 1.0) -> None:
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    def start(self) -> None:
        db.recover_stale_jobs()
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                db.recover_stale_jobs()
                job = await asyncio.to_thread(db.claim_next_job, self.worker_id)
                if job:
                    await asyncio.to_thread(process_claimed_job, job, self.worker_id)
                    continue
            except Exception as exc:
                logger.error("Worker loop error: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass


worker_manager = WorkerManager()
