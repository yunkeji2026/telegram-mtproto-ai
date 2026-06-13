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
LOG=/home/ubuntu/health-watchdog.log

now=$(date +%s); ts=$(date '+%F %T')

if curl -sf -m 8 "http://127.0.0.1:$CHECK_PORT/api/health" 2>/dev/null | grep -q '"healthy":true'; then
  echo 0 > "$STATE"
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
  echo "$now" > "$LASTR"; echo 0 > "$STATE"
  T=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//')
  C=$(grep -E '^ALERT_CHAT_ID='     "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//')
  if [ -n "$T" ] && [ -n "$C" ]; then
    curl -s "https://api.telegram.org/bot$T/sendMessage" \
      --data-urlencode "chat_id=$C" \
      --data-urlencode "text=[华灵运维] 网站健康检查失败，已自动重启 pm2($PM2_NAME) @ $ts" >/dev/null 2>&1
  fi
fi
