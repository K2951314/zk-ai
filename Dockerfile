# ===========================================================================
# ZK-AI - production-ish image.
# Base: official uv image so dependency resolution matches local development.
# ===========================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer (cached unless pyproject/lock changes).
COPY pyproject.toml uv.lock* README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Project layer.
COPY app ./app
COPY config ./config
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    ZKAI_HOST=0.0.0.0 \
    ZKAI_PORT=8317 \
    ZKAI_DATA_DIR=/app/data \
    ZKAI_CONFIG_DIR=/app/config

# Non-root user.
RUN groupadd --system --gid 1001 zkai \
    && useradd --system --uid 1001 --gid zkai --create-home zkai

WORKDIR /app
COPY --from=builder --chown=zkai:zkai /app /app
RUN mkdir -p /app/data && chown -R zkai:zkai /app/data

USER zkai
EXPOSE 8317

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8317/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8317"]
