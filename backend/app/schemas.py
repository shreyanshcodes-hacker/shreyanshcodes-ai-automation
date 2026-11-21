"""Strict Pydantic schemas for scene plans, jobs, QA, and publishing."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


class SceneItem(BaseModel):
    scene_index: int = Field(ge=1, description="1-based index of the scene.")
    narration: str = Field(min_length=3, max_length=2500, description="Hinglish voiceover narration for this scene.")
    visual_prompt: str = Field(min_length=5, max_length=1500, description="Original AI image-generation prompt.")
    caption_text: str = Field(min_length=1, max_length=240, description="Short on-screen caption text.")
    scene_duration: float = Field(gt=0.5, le=180.0, description="Target duration of this scene in seconds.")
    source_notes: str = Field(
        min_length=3,
        max_length=500,
        description="Source attribution or verification note for facts in this scene.",
    )
    needs_verification: bool = Field(
        default=False,
        description="True if any claim in this scene requires human fact-checking.",
    )


class ScenePlan(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    topic: str = Field(min_length=2, max_length=200)
    format: Literal["short", "long"] = "short"
    language: str = "Hinglish"
    target_duration_seconds: float = Field(gt=5.0, le=3600.0)
    synthetic_disclosure: bool = Field(
        default=True,
        description="True when AI voice or AI visuals are used (YouTube altered/synthetic media disclosure).",
    )
    scenes: list[SceneItem] = Field(min_length=1, max_length=60)
    facts_to_verify: list[str] = Field(default_factory=list)
    rights_and_originality_note: str = Field(
        default="Original script with AI-generated visuals and authorized AI voiceover."
    )

    @field_validator("scenes")
    @classmethod
    def validate_scene_indices(cls, scenes: list[SceneItem]) -> list[SceneItem]:
        expected = list(range(1, len(scenes) + 1))
        actual = [s.scene_index for s in scenes]
        if actual != expected:
            for idx, scene in enumerate(scenes, start=1):
                scene.scene_index = idx
        return scenes

    @property
    def full_narration(self) -> str:
        return " ".join(s.narration.strip() for s in self.scenes if s.narration.strip())


class ScriptRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    format: Literal["short", "long"] = "short"
    language: str = "Hinglish"
    target_minutes: int = Field(default=8, ge=1, le=30)


class ScenePlanRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    format: Literal["short", "long"] = "short"
    language: str = "Hinglish"
    target_minutes: int = Field(default=8, ge=1, le=30)
    scene_count: int | None = Field(default=None, ge=2, le=40)


class VoiceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=30000)
    output_name: str = "voiceover.mp3"
    voice_id: str | None = None


class VisualRequest(BaseModel):
    topic: str
    scene: str
    format: Literal["short", "long"] = "short"


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    output_name: str = "scene.png"
    format: Literal["short", "long"] = "short"


class RenderSceneInput(BaseModel):
    image_path: str
    duration: float | None = Field(default=None, gt=0.2)
    caption_text: str = ""


class RenderRequest(BaseModel):
    image_paths: list[str] = Field(min_length=1)
    audio_path: str
    format: Literal["short", "long"] = "short"
    output_name: str = "final.mp4"
    scene_durations: list[float] | None = None
    captions: list[str] | None = None
    transition_seconds: float = Field(default=0.35, ge=0.0, le=2.0)
    burn_subtitles: bool = True
    normalize_audio: bool = True


class OrchestrateRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    format: Literal["short", "long"] = "short"
    language: str = "Hinglish"
    target_minutes: int = Field(default=8, ge=1, le=30)
    output_prefix: str | None = None
    run_qa: bool = True


class JobCreateRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    format: Literal["short", "long"] = "short"
    language: str = "Hinglish"
    target_minutes: int = Field(default=8, ge=1, le=30)
    max_attempts: int = Field(default=3, ge=1, le=5)


class QACheckRequest(BaseModel):
    video_path: str
    format: Literal["short", "long"] = "short"
    scene_plan: ScenePlan | None = None
    subtitle_path: str | None = None
    asset_ids: list[str] = Field(default_factory=list)


class QACheckResult(BaseModel):
    passed: bool
    checks: dict[str, bool]
    details: dict[str, Any]
    blocking_reasons: list[str] = Field(default_factory=list)


class YouTubeUploadRequest(BaseModel):
    job_id: str
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    publish_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp (e.g. 2026-11-22T14:30:00Z). Only honored if QA passes and public scheduling is enabled.",
    )
    idempotency_key: str | None = None
    contains_synthetic_media: bool = True


class KillSwitchRequest(BaseModel):
    enabled: bool
