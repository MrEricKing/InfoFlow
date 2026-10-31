# Operations

This document owns supported deployment, startup, backup/restore, validation, recovery, and operator procedures. Troubleshooting symptoms and source locators live in [`troubleshooting.md`](troubleshooting.md); security-sensitive operating rules live in [`security.md`](security.md).

## Environments

| Environment | Expected shape |
| --- | --- |
| Local development | Current repository worktree, project Conda environment `infoflowaggregator`, local PostgreSQL development container, Vite dev server |
| Isolated checkout | The current worktree's backend cwd and worktree-local frontend dependencies; do not import backend code from another checkout |
| Personal deployment | Windows host with Docker Desktop, PowerShell operator shell, deployment bundle normally at `C:\Apps\InfoFlow` |

Local or worktree evidence is not production evidence. Production/container symptoms should be diagnosed on the deployment machine unless the owner explicitly says the issue is local.

## Deployment Topology

The supported deployment bundle is `deploy/docker-compose.yml` plus `deploy/.env.example` and `deploy/update.ps1`. It runs:

- one application image serving FastAPI and the built Vite SPA;
- PostgreSQL 16 as the canonical store;
- RSSHub with Chromium support;
- Redis only as RSSHub's cache backend, not as an application cache or job broker.

The application container runs `alembic upgrade head` before Uvicorn. Capture runtimes are included in the application image. The app-local PostgreSQL job queue and workers run inside the backend process; there is no required external app worker, scheduler, cache, object store, or second database.

## Initial Deployment

From the deployment bundle in PowerShell:

```powershell
Copy-Item .env.example .env
# Edit .env before startup.
docker compose up -d
```

Required values include PostgreSQL credentials, explicit production API acknowledgement, allowed CORS origins, encryption key, published image tag, and host storage paths. Optional AI keys and RSSHub route credentials remain secrets.

Health check:

```text
http://127.0.0.1:5173/api/health
```

Inspect service state:

```powershell
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
docker compose ps
docker compose logs --tail 200 app
```

## Updating A Published Image

Run from the deployment directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1
```

When the PostgreSQL Compose service is running, the update script writes a PostgreSQL dump before pulling and recreating containers and fails if `pg_dump` returns an error. If that service is not running, the script only prints a warning and continues without creating a backup. Before updating, the operator must either verify an existing suitable dump or bring PostgreSQL up and create one. Protect the backup path as sensitive data.

## Backup, Restore, And Portability

- PostgreSQL is the only complete backup source. A full dump includes subscriptions, items, states, saved material, snapshots, assets, settings, operations, and diagnostic history.
- OPML is subscription portability only. It does not include saved items, reading state, AI settings, captures, uploads, local mirror exports, operation history, or provider settings.
- `KnowledgeBase/` is a rebuildable mirror and is not a database backup.
- Preserve database dumps before destructive schema, deployment, or data-repair work. Backup files may contain personal content and encrypted provider settings.
- The current compatibility boundary supports a fresh v3 database plus OPML subscription import; it does not support in-place upgrade of old v2 production databases.
- The repository does not currently provide an automated, validated restore command or restore-drill evidence. The clean-database cutover checklist below is operator guidance, not proof that a dump has been restored successfully.

Clean-database cutover:

1. Create and verify a PostgreSQL backup.
2. Export OPML subscriptions from the old deployment.
3. Start a clean supported database.
4. Import OPML through preview plus commit.
5. Reconfigure secrets and AI provider settings.
6. Run source refresh and verify AI home, reader, capture, saved-item insight, and mirror workflows.

## Local Development

Use the existing named Conda environment; do not create an environment inside the worktree.

```powershell
npm --prefix frontend ci
npm run dev
```

The root dev script starts or reuses the local PostgreSQL container, runs the backend with restart support, and starts Vite. Stop the stack with:

```powershell
npm run stop
```

If a development database predates the supported schema and cannot be migrated under the current compatibility contract, reset that development database explicitly rather than stamping Alembic state manually.

## Validation Commands

Backend commands that import project code must run with `backend/` as cwd through the project runner.

```powershell
npm run lint:backend
npm run test:backend -- -q
npm run test:backend:postgres -- -q
npm run lint:client
npm run test:frontend:logic
npm run typecheck:client
npm run build:client
```

Targeted backend tests must use the parallel wrapper:

```powershell
npm run test:backend:parallel -- tests/path_or_nodeid -q
```

Local default worker counts are fixed by project scripts. Serial variants are available for diagnosing ordering or xdist behavior.

## Operation And Worker Recovery

- Operation creation routes persist operation/job rows, commit, wake the in-process worker, and return without draining jobs inline.
- `app_operations` remains the frontend progress/result contract. `jobs` and `job_events` own claim, lease, retry, cancellation, and child execution for job-backed operation families.
- Stop APIs write `stop_requested` / `stopping` in a short transaction and wake the worker. The worker owns child cancellation, source-refresh artifact stop forwarding, and terminal fan-in.
- Stop row-lock timeout returns a visible conflict rather than hanging indefinitely.
- Active job-backed operations recover from job state and lease reclaim. Legacy same-process operations may become interrupted after restart and require an explicit retry.
- AI provider jobs are claimed by the AI lane worker; generic operation paths enqueue/synchronize/wake them but do not consume provider slots directly.

Start diagnosis with:

```text
GET /api/operations/active
GET /api/operations/active-list
```

Then inspect `app_operations`, `jobs`, and `job_events` using the symptom-specific guidance in [`troubleshooting.md`](troubleshooting.md).

## PostgreSQL Diagnostics On Windows

Discover actual container names before tailoring SQL commands:

```powershell
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
```

Prefer piping SQL through stdin to avoid PowerShell quoting failures:

```powershell
@"
select pid, state, now() - query_start as age, left(query, 500) as query
from pg_stat_activity
where datname = current_database()
order by query_start nulls last;
"@ | docker exec -i infoflowaggregator-postgres psql -U postgres -d infoflowaggregator
```

Do not assume database, user, or container names when deployment discovery shows different values.

## Maintenance And Rebuilds

Feed content extraction maintenance:

```powershell
npm run py:backend -- -m app.cli.feed_content_extraction_maintenance retry
npm run py:backend -- -m app.cli.feed_content_extraction_maintenance export
npm run py:backend -- -m app.cli.feed_content_extraction_maintenance probe-url <url>
```

Rebuild the local mirror from PostgreSQL:

```powershell
npm run py:backend -- -m app.cli.rebuild_local_mirror
```

Never repair canonical state by editing `KnowledgeBase/` or `llm_wiki/`. Validate and repair PostgreSQL or the owning import/rebuild path instead.

## 本机验证调度

公开 npm 测试、构建、类型检查与 lint 入口通过 `scripts/host-validation.mjs` 接入显式配置的宿主机共享验证名额。其他项目或工作树占用时等待，嵌套 npm 调用复用当前运行；原命令、参数、工作目录和并行 worker 不变。每次顶层运行使用独立日志目录，不共用固定重定向文件。配置了入口但工具不可用时明确失败；未配置的其他主机保持原有执行环境。
