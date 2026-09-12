#!/usr/bin/env bash
# FA 语言项目 —— 快照备份脚本
# 用法: bash scripts/backup.sh [备注]
# 产出: $FA_BACKUP_DIR/fa-YYYYmmdd-HHMMSS-<备注>.tar.gz  (自动保留最近 30 份)
#
# SRC 由脚本自身位置推导，不再硬编码路径（仓库叫什么名字、放在哪儿都能用）。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${FA_BACKUP_DIR:-$(dirname "$SRC")/backups}"
NOTE="${1:-snapshot}"
STAMP="$(date +%Y%m%d-%H%M%S)"
SAFE_NOTE="$(echo "$NOTE" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-40)"
OUT="$DEST/$(basename "$SRC")-$STAMP-$SAFE_NOTE.tar.gz"

mkdir -p "$DEST"

# 1) 优先保证 git 里有提交（有改动就自动 commit，绝不丢失工作）
cd "$SRC"
if [ -d .git ]; then
  if [ -n "$(git status --porcelain)" ]; then
    git add -A >/dev/null 2>&1 || true
    git commit -q -m "auto backup: $NOTE ($STAMP)" >/dev/null 2>&1 || true
  fi
fi

# 2) 打包（排除掉可重建的中间产物）
tar --exclude-vcs-ignores \
    --exclude='./build/*' \
    --exclude='./.git/*' \
    --exclude='__pycache__' \
    --exclude='.fa_work' \
    -czf "$OUT" -C "$(dirname "$SRC")" "$(basename "$SRC")"

# 3) 保留最近 30 份
ls -1t "$DEST"/$(basename "$SRC")-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "backup -> $OUT  ($(du -h "$OUT" | cut -f1))"
