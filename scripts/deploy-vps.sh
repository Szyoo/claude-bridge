#!/usr/bin/env bash
# 立即部署到 szyyw-lighthouse（腾讯云轻量 东京；平常不需要：推送 main 后，VPS 的 szyyw-autodeploy 每 10 分钟会自动拉取并部署）。
# 用法：bash scripts/deploy-vps.sh                   部署 main 最新提交
#       bash scripts/deploy-vps.sh --rollback <ref>  回滚到某个 tag / 提交（deploy-app.sh --ref；之后自动部署会跳过非 main 的状态）
#       bash scripts/deploy-vps.sh --status          查看部署状态
# 部署本身由 VPS 上的 /opt/ingress/deploy/deploy-app.sh 完成：git 拉取 → build → up -d --no-deps → 健康检查，失败自动回滚。
# 这里只做前置检查（在 main、工作区干净、已推送），然后 ssh 过去。
set -euo pipefail
HOST="${DEPLOY_HOST:-szyyw-lighthouse}"
APP=claude-bridge
REMOTE=/opt/ingress/deploy/deploy-app.sh

case "${1:-}" in
  --rollback)
    [ -n "${2:-}" ] || { echo "用法：$0 --rollback <ref>" >&2; exit 2; }
    exec ssh "$HOST" "$REMOTE" "$APP" --ref "$2" ;;
  --status)
    exec ssh "$HOST" "$REMOTE" "$APP" --status ;;
  "") ;;
  *) echo "未知参数：$1（可用：--rollback <ref> | --status）" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."
[ "$(git branch --show-current)" = main ] || { echo "当前不在 main 分支，已中止" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "工作区有未提交的改动，已中止" >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
  || { echo "本地 main 与 origin/main 不一致（未推送或落后），已中止" >&2; exit 1; }

exec ssh "$HOST" "$REMOTE" "$APP"
