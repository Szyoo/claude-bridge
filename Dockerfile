# claude-bridge server image (multi-user). Deployment: deploy/vps — no published ports, only Caddy's ingress network.
# The server never runs claude; the worker does, on the machine that is logged in (deploy/mac).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo \
    CLAUDE_BRIDGE_DB=/data/bridge.db \
    CLAUDE_BRIDGE_MULTI_USER=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip install ".[serve]" && rm -rf build src/*.egg-info

RUN mkdir -p /data && useradd -r -u 10001 bridge && chown -R bridge /data
USER bridge
VOLUME ["/data"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4).status==200 else 1)"

CMD ["claude-bridge", "serve", "--host", "0.0.0.0", "--port", "8770"]
