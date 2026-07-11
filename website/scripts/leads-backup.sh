#!/usr/bin/env bash
# 华灵网站 · 留资数据服务器侧每日备份 (cron 每天 03:10)
# 将 LEADS_DIR(默认 ~/hualing-leads) 打包到 ~/leads-backups/，保留最近 14 份。
# 与「117 定时拉取」双保险：即使拉取失败，服务器本地也有副本；配合 UCloud 快照三重防护。
export HOME=/home/ubuntu
LEADS_DIR="${LEADS_DIR:-/home/ubuntu/hualing-leads}"
BAK_DIR="${BAK_DIR:-/home/ubuntu/leads-backups}"
KEEP="${KEEP:-14}"
LOG=/home/ubuntu/leads-backup.log

ts=$(date '+%F %T'); stamp=$(date '+%Y%m%d-%H%M%S')
mkdir -p "$BAK_DIR"
if [ ! -d "$LEADS_DIR" ]; then
  echo "[$ts] leads dir missing: $LEADS_DIR" >> "$LOG"; exit 0
fi
out="$BAK_DIR/leads-$stamp.tar.gz"
tar -czf "$out" -C "$(dirname "$LEADS_DIR")" "$(basename "$LEADS_DIR")" 2>>"$LOG" \
  && echo "[$ts] backup OK -> $(basename "$out") ($(du -h "$out" | cut -f1))" >> "$LOG" \
  || echo "[$ts] backup FAILED" >> "$LOG"
# 只保留最近 KEEP 份
ls -1t "$BAK_DIR"/leads-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
