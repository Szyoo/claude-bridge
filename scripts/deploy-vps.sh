#!/usr/bin/env bash
# 一键部署到 vultr-jp：rsync 源码 → 远端 docker compose up -d --build → 健康检查。
# 用法：bash scripts/deploy-vps.sh [ssh别名，默认 vultr-jp]
# 首次部署前在 VPS 上准备 /opt/claude-bridge/deploy/vps/.env（见 env.example），
# 并建第一个管理员：docker exec -it claude-bridge claude-bridge users add <名字> --admin
set -euo pipefail
HOST="${1:-vultr-jp}"
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude /.git --exclude /.venv --exclude /deploy/vps/.env --exclude '__pycache__' --exclude /.pytest_cache \
  --exclude /.ruff_cache --exclude '*.egg-info' --exclude '*.db' --exclude '*.db-*' --exclude /claude-bridge-files \
  ./ "$HOST":/opt/claude-bridge/
ssh "$HOST" 'cd /opt/claude-bridge/deploy/vps && test -f .env || { echo "缺 /opt/claude-bridge/deploy/vps/.env（见 env.example）"; exit 1; }
  docker compose up -d --build 2>&1 | tail -3 && docker image prune -f >/dev/null
  for i in $(seq 1 15); do
    if docker exec claude-bridge python -c "import urllib.request;print(urllib.request.urlopen(\"http://127.0.0.1:8770/api/health\",timeout=4).read().decode())" 2>/dev/null; then exit 0; fi
    sleep 2
  done
  echo "健康检查未通过："; docker logs --tail 20 claude-bridge; exit 1'
