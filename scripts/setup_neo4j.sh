#!/usr/bin/env bash
# Neo4j 本机安装与启动（Homebrew，无 Docker）
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

if ! command -v brew >/dev/null; then
  echo "需要 Homebrew"
  exit 1
fi

# 清理错误的 cask 占位，避免 brew list 报错
mkdir -p /opt/homebrew/Caskroom/neo4j-desktop 2>/dev/null || true

# 国内网络建议使用清华 bottle 镜像（ghcr.io 可能极慢或失败）
export HOMEBREW_BOTTLE_DOMAIN="${HOMEBREW_BOTTLE_DOMAIN:-https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles}"

if ! brew list --formula neo4j >/dev/null 2>&1; then
  echo "安装 neo4j（HOMEBREW_BOTTLE_DOMAIN=$HOMEBREW_BOTTLE_DOMAIN）…"
  brew install neo4j
fi

# 首次密码
if command -v neo4j-admin >/dev/null; then
  neo4j-admin dbms set-initial-password chengzhu2026 2>/dev/null || true
fi

CONF="$(brew --prefix neo4j)/libexec/conf/neo4j.conf"
if [[ -f "$CONF" ]]; then
  # 限制内存
  if ! grep -q '^server.memory.heap.max_size=' "$CONF"; then
    echo 'server.memory.heap.max_size=1g' >> "$CONF"
  fi
  if ! grep -q '^server.memory.pagecache.size=' "$CONF"; then
    echo 'server.memory.pagecache.size=512m' >> "$CONF"
  fi
fi

neo4j start || true
echo "等待 bolt…"
for i in $(seq 1 30); do
  if cypher-shell -u neo4j -p neo4j 'RETURN 1;' >/dev/null 2>&1 \
     || cypher-shell -u neo4j -p chengzhu2026 'RETURN 1;' >/dev/null 2>&1; then
    echo "Neo4j 已就绪"
    exit 0
  fi
  sleep 2
done

echo "请打开 http://localhost:7474 将密码设为 chengzhu2026（与 .env 一致）"
echo "然后: neo4j status"
