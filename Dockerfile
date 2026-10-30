# ACR seller API — x402-gated FastAPI service (services/index_api).
# Build:  docker build -t acr-api .
# Run:    docker run --rm -p 8000:8000 --env-file .env acr-api
# All runtime config is ACR_* env vars (see .env.example); with none set the
# service comes up credential-free on the dev gate + simulator tape.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Lockfile + workspace sources (the uv workspace is packages/* + services/*;
# apps/ and contracts/ are not Python members and stay out of the image).
COPY pyproject.toml uv.lock README.md ./
COPY packages/ packages/
COPY services/ services/
# The reference levels and the market baskets (anchors/). `par.market_basket`
# reads them at request time and `anchors-check` gates them, so a cheap way to
# ship a broken image is to leave this out: the basket then reports ABSENT, every
# bill falls back to fleet quotes, and nothing errors. The failure is a quieter
# benchmark, not a crash — which is exactly how it went unnoticed once before,
# when an image without this directory posted a bound of 0 for a month while the
# same code computed a real figure locally.
COPY anchors/ anchors/

# `--extra circle` pulls the Circle Developer-Controlled Wallets SDK so the oracle
# poster can sign+relay prints under Circle custody in-cloud. `--extra armor`
# pulls google-auth, without which ModelArmorScreen cannot mint a bearer token
# and build_screen silently falls back to the offline LocalScreen floor — the
# image reporting a screen it does not have, which is the exact "looks identical
# from outside" failure /armor/info exists to prevent. Default on; build with
# `--build-arg UV_EXTRAS=` for a lighter minimal image (no in-cloud posting, no
# Model Armor).
ARG UV_EXTRAS="--extra circle --extra armor"
RUN uv sync --frozen --no-dev ${UV_EXTRAS} && rm -rf /root/.cache/uv

# Non-root runtime user; the sync above ran as root so site-packages are owned
# read-only from the app user's perspective — the API only writes under
# ACR_WEBHOOK_LOG_PATH, which /app/data keeps writable.
RUN useradd --create-home --uid 10001 acr \
    && mkdir -p /app/data \
    && chown -R acr:acr /app
USER acr

ENV PORT=8000
# glibc gives every thread its own malloc arena (up to 8 x cores); the heavy jobs
# run on asyncio's thread pool, and the arenas' fragmentation is what kept the
# hourly press's ~150 MiB transient resident until the 512 MiB kill on
# 2026-09-13. Two arenas is the usual setting for a small container.
ENV MALLOC_ARENA_MAX=2
EXPOSE 8000

# /health also reports gate/signer/tape provenance; the generous start period
# covers the first store build (simulator + estimation) on small instances.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python3", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=8).status==200 else 1)"]

# Shell form so $PORT from the platform (Cloud Run, Railway, Render…) wins.
CMD ["sh", "-c", "uv run --no-sync uvicorn index_api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
