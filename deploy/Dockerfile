# trae-agent service image. The Eyes source tree is NOT baked in; it is mounted
# read-only at /opt/eyes by compose.yaml and put on PYTHONPATH, so the pinned
# Eyes baseline stays externally verifiable instead of copied.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --home-dir /app --shell /usr/sbin/nologin agent

WORKDIR /app
ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN mkdir -p /data/state && chown agent:agent /data/state && chmod 700 /data/state

USER agent
ENV PATH="/app/.venv/bin:$PATH" \
    AGENT_STATE_DIR=/data/state \
    AGENT_HTTP_HOST=0.0.0.0 \
    AGENT_HTTP_PORT=19070

EXPOSE 19070
# One service process per protocol store; never raise --workers above 1.
CMD ["trae-agent-serve"]
