#!/usr/bin/env bash
# P7-1：初始化 MinIO bucket（一次性）
# 用法：docker exec -it rpa-ha-minio sh -c "$(cat scripts/ha/init_minio.sh)"

set -euo pipefail

: "${MINIO_ROOT_USER:?need MINIO_ROOT_USER}"
: "${MINIO_ROOT_PASSWORD:?need MINIO_ROOT_PASSWORD}"

mc alias set local http://localhost:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"

if ! mc ls local/rpa-backup >/dev/null 2>&1; then
  mc mb local/rpa-backup
  mc version enable local/rpa-backup   # 启用对象版本，增加一层保护
  # 生命周期：保留 90 天旧版本
  cat <<JSON | mc ilm import local/rpa-backup
{
  "Rules": [
    {
      "ID": "expire-noncurrent",
      "Status": "Enabled",
      "NoncurrentVersionExpiration": { "NoncurrentDays": 90 }
    }
  ]
}
JSON
  echo "bucket rpa-backup ready"
else
  echo "bucket rpa-backup already exists"
fi
