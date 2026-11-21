# Google Antigravity Task: Secure, Fix, and Production-Harden Shreyansh Codes AI YouTube Automation

You are working on my existing project:

**Repository:** https://github.com/shreyanshcodes-hacker/shreyanshcodes-ai-automation

**Project:** Shreyansh Codes AI YouTube Automation

Your task is to audit, fix, and production-harden the existing application. Do not rebuild the project from scratch.

## 1. Most important instruction

First, inspect the complete existing repository, including:

- Backend source code
- Frontend source code
- Database models and migrations
- AI generation modules
- Video rendering and QA modules
- YouTube OAuth and upload modules
- Background job processing
- Existing tests
- Environment variables and deployment configuration
- Documentation

Understand the existing architecture before making changes.

**Preserve all working features.** Make targeted modifications, not a destructive rewrite. Do not remove existing functionality simply to make tests pass.

Create a task checklist and implement each item systematically.

## 2. Fix critical API security vulnerabilities

The current API needs authentication and authorization.

Implement:

- Secure user authentication.
- Password hashing using Argon2 or bcrypt if password-based accounts are supported.
- Secure session management or appropriately implemented JWT authentication.
- Owner/admin authorization for sensitive operations.
- Protection for job creation, viewing, retrying, cancellation, uploads, settings, and kill-switch endpoints.
- Rate limiting on expensive AI generation and upload endpoints.
- Request validation and sensible payload limits.
- Protection against unauthorized access to another user's jobs.

Do not expose API keys, OAuth tokens, provider credentials, or internal configuration through frontend responses.

For production, use secure cookies where appropriate, HTTPS, and CSRF protection for cookie-authenticated state-changing requests.

If development authentication bypasses are supported, make them explicitly development-only and impossible to enable accidentally in production.

## 3. Fix YouTube OAuth state validation

The current OAuth flow must not use a fixed state value such as `studio`.

Implement proper OAuth state security:

1. Generate a cryptographically secure random state for every OAuth attempt.
2. Bind the state to the initiating authenticated session.
3. Store it with a short expiration.
4. Validate the returned state during the OAuth callback.
5. Make the state single-use.
6. Reject missing, expired, reused, or mismatched states.
7. Validate redirect URI configuration.
8. Require HTTPS redirect URIs in production.

Add automated tests for valid and invalid OAuth callback scenarios.

Never log OAuth authorization codes, access tokens, or refresh tokens.

## 4. Fix ElevenLabs voice authorization bypass

Inspect `backend/app/production.py` and the voice authorization logic.

The authorized voice ID list must be mandatory.

Requirements:

- Consent verification must be enabled.
- Authorized voice ID list must not be empty.
- Requested voice ID must exactly match an authorized voice ID.
- Reject unauthorized voices.
- Reject missing or invalid consent configuration.
- Fail closed when environment variables are missing or malformed.

An empty allowlist must never disable authorization.

Add tests for:
- Authorized voice.
- Unauthorized voice.
- Empty allowlist.
- Missing consent.
- Invalid consent configuration.

## 5. Fix synthetic media disclosure

The current YouTube upload request must not trust a client-provided `contains_synthetic_media` boolean.

The backend must derive disclosure requirements from the actual generated production assets and ScenePlan.

Requirements:

- Track AI-generated visuals.
- Track AI-generated or synthetic voice.
- Track other relevant synthetic media.
- Persist the resulting disclosure metadata with the production job.
- Prevent the frontend from overriding disclosure requirements.
- Pass the correct disclosure information to YouTube where supported by the API.
- Do not claim that disclosure was successfully submitted unless the YouTube API confirms it.

Add tests proving that a client cannot disable a disclosure required by the generated assets.

## 6. Implement durable background job processing

The existing SQLite job records are useful, but an in-process asyncio task is not sufficient for reliable production deployment.

Implement durable job execution.

Use an appropriate architecture compatible with the existing project:

- Redis with Celery/RQ, OR
- A robust database-backed worker queue if that is more appropriate for the existing deployment.

Requirements:

- Persist pending jobs.
- Recover interrupted jobs after application restarts.
- Use atomic job claiming to prevent duplicate workers from processing the same job.
- Add worker leases or heartbeat tracking.
- Handle worker crashes.
- Support retries with exponential backoff.
- Enforce retry limits.
- Support cancellation.
- Track job progress and failure reasons.
- Make operations idempotent.
- Set concurrency limits for Gemini, Replicate, and ElevenLabs.
- Avoid duplicate YouTube uploads after retries.
- Ensure multi-worker deployments do not process the same job simultaneously.

Update deployment instructions to include any additional services.

## 7. Improve factual source verification and QA

Inspect `backend/app/qa.py` and the ScenePlan schema.

The existing check for nonempty `source_notes` is not enough to establish factual reliability.

Create structured provenance records for factual claims.

Each factual claim should support:

- Claim text.
- Source title.
- Source URL.
- Retrieval timestamp.
- Relevant supporting excerpt or evidence.
- Verification status.
- Optional reviewer notes.

Requirements:

1. Validate source URL formats.
2. Reject empty or malformed source records.
3. Check that each factual claim has supporting evidence.
4. Distinguish verified claims from claims awaiting verification.
5. Do not fabricate citations, source URLs, or evidence.
6. Flag unsupported or contradictory claims.
7. Block public scheduling if factual claims remain unverified.
8. Allow private previews for review, with appropriate warnings.
9. Store provenance records with the production job.

Do not treat a URL existing as proof that its content supports the claim.

If automatic verification is unavailable, mark the claim as requiring human review rather than pretending it has been verified.

## 8. Fix CORS configuration

Inspect `backend/app/main.py`.

Remove unrestricted wildcard CORS configuration for production.

Requirements:

- Use explicit allowed frontend origins.
- Configure allowed methods and headers narrowly.
- Enable credentials only for explicitly trusted origins.
- Validate CORS configuration at application startup.
- Separate local development and production settings.

Never combine wildcard origins with credentialed browser access.

## 9. Fix file path and storage security

Inspect all endpoints that accept or return video, subtitle, image, and other file paths.

Prevent:

- Path traversal.
- Arbitrary filesystem reads.
- Arbitrary filesystem writes.
- Access to files outside the application's storage directory.
- Unauthorized preview and download access.

Requirements:

- Resolve paths against a fixed storage root.
- Reject paths that escape the storage root.
- Prefer internal job IDs over arbitrary client-provided paths.
- Validate file types and sizes.
- Ensure each user can access only authorized job assets.
- Add tests for `../` traversal and absolute-path attacks.

## 10. Fix Replicate polling URL validation

Inspect the Replicate image generation adapter.

Never blindly send authorization headers to a polling URL received from an external provider response.

Requirements:

- Require HTTPS.
- Validate the polling URL hostname against trusted Replicate API hosts.
- Reject unexpected schemes and hosts.
- Do not forward bearer tokens to arbitrary URLs.
- Add tests for valid provider URLs and malicious polling URLs.

## 11. Improve production environment configuration

Review `.env.example` and all settings.

Add validation for required configuration.

At minimum, document and validate:

- GEMINI_API_KEY
- ELEVENLABS_API_KEY
- ELEVENLABS_VOICE_ID
- ELEVENLABS_AUTHORIZED_VOICE_IDS
- ELEVENLABS_VOICE_CONSENT_VERIFIED
- REPLICATE_API_TOKEN
- REPLICATE_MODEL_VERSION
- YOUTUBE_CLIENT_ID
- YOUTUBE_CLIENT_SECRET
- YOUTUBE_REDIRECT_URI
- DATABASE_URL or database configuration
- FRONTEND_ORIGINS
- AUTH_SECRET or equivalent secure session configuration
- STORAGE_ROOT
- REDIS_URL if Redis is used

Never commit real secrets.

Validate production settings on startup and provide clear error messages without printing secret values.

## 12. Preserve YouTube publishing safety

Keep the existing safe publishing workflow.

The following requirements must remain enforced:

- Private upload is the default.
- Public scheduling is disabled by default.
- QA must pass before scheduling.
- Factual source verification must pass before public scheduling.
- Synthetic media disclosure must be derived from actual assets.
- OAuth must be valid.
- Uploads must be idempotent.
- The kill switch must prevent publishing immediately.
- Failed uploads must not silently become public.
- Public scheduling must require explicit owner authorization.

Do not automatically enable public publishing as part of this task.

## 13. Improve video production without breaking the existing renderer

The existing renderer uses still-image slideshows.

Preserve it as a reliable fallback.

Improve the architecture so that a future motion-video generation provider can be integrated without rewriting the production pipeline.

Use clear provider interfaces for:

- Image generation.
- Motion video generation.
- Voice generation.
- Video rendering.
- Subtitles.
- Quality checks.

Do not falsely describe still-image slideshows as AI-generated motion video.

## 14. Automated tests

Add or update tests for all security and reliability fixes.

Required test areas:

- Authentication.
- Authorization and user isolation.
- Rate limiting.
- CSRF protection where applicable.
- OAuth state validation.
- Voice allowlist enforcement.
- Synthetic media disclosure.
- Durable job recovery.
- Duplicate job prevention.
- Upload idempotency.
- Provenance validation.
- Public scheduling gates.
- Path traversal.
- File authorization.
- CORS configuration.
- Replicate polling URL validation.
- Kill-switch behavior.

Use mocks for external APIs where appropriate, but clearly distinguish mocked tests from real integration tests.

Do not make tests pass by weakening security checks.

## 15. Run complete project verification

After implementing the fixes:

1. Run backend tests.
2. Run frontend tests.
3. Run linting.
4. Run type checking.
5. Run database migration checks.
6. Run backend startup checks.
7. Run frontend production build.
8. Verify FFmpeg and FFprobe availability.
9. Test the rendering pipeline with a sample production job if the environment supports it.
10. Test the YouTube workflow using a safe private-upload configuration if valid credentials are available.

If external credentials are unavailable, clearly state which integration tests could not be performed.

Do not report mocked tests as live provider tests.

## 16. Update documentation

Update:

- README.md
- `.env.example`
- Backend setup instructions.
- Frontend setup instructions.
- Google Antigravity development instructions.
- Deployment guide.
- Security configuration guide.
- YouTube OAuth setup guide.
- Background worker setup guide.
- Troubleshooting guide.

Explain how to run the system locally and deploy it safely.

## 17. Final deliverables

When implementation is complete, provide a detailed report containing:

1. Files inspected.
2. Files changed.
3. Security vulnerabilities fixed.
4. Reliability improvements.
5. Database migrations introduced.
6. New environment variables.
7. New services required.
8. Commands used to run tests.
9. Actual test results.
10. Tests that remain unverified.
11. Deployment instructions.
12. Any remaining production risks.

For each task, mark it as:
- Completed
- Partially completed
- Blocked

Do not claim completion for work that was not actually implemented or tested.

## 18. Git safety

Do not delete the repository or replace its architecture.

Do not force-push, reset existing history, or overwrite unrelated work.

Do not commit or push changes unless I explicitly request it.

First inspect the project, then implement the fixes, run verification, and provide the final report.

**Primary objective:** Make Shreyansh Codes AI YouTube Automation secure, reliable, recoverable, and safe for real production use while preserving its existing Gemini, ElevenLabs, Replicate, FFmpeg, React, and YouTube functionality.