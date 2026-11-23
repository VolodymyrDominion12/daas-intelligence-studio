# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    DAAS_HOST=0.0.0.0 \
    DAAS_PORT=8000 \
    DAAS_STORAGE_DIR=/app/storage \
    PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app

# Install backend dependencies first for layer caching (editable install preserves PROJECT_ROOT=/app).
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev --no-install-project

COPY backend/app ./backend/app
RUN cd backend && uv sync --frozen --no-dev

COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

RUN mkdir -p /app/storage && useradd --system --uid 10001 --home /app daas \
    && chown -R daas:daas /app/storage

USER daas
WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["daas", "serve", "--host", "0.0.0.0", "--port", "8000"]
