# InfoFlowAggregator

个人 RSS、AI 阅读、网页归档和知识库应用。前端使用 React、Vite 和 TypeScript，后端使用 FastAPI，PostgreSQL 保存应用状态与归档资料。

## 功能

- RSS、RSSHub 订阅，OPML 导入与导出，来源分类与图标。
- AI 首页、经典阅读器、频道筛选、阅读状态和人工反馈。
- 文章摘要、主题、路由、评分，以及模型配置和调用诊断。
- 保存文章、捕获 URL、上传 Markdown／HTML／SingleFile HTML、搜索与备注。
- SingleFile 网页归档、Crawl4AI 正文提取，以及可重建的本地知识库镜像。
- 后台任务的进度、停止、重试与重启恢复。
- 阅读反馈和推荐配置的证据包导出与候选导入。

PostgreSQL 是完整数据的来源。`KnowledgeBase/` 是可重建的导出目录；OPML 只包含订阅源和文件夹，完整备份需要 PostgreSQL dump。

## 本地开发

需要 Node.js、Conda，以及 Docker Desktop 或 Docker Engine。

```bash
conda env create -f environment.yml
conda activate infoflowaggregator
npm --prefix frontend ci
npm run dev
```

已有 `infoflowaggregator` 环境时直接使用该环境。开发入口启动或复用本地 PostgreSQL 容器，再启动后端与 Vite。默认前端地址为 `http://localhost:5173`，后端地址为 `http://localhost:8000`。

```bash
npm run stop
```

## 检查

```bash
npm run lint
npm run test:backend -- -q
npm run test:backend:postgres -- -q
npm run test:frontend:logic
npm run typecheck:client
npm run build:client
```

后端命令通过项目 Python 入口选择环境，并从 `backend/` 加载应用。普通后端测试使用 4 个 worker，PostgreSQL 集成测试使用 6 个 worker。小范围测试可运行：

```bash
npm run test:backend:parallel -- tests/test_config.py -q
```

## 部署

`Dockerfile` 构建同时提供前后端的应用镜像，`deploy/` 提供应用、PostgreSQL、RSSHub 和 Redis 的 Compose 配置。Redis 仅用于 RSSHub 缓存。

在部署目录的 PowerShell 中执行：

```powershell
Copy-Item .env.example .env
# 配置镜像、数据库密码、端口、CORS、加密密钥和数据路径。
docker compose up -d
```

容器启动时执行 Alembic 迁移。默认健康检查地址为 `http://127.0.0.1:5173/api/health`。

应用不提供用户登录，生产环境依赖外部访问控制，并要求显式配置 `INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API=true`。完整配置、备份和更新步骤见[运维文档](docs/operations.md)与[安全边界](docs/security.md)。

## 目录

| 路径 | 内容 |
| --- | --- |
| `backend/` | FastAPI 应用、数据模型、迁移与测试 |
| `frontend/` | 页面、组件、状态管理和 API 客户端 |
| `scripts/` | 开发启动、Python 环境与检查入口 |
| `deploy/` | Compose 配置、环境模板和更新脚本 |
| `docker/` | 容器启动脚本 |
| `vendor/` | 应用使用的第三方运行时及许可证 |
| `docs/` | 产品、架构、接口、数据、配置和运维说明 |

文档入口：[docs/README.md](docs/README.md)。源码定位：[目录地图](docs/directory-map.md)。
