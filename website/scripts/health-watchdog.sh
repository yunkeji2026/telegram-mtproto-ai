#!/usr/bin/env bash
# 华灵网站 · 健康看护 (cron 每 2 分钟跑一次)
# 本地探活 /api/health：连续 2 次不健康则重启 pm2（带 10min 冷却防抖），
# 若 .env.local 配了 ALERT_CHAT_ID 则同时 Telegram 告警。健康时静默退出。
#
# 安装：bash 上传到 /home/ubuntu/health-watchdog.sh 后，crontab 加：
#   */2 * * * * /home/ubuntu/health-watchdog.sh
export HOME=/home/ubuntu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

CHECK_PORT="${CHECK_PORT:-3000}"     # 仅用于本地探活 URL，独立于 app 的 PORT，避免污染 app 环境
PM2_NAME="${PM2_NAME:-yuntech}"
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
COOLDOWN="${COOLDOWN:-600}"          # 两次自愈重启的最小间隔(秒)
STATE=/home/ubuntu/.health-watchdog.fails
LASTR=/home/ubuntu/.health-watchdog.lastrestart
DOWNFLAG=/home/ubuntu/.health-watchdog.down    # 存在=已就"宕机"告过警，待恢复时发"已恢复"
LOG=/home/ubuntu/health-watchdog.log

now=$(date +%s); ts=$(date '+%F %T')

# Telegram 通知（单行纯文本）。收件人 = ALERT_CHAT_ID(env，可多个逗号分隔)
#   ∪ 网站 /bindadmin 绑定的留资接收人(admin_chats.json)——绑一次，留资推送和运维告警同时生效。
notify() {
  local T ids
  T=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//')
  [ -n "$T" ] || return 0
  ids=$(grep -E '^ALERT_CHAT_ID=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr ',' '\n')
  if [ -f "$HOME/hualing-leads/admin_chats.json" ]; then
    ids="$ids
$(grep -oE '[0-9-]{6,}' "$HOME/hualing-leads/admin_chats.json" 2>/dev/null)"
  fi
  ids=$(echo "$ids" | grep -E '^-?[0-9]+$' | sort -u)
  [ -n "$ids" ] || return 0
  local c
  for c in $ids; do
    curl -s "https://api.telegram.org/bot$T/sendMessage" \
      --data-urlencode "chat_id=$c" \
      --data-urlencode "text=$1" >/dev/null 2>&1
  done
}

if curl -sf -m 8 "http://127.0.0.1:$CHECK_PORT/api/health" 2>/dev/null | grep -q '"healthy":true'; then
  echo 0 > "$STATE"
  # 若此前发过"宕机/已重启"告警，现在恢复了，补发一次"已恢复"并清标志（仅一次）。
  if [ -f "$DOWNFLAG" ]; then
    echo "[$ts] recovered, sending recovery notice" >> "$LOG"
    notify "[华灵运维] ✅ 网站已恢复健康 @ $ts"
    rm -f "$DOWNFLAG"
  fi
  exit 0
fi

fails=$(cat "$STATE" 2>/dev/null || echo 0); fails=$((fails + 1)); echo "$fails" > "$STATE"
echo "[$ts] health check FAILED (consecutive #$fails)" >> "$LOG"
[ "$fails" -lt 2 ] && exit 0   # 首次失败先观望，避免瞬时抖动误重启

last=$(cat "$LASTR" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$COOLDOWN" ]; then
  echo "[$ts] within cooldown(${COOLDOWN}s) since last restart, skip restart" >> "$LOG"
else
  echo "[$ts] restarting pm2 ($PM2_NAME) ..." >> "$LOG"
  # 不加 --update-env：复用 pm2 已保存的正确环境，避免把看护脚本的临时环境(如 CHECK_PORT/PORT)注入 app
  pm2 restart "$PM2_NAME" >> "$LOG" 2>&1
  echo "$now" > "$LASTR"; echo 0 > "$STATE"; touch "$DOWNFLAG"
  notify "[华灵运维] ⚠️ 网站健康检查失败，已自动重启 pm2($PM2_NAME) @ $ts"
fi
