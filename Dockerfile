# =============================================================================
#  DevBrain application image
#
#  Multi-stage build: dependencies are compiled in a full Python image, then
#  only the finished virtualenv is copied into a slim runtime image. That keeps
#  the shipped image small and free of compilers.
#
#  Build and run through Compose (recommended):
#      docker compose --profile full up -d --build
#
#  Or standalone:
#      docker build -t devbrain-app .
#      docker run --rm -p 8001:8001 --env-file .env devbrain-app
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder: install dependencies into a self-contained virtualenv
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Never use alpine for Python: musl libc forces source builds of packages that
# already ship manylinux wheels, making images slower to build and often slower
# at runtime.

WORKDIR /build

# Copy ONLY requirements.txt first. Docker caches each instruction as a layer,
# so as long as this file is unchanged the (slow) pip install below is reused
# even when application code changes. Copying the code first would invalidate
# the dependency layer on every single edit.
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2 — runtime: slim image containing only the venv and the app
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Run as a non-root user. If the application is ever compromised, the attacker
# lands as an unprivileged account rather than as root inside the container.
RUN groupadd --system devbrain \
 && useradd --system --gid devbrain --home-dir /app --shell /usr/sbin/nologin devbrain

# Bring across the finished virtualenv. No compilers or build headers come with
# it, so the runtime image stays small.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    # Stream logs straight out instead of buffering them, so `docker logs`
    # shows output in real time.
    PYTHONUNBUFFERED=1 \
    # Do not litter the image with .pyc files.
    PYTHONDONTWRITEBYTECODE=1 \
    # Inside the container the server must bind 0.0.0.0, not 127.0.0.1.
    # Binding to loopback would make it unreachable from outside the container.
    API_PORT=8001

WORKDIR /app

# Application code, owned by the unprivileged user.
COPY --chown=devbrain:devbrain *.py index.html ./

# Mount point for your documents (bound to ./data by docker-compose.yml).
RUN mkdir -p /app/data && chown devbrain:devbrain /app/data

USER devbrain

EXPOSE 8001

# Compose and orchestrators use this to know when the app is genuinely ready.
# /api/health returns 503 when Postgres is unreachable, so urlopen raises and
# the container is correctly marked unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=4)"

# No --reload in a container: it watches the filesystem and doubles memory use.
# Two workers is a sensible default for a small box; raise it for more traffic.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
