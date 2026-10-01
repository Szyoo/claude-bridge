#!/usr/bin/env bash
# 同步 @szyyw/design 运行时文件到 vendor —— 直接委托给上游的 sync.sh（文件清单与 VENDORED.md 格式只在上游维护）。
# 只有 `serve --multi-user` 在门户 SSO（SZYYW_SSO=1）下会加载这些文件（右上角应用切换器 + 账户菜单，见 static/corner-boot.js）。
# 用法: bash scripts/update-design.sh [tag|--local]   （默认 latest；--local 用本机 clone，见上游 sync.sh）
set -euo pipefail
DEST="$(cd "$(dirname "$0")/.." && pwd)/src/claude_bridge/static/vendor/szyyw-design"
curl -fsSL https://raw.githubusercontent.com/Szyoo/szyyw-design/main/sync.sh | sh -s -- "$DEST" "${1:-latest}"
git -C "$DEST" status --short -- . || true
