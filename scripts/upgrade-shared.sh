#!/usr/bin/env bash
# upgrade-shared.sh — 把本仓库固定的共享包升到上游最新的正式 tag（vX.Y.Z）：
#   szyyw-auth    pyproject.toml 的 [serve] 依赖（tarball 地址里的 tag）
#   szyyw-design  src/claude_bridge/static/vendor/szyyw-design/（vendored 副本，委托 scripts/update-design.sh → 上游 sync.sh）
# 有改动时 stdout 打印一行摘要（给提交信息用），没有就什么都不打印。CI（.github/workflows/upgrade-shared.yml）和本地都能跑。
set -euo pipefail
cd "$(dirname "$0")/.."

latest() { # <仓库> -> v0.8.0
  git ls-remote --tags --refs "https://github.com/Szyoo/$1.git" 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1
}
newer() { # <cur> <new>：new 严格更新才返回 0
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$2" ]
}

parts=()

# --- szyyw-auth（pyproject.toml）---
f=pyproject.toml
cur=$(grep -oE 'Szyoo/szyyw-auth/archive/refs/tags/v[0-9]+\.[0-9]+\.[0-9]+' "$f" | head -n1 | sed 's#.*/##')
new=$(latest szyyw-auth)
[ -n "$cur" ] || { echo "pyproject.toml 里找不到 szyyw-auth 的固定 tag" >&2; exit 1; }
[ -n "$new" ] || { echo "取不到 szyyw-auth 的 tag" >&2; exit 1; }
if newer "$cur" "$new"; then
  sed -i.bak "s#Szyoo/szyyw-auth/archive/refs/tags/$cur#Szyoo/szyyw-auth/archive/refs/tags/$new#" "$f" && rm -f "$f.bak"
  parts+=("szyyw-auth $cur → $new")
fi

# --- szyyw-design（vendored 副本）---
vf=src/claude_bridge/static/vendor/szyyw-design/VENDORED.md
cur=$(grep -oE '当前版本：\*\*v[0-9.]+\*\*' "$vf" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1)
new=$(latest szyyw-design)
[ -n "$cur" ] || { echo "$vf 里找不到当前版本" >&2; exit 1; }
[ -n "$new" ] || { echo "取不到 szyyw-design 的 tag" >&2; exit 1; }
if newer "$cur" "$new"; then
  bash scripts/update-design.sh "$new" >&2
  parts+=("@szyyw/design $cur → $new")
fi

out=""
for p in "${parts[@]+"${parts[@]}"}"; do out="${out:+$out、}$p"; done
[ -z "$out" ] || echo "$out"
