# Faceless AI video production

## Confirmed creative direction
- Faceless videos; no avatar or on-camera presenter.
- AI-generated natural Hinglish voiceover.
- AI-generated scene visuals.
- Shorts: 1080x1920 (9:16), target 30–60 seconds.
- Long-form: 1920x1080 (16:9), target 5–20 minutes.

## Current endpoints
- `POST /api/script`: Gemini writes an original faceless script with scene and visual directions.
- `POST /api/production/visual-prompt`: Gemini creates one scene-specific image prompt.
- `POST /api/production/voiceover`: ElevenLabs generates an MP3 when credentials are configured.
- `POST /api/production/generate-image`: Replicate generates a still image when credentials and a compatible model version are configured.
- `POST /api/production/render`: FFmpeg assembles scene images and narration into MP4 and verifies aspect dimensions and audio stream.

## Configuration
Install FFmpeg (both `ffmpeg` and `ffprobe` must be on PATH). Configure `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `REPLICATE_API_TOKEN`, and `REPLICATE_MODEL_VERSION` in backend `.env`.

The selected Replicate model must accept `input.prompt`; some models use different schemas, so adapt the adapter to that model's published API contract. Choose a voice and image model with rights suitable for your channel.

## Limitations / next engineering tasks
The current renderer uses still-image scenes, with equal scene durations. It is not yet a full-motion AI video generator. Add scene duration/timing, transitions, subtitle alignment and burn-in, loudness normalization, job queue/persistence, retry handling, cloud storage, video preview, automated content/policy review and YouTube publishing. Do not enable public auto-publishing until these checks pass.
