#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

echo "拉取最新代码…"
git pull --ff-only

echo "重建镜像并重启服务…"
docker compose up -d --build --force-recreate
