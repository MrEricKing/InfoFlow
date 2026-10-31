# Security And Privacy Boundaries

This document owns the current security, privacy, secret-handling, remote-fetch, and diagnostic-retention boundaries for InfoFlowAggregator.

## Access Model

- The application is a personal single-user system and does not currently implement app-layer authentication or multi-user authorization.
- Production startup requires `INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API=true` as an explicit acknowledgement that external access control protects the API.
- Production FastAPI OpenAPI and documentation routes are disabled.
- `CORS_ORIGINS` must list explicit origins in production; wildcard production CORS is rejected.
- RSSHub's optional host debug port binds to `127.0.0.1` by default. Expanding `RSSHUB_HOST_BIND` increases the exposure surface and must be intentional.

## Secret Ownership

- `INFOFLOW_SETTINGS_ENCRYPTION_KEY` encrypts AI provider keys saved through Settings. Stored encrypted values use the `enc:v1:` envelope.
- Production startup fails when plaintext AI key settings remain and no valid encryption key is available. Startup may upgrade plaintext rows only when the encryption key is present.
- `INFOFLOW_AI_GENERAL_API_KEY` and `INFOFLOW_AI_SENSITIVE_API_KEY` are process-environment overrides. They do not rewrite database rows.
- Settings APIs return masked/configured key state, never full provider keys. The browser does not receive full AI provider keys and does not call providers directly.
- RSSHub route credentials such as `TWITTER_AUTH_TOKEN`, `YOUTUBE_KEY`, cookies, and route API keys belong in the deployment `.env` and are passed only to the RSSHub service.
- Real secrets, cookies, tokens, encrypted values, and production `.env` files must not be committed.

Primary owners are `backend/app/core/security.py`, `backend/app/services/app_settings.py`, `backend/app/services/ai_provider.py`, `backend/app/api/routes/settings.py`, `deploy/.env.example`, and `deploy/docker-compose.yml`.

## Remote Fetch And SSRF Boundary

- `backend/app/core/safety.py` owns public-HTTP URL validation, DNS/IP checks, redirect validation, response-size limits, and shared SSRF-guarded fetching.
- Source preview, source sync, reader extraction, favicon discovery, and capture entrypoints must use the established safety boundary rather than issuing unguarded user-controlled requests.
- Requests performed through the shared server-side HTTP fetch boundary reject private, loopback, link-local, multicast, and otherwise disallowed targets. Its server-side HTTP redirects are revalidated at every hop, and response/XML payloads are bounded before parsing.
- SingleFile and Crawl4AI validate the initial URL and a server-side HTTP redirect chain before launching a browser. The browser contexts do not currently intercept and revalidate every client-side navigation, iframe, or subresource request. Treat browser capture as unsuitable for untrusted public callers. Browser request interception or network isolation is needed for that use.
- RSS/Atom and OPML XML use hardened parsing. A malformed or unsafe payload fails visibly; the application does not silently repair a broken external contract.
- HTTP access failures, Cloudflare/anti-bot responses, dead links, and content-quality failures remain diagnostically distinct. RSSHub credentials are not injected into arbitrary source-page extraction.

Primary owners are `backend/app/core/safety.py`, `backend/app/services/source_preview.py`, `backend/app/services/source_sync.py`, `backend/app/services/feed_content_extraction.py`, and capture wrappers under `backend/app/services/capture_tools/`.

## Upload And Imported-Configuration Boundary

- Markdown, HTML, and SingleFile HTML uploads are bounded UTF-8 text inputs. The current text upload limit is 25 MB.
- Upload kind validation currently accepts a matching filename extension or a matching MIME hint; it does not require the two to agree or inspect the bytes to determine the actual content type. Uploads therefore remain trusted single-user inputs, not a safe hostile-file boundary.
- Original uploaded bytes are stored before derived processing. Failed replacement imports preserve the last successful current material and expose the new failure rather than silently switching pointers.
- Imported Insight Profile and Source Profile candidates are validated as untrusted structured input. Inspection remains dry-run until an explicit save/apply action, and activation remains separate from draft creation.
- Operation and job payload validation rejects secret-bearing or unsafe diagnostic fields before persistence.

## Privacy And Diagnostic Retention

- PostgreSQL is canonical and intentionally stores raw source/import material needed for replay, debugging, export, and recovery.
- Source deletion removes source-owned current content according to the confirmed delete contract, while saved Knowledge items are retained. Historical operation/job/attempt rows remain audit evidence; structured source/feed-item references are detached where required.
- AI-derived artifacts are recomputable. User-authored notes, curated insights, structured feedback, state, and saved material are human/canonical evidence and must not be overwritten by regenerated AI output.
- Evidence Package exports are sanitized and bounded. They must not expose full provider keys, encryption material, unrestricted operation payloads, or unrelated private data.
- Application logs and diagnostic payloads use the redaction helpers in `backend/app/core/security.py`. New diagnostic fields must be reviewed for secrets and personal content before persistence or export.
- PostgreSQL dumps contain application state and may include encrypted provider settings or other personal material. Treat backup files and backup directories as sensitive data.
- Raw source/import material, operation/job/attempt history, and behavior evidence do not currently have an automatic TTL, retention cap, or supported cleanup command. Monitor database and backup growth.

## Browser Content Boundary

- Captured or feed-provided HTML is sanitized before normal application rendering.
- Asset and snapshot content routes set `X-Content-Type-Options: nosniff` and private cache headers. `download=true` adds an attachment disposition.
- Inline assets receive a sandbox Content Security Policy only when the stored MIME type is `text/html` or `application/xhtml+xml`. Other stored MIME types, including potentially active SVG content accepted through an extension/MIME mismatch, are not currently forced to attachment or sandboxed; do not expose uploads to untrusted callers.
- The local `KnowledgeBase/` mirror is an export of trusted canonical state, not an ingestion authority. Editing mirror files does not change PostgreSQL.
