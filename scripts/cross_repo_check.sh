#!/usr/bin/env bash
# 跨 repo 协同信号检查 (TG-MTProto ↔ mobile-auto0423 A/B).
#
# 设计目标: 替代 /loop 20min 轮询 — 单次跑出协同状态摘要.
# 用法: bash scripts/cross_repo_check.sh [--since "24 hours ago"]
# 退出码: 0 永远 (不阻塞 CI / SessionStart hook).
#
# 输出 <20 行, 仅协同摘要; 不写文件, 不改代码.

set -uo pipefail

SINCE="${1:-24 hours ago}"
TG_REPO="${TG_REPO:-C:/telegram-mtproto-ai}"
FB_REPO="${FB_REPO:-C:/mobile-auto0423}"
PR79_REPO="victor2025PH/mobile-auto0423"
PR79_NUM=79

echo "=== Cross-repo check  ($(date +%Y-%m-%dT%H:%M))  since: $SINCE ==="

if [ ! -d "$TG_REPO/.git" ]; then
  echo "WARN: TG repo not found at $TG_REPO (set \$TG_REPO env var)"
fi
if [ ! -d "$FB_REPO/.git" ]; then
  echo "WARN: mobile-auto0423 repo not found at $FB_REPO (set \$FB_REPO env var)"
fi

git -C "$TG_REPO" fetch origin --quiet 2>/dev/null || true
git -C "$FB_REPO" fetch origin --quiet 2>/dev/null || true

tg_docs=$(git -C "$TG_REPO" log --since="$SINCE" --all --oneline -- \
  'docs/FROM_TGMTP_*' 'docs/A_TO_TGMTP_*' 'docs/B_TO_TGMTP_*' \
  'docs/*tgmtp*' 'docs/*TGMTP*' 2>/dev/null)

fb_docs=$(git -C "$FB_REPO" log --since="$SINCE" --all --oneline -- \
  'docs/A_TO_*' 'docs/B_TO_*' 'docs/*TGMTP*' 'docs/*tgmtp*' 2>/dev/null)

if [ -z "$tg_docs" ] && [ -z "$fb_docs" ]; then
  echo "[docs] no signals"
else
  if [ -n "$tg_docs" ]; then
    echo "[docs · TG repo]"
    echo "$tg_docs" | sed 's/^/  /'
  fi
  if [ -n "$fb_docs" ]; then
    echo "[docs · mobile-auto0423]"
    echo "$fb_docs" | sed 's/^/  /'
  fi
fi

if [ -z "${GH_TOKEN:-}" ] && command -v git >/dev/null 2>&1; then
  GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null \
    | grep '^password=' | cut -d= -f2)
fi

if [ -n "${GH_TOKEN:-}" ] && command -v curl >/dev/null 2>&1; then
  pr79_state=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$PR79_REPO/pulls/$PR79_NUM" \
    | grep -E '"(state|updated_at)"' | head -2 | tr -d ' ,"' | paste -sd' ' -)
  pr79_count=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/$PR79_REPO/issues/$PR79_NUM/comments" \
    | grep -c '"id"' 2>/dev/null)
  tg_open_prs=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
    "https://api.github.com/repos/yunkeji2026/telegram-mtproto-ai/pulls?state=open" \
    | grep -c '"number"' 2>/dev/null)
  echo "[gh] PR #79 ($pr79_state) comments=$pr79_count  | TG open PRs=$tg_open_prs"
else
  echo "[gh] skipped (no GH_TOKEN or curl)"
fi

echo "=== done ==="
