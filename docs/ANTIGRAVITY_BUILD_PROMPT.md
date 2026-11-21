# Google Antigravity implementation prompt

Act as the lead engineer. Inspect this repo and continue the existing implementation; do not replace it with a mockup.

Product: Shreyansh Codes AI YouTube Automation Studio.
Use Google Antigravity with Gemini as the coding agent and Gemini API as the runtime content model.
User choices: faceless videos, AI-generated Hinglish voice, AI-generated visuals; 2 Shorts/day and 2 long-form videos/week; automatic publishing is desired after strict QA.

## Existing foundation
- FastAPI app in `backend/app`
- Gemini text generation service
- Script endpoint
- ElevenLabs voiceover adapter
- Replicate image-generation adapter (assumes selected model accepts `input.prompt`)
- FFmpeg still-image slideshow renderer for 9:16 and 16:9
- Basic frontend from earlier starter can be integrated/rebuilt

## Continue in milestones
1. Add a strict Pydantic scene-plan schema and Gemini structured output: scene index, narration, visual prompt, caption text, scene duration and source notes.
2. Add orchestration endpoint that generates script -> scene plan -> voiceover -> scene images -> render.
3. Adapt Replicate image adapter to the exact selected model/version schema; validate returned image mime type and dimensions.
4. Improve voice generation: chunk long narration, handle rate limits, concatenate audio, verify pronunciation for Hinglish; voice selection must be authorized.
5. Upgrade FFmpeg rendering: per-scene durations, transitions, subtitle timing/burn-in, audio loudness normalization, 9:16 Shorts and 16:9 long video templates.
6. Add asynchronous job table, progress state, retry/backoff, cancellation, persistent asset records and deduplication.
7. Add preview and download endpoints plus React UI for jobs and rendering progress.
8. Add QA: expected dimensions, duration range, audio stream, corrupt/missing assets, black frames, clipping/loudness, subtitle safe margins, duplicate file hash, factual-source checks and asset provenance.
9. Add n8n schedules: daily topic research, two Shorts per day, two long videos weekly, publishing scheduler and weekly analytics.
10. Implement YouTube OAuth 2.0 resumable uploads, private-first upload, publishAt scheduling, idempotency, quota-aware retry and kill switch.
11. Automatic publishing is the target, but failed QA or missing source/rights evidence must keep a video private. Public scheduling only after owner explicitly configures it. Store tokens server-side; never log secrets.
12. Add tests, deployment docs, .env.example and security checklist.

Use original scripts and user-owned, licensed or generated assets only. No cloning voices without consent, no re-uploading other creators' work, no fabricated trend statistics. Apply YouTube altered/synthetic disclosure when applicable. Report every external service that still requires credentials. Test each milestone before proceeding.
