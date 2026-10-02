# syntax=docker/dockerfile:1.7
# Build context: repository root (the API depends on packages/device-protocol).

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN pip install "poetry>=2.2,<3"

RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

WORKDIR /build
# Third-party dependencies first: this layer only rebuilds when the lock file changes.
COPY apps/api/pyproject.toml apps/api/poetry.lock apps/api/
COPY packages/device-protocol/pyproject.toml packages/device-protocol/
RUN cd apps/api && poetry install --only main --no-root --no-directory

# Our own code, installed non-editable so the venv is self-contained.
COPY packages/device-protocol packages/device-protocol
COPY apps/api/src apps/api/src
COPY apps/api/README.md* apps/api/
RUN pip install --no-deps ./packages/device-protocol ./apps/api


FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

# The slim base lags behind Debian security fixes; pick them up at build time so the
# Trivy gate in CI reflects our image, not the upstream release cadence.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "smarthome.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
