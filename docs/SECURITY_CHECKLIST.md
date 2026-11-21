# Security & Compliance Checklist

- [ ] **Authentication & RBAC**: All API endpoints (except `/health` and `/api/auth/register|login|logout`) require valid JWT/cookie authentication; owner/admin role required for settings, OAuth, uploads, and kill switch.
- [ ] **CSRF & Cookies**: Cookie-authenticated state-changing requests enforce `X-CSRF-Token` matching `sc_csrf`; `HttpOnly`, `SameSite=Lax`, and `Secure` (in production) flags are set.
- [ ] **CORS Hardening**: Explicit `FRONTEND_ORIGINS` required; wildcard `*` with credentials is forbidden and validated at startup.
- [ ] **Path Traversal Protection**: All file paths are resolved against `OUTPUT_DIR` (`storage_root`), rejecting `..` traversal, null bytes, and outside-root paths.
- [ ] **OAuth State Security**: `get_oauth_authorization_url()` generates a 32-byte random `state` bound to the authenticated user with a 10-minute TTL and single-use atomic consumption.
- [ ] **Voice Authorization (Fail-Closed)**: `ELEVENLABS_VOICE_CONSENT_VERIFIED=true` and a non-empty `ELEVENLABS_AUTHORIZED_VOICE_IDS` allowlist are mandatory.
- [ ] **Replicate SSRF/Token Protection**: Polling URLs must be `https://api.replicate.com/...` before sending `Authorization: Bearer`.
- [ ] **Derived Synthetic Media Disclosure**: Disclosure is derived from actual generated assets & `ScenePlan`, cannot be disabled by the client, and is only marked confirmed when YouTube API confirms `status.containsSyntheticMedia=true`.
- [ ] **Private-First & Provenance Gate**: Videos upload `private` first; `publishAt` requires QA pass, verified `ClaimProvenance` records, kill switch OFF, and `YOUTUBE_PUBLIC_SCHEDULING_ENABLED=true`.
