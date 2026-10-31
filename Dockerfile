# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
ENV VITE_API_BASE_URL=/api
RUN npm run build


FROM python:3.11-slim-bookworm AS backend-build

WORKDIR /build/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN python -m pip install --upgrade pip \
    && python -m pip wheel --no-deps --wheel-dir /wheels .


FROM python:3.11-slim-bookworm AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    API_PREFIX=/api \
    STATIC_FILES_DIR=/app/frontend-dist \
    SINGLEFILE_VERSION=1.22.98 \
    SINGLEFILE_EXTENSION_PATH=/app/vendor/singlefile-mv3 \
    SINGLEFILE_EXTENSION_ID=jkeljiefjnokjkioahopjglagkbocpbh \
    SINGLEFILE_BROWSER_EXECUTABLE_PATH=/usr/local/bin/playwright-chromium \
    SINGLEFILE_CAPTURE_TIMEOUT_SECONDS=120 \
    CRAWL4AI_VERSION=0.8.6 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    KNOWLEDGE_BASE_ROOT=/data/KnowledgeBase

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini xvfb fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY --link backend/requirements-runtime.txt ./requirements-runtime.txt
RUN python -m pip install --no-cache-dir --no-compile -r requirements-runtime.txt \
    && find /usr/local/lib/python3.11/site-packages -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

RUN python -m playwright install --with-deps chromium \
    && chromium_path="$(find /ms-playwright -path '*/chrome-linux*/chrome' -type f | head -n 1)" \
    && test -n "$chromium_path" \
    && ln -sf "$chromium_path" /usr/local/bin/playwright-chromium \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,from=backend-build,source=/wheels,target=/wheels,ro \
    python -m pip install --no-cache-dir --no-compile --no-deps /wheels/*.whl

COPY --link backend/alembic.ini ./alembic.ini
COPY --link backend/alembic ./alembic
COPY --link vendor/singlefile-mv3 ./vendor/singlefile-mv3
COPY --link --from=frontend-build /app/frontend/dist ./frontend-dist
COPY --link --chmod=755 docker/app-entrypoint.sh ./app-entrypoint.sh

RUN mkdir -p /data/KnowledgeBase

ARG INFOFLOW_GIT_COMMIT=""
ARG INFOFLOW_GIT_DIRTY=""
ENV INFOFLOW_GIT_COMMIT=${INFOFLOW_GIT_COMMIT} \
    INFOFLOW_GIT_DIRTY=${INFOFLOW_GIT_DIRTY}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/app-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
