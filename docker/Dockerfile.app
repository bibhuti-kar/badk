FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS production
RUN groupadd -r -g 1001 appuser && useradd -r -u 1001 -g appuser appuser
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ['APP_PORT']+'/health')"
CMD ["sh", "-c", "exec uvicorn badk.main:app --host 0.0.0.0 --port ${APP_PORT}"]

# cloud profile: coordinator image with the agent and tool definitions baked in (D-28)
FROM production AS runtime
COPY agents/ agents/
COPY tools/ tools/
# badk serve touches /tmp/badk.heartbeat (badk.heartbeat.HEARTBEAT) every 5 s while connected
HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD python -c "import sys,time; sys.exit(time.time()-__import__('os').stat('/tmp/badk.heartbeat').st_mtime>30)"
CMD ["badk", "serve", "--heartbeat"]
