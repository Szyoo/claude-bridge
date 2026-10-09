#!/usr/bin/env bash
# 升级 @szyyw/design：把 src/claude_bridge/standalone.py 的 DESIGN_VERSION 改成目标 tag。
# 页面从 https://design.szyyw.xyz/<tag>/ 加载设计包（不可变，不再 vendoring）；改之前确认 CDN 上已有该版本
# （tag 推上 GitHub 后 ≤10 分钟出现），没有就失败退出（bot 下一轮重试）。
# 用法: bash scripts/update-design.sh [vX.Y.Z]   （默认上游最新正式 tag）
set -euo pipefail
cd "$(dirname "$0")/.."
F=src/claude_bridge/standalone.py
REF="${1:-latest}"
if [ "$REF" = "latest" ]; then
  REF=$(git ls-remote --tags --refs https://github.com/Szyoo/szyyw-design.git 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)
  [ -n "$REF" ] || { echo "取不到 szyyw-design 的最新 tag" >&2; exit 1; }
fi
REF="v${REF#v}"
echo "$REF" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "不是正式 tag：$REF" >&2; exit 1; }
curl -fsI --max-time 15 "https://design.szyyw.xyz/$REF/version.js" >/dev/null \
  || { echo "CDN 上还没有 $REF（https://design.szyyw.xyz/$REF/version.js），稍后再试" >&2; exit 1; }
grep -qE '^DESIGN_VERSION = "v[0-9]+\.[0-9]+\.[0-9]+"$' "$F" || { echo "$F 里找不到 DESIGN_VERSION" >&2; exit 1; }
sed -i.bak -E "s/^DESIGN_VERSION = \"v[0-9]+\.[0-9]+\.[0-9]+\"$/DESIGN_VERSION = \"$REF\"/" "$F" && rm -f "$F.bak"
grep -E '^DESIGN_VERSION = ' "$F"
