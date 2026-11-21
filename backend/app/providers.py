"""Provider interfaces for decoupled image, motion-video, voice, rendering, subtitles, and QA."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ImageProvider(Protocol):
    def generate_image(self, prompt: str, output_name: str, fmt: str = "short") -> str: ...


class MotionVideoProvider(Protocol):
    """Interface for future AI motion-video generation providers (not yet active)."""

    def generate_motion_clip(
        self, prompt: str, duration_seconds: float, fmt: str = "short", output_name: str = "clip.mp4"
    ) -> str: ...


class VoiceProvider(Protocol):
    def create_voiceover(
        self, text: str, output_name: str = "voiceover.mp3", voice_id: str | None = None
    ) -> str: ...


class VideoRenderer(Protocol):
    def render_slideshow(
        self,
        image_paths: list[str],
        audio_path: str,
        fmt: str = "short",
        output_name: str = "final.mp4",
        **kwargs: Any,
    ) -> dict[str, Any]: ...


class SubtitleGenerator(Protocol):
    def write_srt_subtitles(
        self, captions: list[str], durations: list[float], target_path: Path
    ) -> Path: ...


class QualityChecker(Protocol):
    def run_qa_checks(self, **kwargs: Any) -> Any: ...
