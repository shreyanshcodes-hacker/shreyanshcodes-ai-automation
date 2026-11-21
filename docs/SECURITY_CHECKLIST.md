# Security & Compliance Checklist

- [ ] **Server-Side Token Storage**: YouTube OAuth tokens are stored server-side (`YOUTUBE_TOKEN_FILE`, `0600` permissions) and never exposed in API responses or logs.
- [ ] **No Secrets in Git**: `.env`, `storage/`, and `storage/tokens/` are excluded from version control.
- [ ] **Authorized Voice Only**: `ELEVENLABS_VOICE_CONSENT_VERIFIED=true` and `ELEVENLABS_AUTHORIZED_VOICE_IDS` allowlist enforced before TTS calls; no unauthorized voice cloning.
- [ ] **Originality & Provenance**: Every generated asset is hashed (SHA-256) and stored with provenance metadata in `studio.db`.
- [ ] **Synthetic Media Disclosure**: `containsSyntheticMedia=true` is set on YouTube uploads when AI voice or AI visuals are used.
- [ ] **Private-First & QA Gate**: Videos always upload as `private` first; `publishAt` scheduling is blocked unless QA passes, source notes exist, kill switch is OFF, and `YOUTUBE_PUBLIC_SCHEDULING_ENABLED=true` is explicitly set by the owner.
- [ ] **Kill Switch**: `POST /api/youtube/kill-switch` (`{"enabled": true}`) immediately halts all YouTube uploads.
- [ ] **Quota-Aware Retry**: `quotaExceeded` (403) halts retries immediately to avoid API quota exhaustion.
