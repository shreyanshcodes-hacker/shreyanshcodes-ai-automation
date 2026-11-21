"""End-to-end production orchestrator and async job runner with retry and cancellation."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from . import ai, db, production, qa
from .schemas import ScenePlan

logger = logging.getLogger(__name__)

_ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}


class JobCancelledError(RuntimeError):
    """Raised when a job is cancelled cooperatively."""


def _check_cancelled(job_id: str | None) -> None:
    if job_id and db.is_cancel_requested(job_id):
        raise JobCancelledError(f"Job {job_id} was cancelled")


def run_pipeline(
    *,
    topic: str,
    fmt: str = "short",
    language: str = "Hinglish",
    target_minutes: int = 8,
    output_prefix: str | None = None,
    run_qa: bool = True,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Run script/scene-plan -> voiceover -> scene images -> render -> QA."""
    prefix = output_prefix or (job_id or uuid.uuid4().hex[:10])
    asset_ids: list[str] = []

    if job_id:
        db.update_job(job_id, status="running", stage="scene_plan", progress_pct=10)
    _check_cancelled(job_id)

    scene_plan: ScenePlan = ai.generate_scene_plan(
        topic=topic,
        fmt=fmt,
        language=language,
        target_minutes=target_minutes,
    )
    if job_id:
        db.update_job(job_id, stage="voiceover", progress_pct=30)
    _check_cancelled(job_id)

    audio_path = production.create_voiceover(
        scene_plan.full_narration,
        output_name=f"{prefix}_voiceover.mp3",
    )
    audio_asset = db.register_asset(
        job_id=job_id,
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

    if job_id:
        db.update_job(job_id, stage="images", progress_pct=50)

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
        image_paths.append(img_asset["file_path"])
        if job_id:
            pct = 50 + int(((idx + 1) / max(1, total_scenes)) * 25)
            db.update_job(job_id, stage="images", progress_pct=pct)

    _check_cancelled(job_id)
    if job_id:
        db.update_job(job_id, stage="render", progress_pct=80)

    render_result = production.render_slideshow(
        image_paths=image_paths,
        audio_path=audio_asset["file_path"],
        fmt=fmt,
        output_name=f"{prefix}_final.mp4",
        scene_durations=[s.scene_duration for s in scene_plan.scenes],
        captions=[s.caption_text for s in scene_plan.scenes],
    )
    video_asset = db.register_asset(
        job_id=job_id,
        asset_type="video",
        file_path=render_result["file"],
        mime_type="video/mp4",
        width=render_result.get("width"),
        height=render_result.get("height"),
        duration_seconds=render_result.get("duration_seconds"),
        provenance={
            "provider": "ffmpeg",
            "synthetic_disclosure": scene_plan.synthetic_disclosure,
            "source_assets": asset_ids.copy(),
        },
        deduplicate=False,
    )
    asset_ids.append(video_asset["id"])

    qa_report: dict[str, Any] | None = None
    if run_qa:
        _check_cancelled(job_id)
        if job_id:
            db.update_job(job_id, stage="qa", progress_pct=92)
        qa_res = qa.run_qa_checks(
            video_path=render_result["file"],
            fmt=fmt,
            scene_plan=scene_plan,
            subtitle_path=render_result.get("subtitle_file"),
            asset_ids=asset_ids,
            check_duplicate_hash=False,
        )
        qa_report = qa_res.model_dump()

    result = {
        "job_id": job_id,
        "scene_plan": scene_plan.model_dump(),
        "audio_path": audio_asset["file_path"],
        "image_paths": image_paths,
        "render": render_result,
        "qa_report": qa_report,
        "asset_ids": asset_ids,
    }
    if job_id:
        db.update_job(
            job_id,
            status="completed",
            stage="completed",
            progress_pct=100,
            result=result,
            error="",
        )
    return result


async def _run_job_with_retries(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    payload = job["payload"]
    max_attempts = int(job.get("max_attempts", 3))

    for attempt in range(job.get("attempts", 0) + 1, max_attempts + 1):
        if db.is_cancel_requested(job_id):
            db.update_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                error="Cancelled by user",
            )
            return
        db.update_job(job_id, attempts=attempt, status="running", error="")
        try:
            await asyncio.to_thread(
                run_pipeline,
                topic=payload["topic"],
                fmt=payload.get("format", "short"),
                language=payload.get("language", "Hinglish"),
                target_minutes=int(payload.get("target_minutes", 8)),
                output_prefix=job_id,
                run_qa=True,
                job_id=job_id,
            )
            return
        except JobCancelledError:
            db.update_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                error="Cancelled by user",
            )
            return
        except Exception as exc:
            logger.warning("Job %s attempt %d/%d failed: %s", job_id, attempt, max_attempts, exc)
            if attempt >= max_attempts:
                db.update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    error=str(exc),
                )
                return
            backoff = min(2.0 * (2 ** (attempt - 1)), 30.0)
            db.update_job(
                job_id,
                status="queued",
                stage="retrying",
                error=f"Attempt {attempt} failed: {exc}",
            )
            await asyncio.sleep(backoff)


def enqueue_job(job_id: str) -> None:
    """Schedule job execution on the running asyncio event loop."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(_run_job_with_retries(job_id))
    _ACTIVE_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _ACTIVE_TASKS.pop(job_id, None))
