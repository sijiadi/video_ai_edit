#!/usr/bin/env bash
# 把一个技能复制到 WorkBuddy 技能目录。用法：bash scripts/install.sh short-video-clip-merge
set -euo pipefail

SKILL="${1:-short-video-clip-merge}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${WORKBUDDY_SKILLS_DIR:-$HOME/.workbuddy/skills}"

if [ ! -d "$HERE/skills/$SKILL" ]; then
  echo "找不到技能：$HERE/skills/$SKILL" >&2
  exit 1
fi

mkdir -p "$DEST"
cp -R "$HERE/skills/$SKILL" "$DEST/"
echo "已安装 $SKILL 到 $DEST/$SKILL"
echo "请重启 WorkBuddy 会话以加载技能。"
