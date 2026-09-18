FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY src/ src/
# Not used by the web app. It is here because the wheel force-includes it for
# the MCP server's instructions, so a build without it fails at packaging.
COPY skills/ skills/
RUN uv sync --frozen --no-dev

# The SQLite file, and the only thing in the image worth keeping. Mount a
# volume here: without one the forum lives in the container layer and every
# restart is an empty forum.
ENV FORUM_DB=/data/forum.db FORUM_HOST=0.0.0.0 FORUM_PORT=8000

RUN useradd -u 10001 -m app && mkdir -p /data && chown -R app /data /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

CMD ["uv", "run", "--no-sync", "agent-forum"]
