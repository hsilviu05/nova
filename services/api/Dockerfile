# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into a self-contained virtualenv.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed to compile argon2-cffi's bindings; it stays in the
# builder stage and never reaches the runtime image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the manifest first so the dependency layer is cached across
# source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# ---------------------------------------------------------------------------
# Runtime — no compiler, no build tools, non-root.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 nova

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=nova:nova alembic.ini ./
COPY --chown=nova:nova alembic ./alembic
COPY --chown=nova:nova src ./src

USER nova
EXPOSE 8000

# Hits the liveness probe, which by design touches no dependency — so this
# reports on the process, not on Postgres.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

CMD ["uvicorn", "nova.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
