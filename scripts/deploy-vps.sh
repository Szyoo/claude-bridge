#!/usr/bin/env bash
# 一键部署到 vultr-jp：rsync 源码 → 远端 docker compose up -d --build → 健康检查。
# 用法：bash scripts/deploy-vps.sh [--dry-run] [ssh别名，默认 vultr-jp]
#   --dry-run：只列出 rsync 会传输/删除的文件，不改远端、不重建容器。
# 首次部署前在 VPS 上准备 /opt/claude-bridge/deploy/vps/.env（见 env.example），
# 并建第一个管理员：docker exec -it claude-bridge claude-bridge users add <名字> --admin
set -euo pipefail
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; shift; fi
HOST="${1:-vultr-jp}"
cd "$(dirname "$0")/.."
RSYNC_OPTS=(-az --delete)
[ "$DRY_RUN" = 1 ] && RSYNC_OPTS+=(--dry-run --itemize-changes)
# 排除项同时保护远端同名文件不被 --delete 删掉：
#   /.env* 与 /deploy/vps/.env*：本机开发 env 不上传，远端运行时 env 及其 .env.bak-* 备份保留
#   /logs 与 /deploy/vps/*.log：本机运行日志不上传，远端日志保留
rsync "${RSYNC_OPTS[@]}" \
  --exclude /.git --exclude /.venv --exclude '/.env*' --exclude '/deploy/vps/.env*' \
  --exclude /logs --exclude '/deploy/vps/*.log' --exclude '__pycache__' --exclude /.pytest_cache \
  --exclude /.ruff_cache --exclude '*.egg-info' --exclude '*.db' --exclude '*.db-*' --exclude /claude-bridge-files \
  ./ "$HOST":/opt/claude-bridge/
if [ "$DRY_RUN" = 1 ]; then echo "（dry-run：未改动远端）"; exit 0; fi
ssh "$HOST" 'cd /opt/claude-bridge/deploy/vps && test -f .env || { echo "缺 /opt/claude-bridge/deploy/vps/.env（见 env.example）"; exit 1; }
  docker compose up -d --build 2>&1 | tail -3
  for i in $(seq 1 15); do
    if docker exec claude-bridge python -c "import urllib.request;print(urllib.request.urlopen(\"http://127.0.0.1:8770/api/health\",timeout=4).read().decode())" 2>/dev/null; then exit 0; fi
    sleep 2
  done
  echo "健康检查未通过："; docker logs --tail 20 claude-bridge; exit 1'
