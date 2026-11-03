#!/usr/bin/env bash
# 部署 PageDrop 到显式指定的 HOST。重复执行更新源码并重建服务，不覆盖数据或 Token。
# 前置：服务器已配置 /srv/page-drop/.env，旧服务已停止，Docker 与 Compose 可用。
set -euo pipefail

HOST=${HOST:?Set HOST to your deployment SSH host}
REMOTE=${REMOTE:-/srv/page-drop}
# REMOTE 会嵌入远程命令；拒绝特殊字符及跨目录路径，避免操作到非目标位置。
if [[ ! "$REMOTE" =~ ^/[a-zA-Z0-9_./-]+$ || "$REMOTE" == / || "/$REMOTE/" == *"/../"* || "$REMOTE" == *"//"* ]]; then
  echo "REMOTE 必须是无特殊字符、无 .. 的绝对路径" >&2
  exit 1
fi
REMOTE=${REMOTE%/}
SRC="$REMOTE/src"
ROOT=$(cd "$(dirname "$0")/.." && pwd)

echo "== 检查服务端配置 =="
ssh "$HOST" "test -s '$REMOTE/.env' || { echo '缺少非空 .env；请先安全配置 Token，不自动生成或覆盖' >&2; exit 1; }"

echo "== 同步源码到 $HOST:$SRC =="
ssh "$HOST" "mkdir -p '$SRC' && rm -rf '$SRC/service' '$SRC/deploy' '$SRC/cli' '$SRC/skills'"
COPYFILE_DISABLE=1 tar -C "$ROOT" --no-xattrs --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  -czf - service deploy cli skills README.md docs/guide.md docs/specification.md LICENSE .dockerignore \
  | ssh "$HOST" "tar -xzf - -C '$SRC'"

echo "== 数据目录与属主 =="
ssh "$HOST" "install -d -m 0700 -o 10001 -g 10001 '$REMOTE/data' && chmod 600 '$REMOTE/.env'"

echo "== 构建并启动 =="
ssh "$HOST" "cd '$SRC' && PAGE_DROP_ENV_FILE='$REMOTE/.env' PAGE_DROP_DATA_DIR='$REMOTE/data' docker compose --env-file '$REMOTE/.env' -p page-drop -f deploy/docker-compose.yml up -d --build --wait --wait-timeout 120"
ssh "$HOST" "docker ps --filter name=page-drop --format '{{.Names}} {{.Status}} {{.Ports}}'"
