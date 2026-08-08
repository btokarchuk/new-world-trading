# Trading stack image: engine + risk + research + watchdog, one image, many
# entrypoints (compose picks the command per service).
#
# Isolation intent (Phase 4): the trading code must not run with the operator's
# full user permissions. Inside here it is a non-root user with a read-only view
# of config and secrets, a single writable data volume, and nothing else.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer: only manifests, so code edits don't re-resolve the world.
COPY pyproject.toml uv.lock ./
COPY contracts/pyproject.toml contracts/
COPY engine/pyproject.toml engine/
COPY research/pyproject.toml research/
COPY risk/pyproject.toml risk/
COPY watchdog/pyproject.toml watchdog/
COPY backend/pyproject.toml backend/
RUN mkdir -p contracts/src/nwt_contracts engine/src/nwt_engine \
             research/src/nwt_research risk/src/nwt_risk watchdog/src/nwt_watchdog \
             backend/src/nwt_backend \
    && touch contracts/src/nwt_contracts/__init__.py engine/src/nwt_engine/__init__.py \
             research/src/nwt_research/__init__.py risk/src/nwt_risk/__init__.py \
             watchdog/src/nwt_watchdog/__init__.py backend/src/nwt_backend/__init__.py \
    && uv sync --all-packages --no-dev --frozen

# Source layer.
COPY contracts/ contracts/
COPY engine/ engine/
COPY research/ research/
COPY risk/ risk/
COPY watchdog/ watchdog/
COPY backend/ backend/
RUN uv sync --all-packages --no-dev --frozen

# Non-root. uid 1000 keeps bind-mount writes sane on Linux hosts; macOS bind
# mounts are uid-mapped by the VM layer so it is a no-op there.
RUN useradd --uid 1000 --create-home --shell /bin/bash nwt \
    && mkdir -p /app/data \
    && chown -R nwt:nwt /app /opt/venv
USER nwt

COPY deploy/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
# Idle by default so a bare `docker run` lands somewhere safe to exec into; the
# real commands are set per service in docker-compose.yml (engine: `nwt-risk
# run`, watchdog: `nwt-watchdog`).
CMD ["sleep", "infinity"]
