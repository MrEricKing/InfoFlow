# Directory Map

Use this map to find the owning boundary before opening source. For symptom-specific routing, use [`troubleshooting.md`](troubleshooting.md). For semantic behavior, read the smallest owning current document listed in [`README.md`](README.md).

## Top-Level Boundaries

| Path | Owns | Read first when |
| --- | --- | --- |
| `README.md` | Human-oriented project overview and onboarding | Explaining, deploying, or sharing the project |
| `docs/` | Product, architecture, data, API, configuration, and operations documentation | Locating behavior or durable documentation ownership |
| `backend/` | FastAPI, domain services, SQLAlchemy, Alembic, and backend tests | API, persistence, ingestion, jobs, capture, AI, or operations |
| `frontend/` | React/Vite UI, state orchestration, API adapters, and view models | UI, interaction state, client contracts, or layout |
| `deploy/` | Copyable personal deployment bundle | Docker Compose deployment or target-machine update |
| `docker/` | Application image startup support | Alembic-on-start or container entrypoint behavior |
| `scripts/` | Development, Python-runner, test, and release helpers | Supported command behavior |
| `vendor/` | Pinned third-party runtime assets | SingleFile runtime provenance or upgrades |
| `KnowledgeBase/` | Rebuildable local mirror | Inspecting exports only; never canonical repair |
| `llm_wiki/` | Reserved future derived workspace | Future LLM Wiki work only |

## Backend Boundaries

| Path | Owns |
| --- | --- |
| `backend/app/main.py` | FastAPI app lifecycle, middleware, production SPA/static serving |
| `backend/app/api/router.py` | Main API composition |
| `backend/app/api/routes/` | HTTP request validation and transaction entrypoints |
| `backend/app/schemas/` | Pydantic request/response contracts |
| `backend/app/core/config.py` | Environment/runtime settings loading |
| `backend/app/core/security.py` | Secret encryption, masking, redaction, and persisted payload validation |
| `backend/app/core/safety.py` | Safe XML parsing, public URL/redirect checks, SSRF guards, and byte caps |
| `backend/app/db/models.py` | SQLAlchemy canonical and derived table definitions |
| `backend/app/db/session.py` | Engine/session ownership |
| `backend/alembic/versions/` | Database migration history and executable schema transitions |
| `backend/app/services/source_*` | Source identity, preview, sync, icons, deletion, and preference profiles |
| `backend/app/services/reader.py`, `structured_feedback.py`, `behavior_events.py` | Reader state and human evidence |
| `backend/app/services/homepage*.py`, `insight_*.py` | Homepage projections, AI artifacts, routing, scoring, profiles, diagnostics, and evidence export |
| `backend/app/services/operations.py`, `operation_jobs.py`, `job_queue.py` | Visible operations, child jobs, workers, stop/recovery, and terminal fan-in |
| `backend/app/services/knowledge_*.py`, `saved_item_insights.py` | Saved knowledge, capture/upload, snapshots/assets, mirror, search, and curated insight |
| `backend/app/services/capture_tools/` | SingleFile and Crawl4AI invocation boundaries |
| `backend/app/cli/` | Supported maintenance and rebuild commands |
| `backend/tests/` | Backend behavior and contract tests |

Detailed backend responsibility mapping lives in [`components.md`](components.md). Data ownership and state values live in [`data.md`](data.md).

## Frontend Boundaries

| Path | Owns |
| --- | --- |
| `frontend/src/App.tsx` | Application shell composition and pane layout |
| `frontend/src/hooks/use-info-flow-app.ts` | Top-level view and workflow orchestration |
| `frontend/src/hooks/info-flow/` | Focused homepage, operation, source-editor, and helper controllers |
| `frontend/src/components/` | Presentational and workflow components |
| `frontend/src/components/detail-pane/` | Reader and Knowledge detail subviews |
| `frontend/src/services/` | HTTP adapters and DTO-to-view-model mapping |
| `frontend/src/api/` | Shared fetch client and normalized API errors |
| `frontend/src/types/` | TypeScript DTO and view-model types |
| `frontend/src/lib/` | Pure client-side utilities and filters |
| `frontend/public/` | Static assets |
| `frontend/vite.config.ts` | Vite build/dev configuration and aliases |

Detailed view-to-data and component ownership lives in [`components.md`](components.md). HTTP alignment lives in [`api.md`](api.md).

## Deployment And Command Boundaries

| Path | Owns |
| --- | --- |
| `deploy/docker-compose.yml` | Supported app/PostgreSQL/RSSHub/Redis personal deployment topology |
| `deploy/.env.example` | Deployment configuration template |
| `deploy/update.ps1` | Backup-before-update, pull, and recreate workflow |
| `docker/app-entrypoint.sh` | Alembic upgrade then Uvicorn startup |
| `Dockerfile` | Single application image and capture runtime packaging |
| `backend/requirements-runtime.txt` | Docker runtime Python dependency input |
| `scripts/dev.mjs`, `scripts/dev-stop.mjs` | Local development startup and targeted cleanup |
| `scripts/host-validation.mjs` | Public npm validation commands joining the configured host slot |
| `scripts/python.mjs` | Resolution of the existing `infoflowaggregator` Conda Python and backend cwd |
| `package.json` backend test scripts | Fixed-worker local backend pytest wrappers built on `scripts/python.mjs` |

Supported operator procedures live in [`operations.md`](operations.md); settings and constants live in [`configuration.md`](configuration.md).

## Documentation

[`README.md`](README.md) indexes the product, architecture, API, data, configuration, security, operations, component, and troubleshooting documents.

## Generated Or Local Material

Do not scan these first unless the generated artifact itself is the subject:

- `frontend/node_modules/`, `frontend/dist/`;
- backend caches, temporary output, and editable-install metadata;
- Docker caches, images, containers, networks, and volumes;
- `KnowledgeBase/**/assets/**`, `KnowledgeBase/**/.infoflow/**`, generated mirror Markdown/HTML;
- local `llm_wiki/` output;
- browser, test, upload, and capture caches.
