#!/usr/bin/env bash
# 同步 @szyyw/design 运行时文件到 vendor —— 直接委托给上游的 sync.sh（文件清单与 VENDORED.md 格式只在上游维护）。
# sync.sh 从目标 tag 取（文件清单与那个版本一致），不用 main 上的。
# 用法: bash scripts/update-design.sh [tag|--local]   （默认 latest；--local 用本机 clone，见上游 sync.sh）
set -euo pipefail
DEST="$(cd "$(dirname "$0")/.." && pwd)/src/claude_bridge/static/vendor/szyyw-design"
REF="${1:-latest}"
if [ "$REF" = "--local" ]; then
  sh "${DESIGN_UPSTREAM:-$HOME/Documents/GitHub/szyyw-design}/sync.sh" "$DEST" --local
else
  if [ "$REF" = "latest" ]; then
    REF=$(git ls-remote --tags --refs https://github.com/Szyoo/szyyw-design.git 'v*' | sed 's#.*refs/tags/##' \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)
    [ -n "$REF" ] || { echo "取不到 szyyw-design 的最新 tag" >&2; exit 1; }
  fi
  curl -fsSL "https://raw.githubusercontent.com/Szyoo/szyyw-design/$REF/sync.sh" | sh -s -- "$DEST" "$REF"
fi
git -C "$DEST" status --short -- . || true
