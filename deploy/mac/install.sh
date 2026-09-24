#!/usr/bin/env bash
# 在 Mac mini 上安装 / 更新 claude-bridge worker（用户级 LaunchAgent）。
# 前提：.venv 已建好并 pip install -e .；仓库 .env 里有 CLAUDE_BRIDGE_URL 与 CLAUDE_BRIDGE_AGENT_TOKEN
#       （见 deploy/mac/env.example）；claude CLI 已登录。
# 用法：bash deploy/mac/install.sh          安装/更新并启动（改了代码后也跑这个，worker 用的是这份代码）
#       bash deploy/mac/install.sh --stop   停止并卸载
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="xyz.szyyw.claude-bridge-worker"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

if [ "$UID_NUM" = "0" ]; then
  echo "不要用 sudo：LaunchAgent 是用户级服务，以本人身份执行。"; exit 1
fi

if [ "${1:-}" = "--stop" ]; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "已停止并移除 $LABEL"; exit 0
fi

[ -x "$REPO/.venv/bin/claude-bridge" ] || { echo "缺少 $REPO/.venv/bin/claude-bridge，先建 venv 并 pip install -e ."; exit 1; }
grep -q '^CLAUDE_BRIDGE_URL=.\+' "$REPO/.env" 2>/dev/null || { echo ".env 缺少 CLAUDE_BRIDGE_URL"; exit 1; }
grep -q '^CLAUDE_BRIDGE_AGENT_TOKEN=.\+' "$REPO/.env" 2>/dev/null || { echo ".env 缺少 CLAUDE_BRIDGE_AGENT_TOKEN"; exit 1; }

CWD_DIR="$(sed -n 's/^CLAUDE_BRIDGE_CWD=//p' "$REPO/.env")"
[ -n "$CWD_DIR" ] && mkdir -p "$CWD_DIR"

echo "1/3 检查服务端与本机 claude…"
"$REPO/.venv/bin/claude-bridge" status --env-file "$REPO/.env" || true

echo "2/3 写入 LaunchAgent…"
mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/deploy/mac/$LABEL.plist" > "$PLIST_DST"

echo "3/3 重启服务…"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# bootout 是异步的，紧接着 bootstrap 会报 "5: Input/output error"，等一下再试
for i in 1 2 3 4 5; do
  sleep 1
  if launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST" 2>/dev/null; then break; fi
  [ "$i" = 5 ] && { echo "bootstrap 失败，手动执行：launchctl bootstrap gui/$UID_NUM $PLIST_DST"; exit 1; }
done
launchctl kickstart -k "gui/$UID_NUM/$LABEL"
sleep 2
launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state|pid" | head -3
echo "日志：tail -f $REPO/logs/worker.log"
