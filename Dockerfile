# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the virtualenv with the runtime dependencies only
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ARG POETRY_VERSION=2.1.3

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# Poetry lives in its own venv and never reaches the final image
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install "poetry==${POETRY_VERSION}" \
    && python -m venv /opt/venv

WORKDIR /build
# Only the dependency manifests, so this layer stays cached until they change
COPY pyproject.toml poetry.lock ./
# Installs into the active venv (VIRTUAL_ENV); dev dependencies are skipped.
# The Claude Code CLI is bundled with claude-agent-sdk, no Node.js required.
RUN /opt/poetry/bin/poetry install --only main --no-root


# ---------------------------------------------------------------------------
# Stage 2: slim runtime image, running as an unprivileged user
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# UID/GID of the runtime user. Only relevant for bind-mounted directories
# (e.g. a CLAUDE_CWD workspace), which must be writable by this user:
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# Real home directory: the Claude CLI keeps its runtime state in ~/.claude and ~/.claude.json.
# Authentication: login via API (/v1/claude-login), CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY.
# ~/.claude-wrapper holds the token from an API login (/v1/claude-login); mount a volume
# there to keep it across container re-creation. Created here so the volume is app-owned.
RUN groupadd --gid "${GID}" app \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin app \
    && install -d -m 700 -o app -g app /home/app/.claude-wrapper

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Root-owned and read-only for the app user: the service never modifies its own code
COPY src ./src

USER app

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
