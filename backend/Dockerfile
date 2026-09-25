FROM python:3.11-slim

WORKDIR /app

# System deps for asyncpg/pgvector builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user (VA security fix) — the app only binds to 8000,
# so no root / CAP_NET_BIND_SERVICE is needed. UID/GID 1001 is what
# k8s/04-backend-deployment.yaml's securityContext (runAsUser/runAsGroup)
# expects for this image.
RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

USER appuser

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
