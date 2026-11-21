"""Gemini text generation and structured ScenePlan generation."""
from __future__ import annotations

import json
import re
from google import genai
from google.genai import types

from .config import settings
from .schemas import ScenePlan


def _client() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError("Set GEMINI_API_KEY in .env")
    return genai.Client(api_key=settings.gemini_api_key)


def generate_text(prompt: str) -> str:
    response = _client().models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
    )
    return response.text or ""


def _extract_json_payload(raw_text: str) -> str:
    cleaned = raw_text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return cleaned


def generate_scene_plan(
    topic: str,
    fmt: str = "short",
    language: str = "Hinglish",
    target_minutes: int = 8,
    scene_count: int | None = None,
) -> ScenePlan:
    """Generate a strict Pydantic-validated ScenePlan using Gemini structured output."""
    if fmt == "short":
        duration_hint = "35 to 55 seconds total"
        default_scenes = scene_count or 5
        aspect_hint = "vertical 9:16"
    else:
        duration_hint = f"about {target_minutes} minutes ({target_minutes * 60} seconds) total"
        default_scenes = scene_count or max(6, min(20, target_minutes * 2))
        aspect_hint = "horizontal 16:9"

    prompt = f"""Create an original, faceless YouTube video scene plan for 'Shreyansh Codes'.
Topic: {topic}
Format: {fmt} ({aspect_hint})
Language: {language} (natural conversational Hinglish using Roman script for Hindi+English tech terms)
Target duration: {duration_hint}
Number of scenes: {default_scenes}

Rules:
1. Original script only; no copied wording, no fabricated trend statistics, no unverified claims.
2. Each scene must have:
   - scene_index (1..{default_scenes})
   - narration (spoken Hinglish voiceover)
   - visual_prompt (detailed AI image prompt for {aspect_hint}, no logos/watermarks/copyrighted characters/text)
   - caption_text (concise on-screen Hinglish caption, max 90 chars)
   - scene_duration (seconds, realistic for the narration word count)
   - source_notes (cite official docs, primary sources, or mark 'Original conceptual explanation')
   - needs_verification (true if any factual number/date/benchmark needs verification)
3. Set synthetic_disclosure=true because AI voice and AI visuals are used.
4. Populate facts_to_verify with any specific claims that must be checked before publishing."""

    client = _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ScenePlan,
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=config,
    )

    if getattr(response, "parsed", None) is not None:
        parsed = response.parsed
        if isinstance(parsed, ScenePlan):
            return parsed
        return ScenePlan.model_validate(parsed)

    raw_text = response.text or ""
    if not raw_text.strip():
        raise RuntimeError("Gemini returned an empty response for scene plan")

    data = json.loads(_extract_json_payload(raw_text))
    return ScenePlan.model_validate(data)
