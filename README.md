# 成竹 Foresketch

投研信息整理与仿真推演多 Agent 系统。

产品设计文档：`docs/product/`（同步自 `../goai2026/`）。

## 环境要求

| 工具 | 版本 | 说明 |
|------|------|------|
| Node.js | >=18 | 本机用 pnpm |
| Python | **建议 3.11–3.12** | 系统自带 3.9 可跑 Phase 0；Phase 2 Graphiti 建议升到 3.11 |
| Neo4j | 5.26+ | `brew install neo4j`，**不用 Docker** |
| API Key | Phase 0 不需要 | Phase 1 起博查；Phase 2 起百炼 |

## 本地开发（零 Docker）

```bash
# 1. Neo4j（Homebrew 原生，限制内存）
brew install neo4j
# Apple Silicon 配置文件通常在：
#   /opt/homebrew/opt/neo4j/libexec/conf/neo4j.conf
# 设置：
#   server.memory.heap.max_size=1g
#   server.memory.pagecache.size=512m
# 首次启动后在浏览器 http://localhost:7474 把密码改为 chengzhu2026（与 .env 一致）
neo4j start

# 2. 环境变量
cp .env.example .env

# 3. 依赖（包管理器用 pnpm）
pnpm run setup:all

# 4. 启动前后端
pnpm run dev
```

- 前端：http://localhost:3000
- 后端：http://localhost:5001  （`GET /api/health`）
- Neo4j Browser：http://localhost:7474

```bash
# 仅测后端
pnpm run backend
# SQLite 验收
pnpm run test:backend
```

## 可选 Docker（仅交付评委）

本地开发与演示**不依赖** Docker。交付包提供：

```bash
docker compose up -d
```

## Phase 0 验收状态

- [x] `GET /api/health` → 200
- [x] 前端空壳首页可访问
- [x] `pytest tests/test_db.py` 3 passed
- [ ] Neo4j brew 安装与 bolt 连通（见上）

## 合规声明

本系统仅做公开信息整理与情景观察，不构成投资建议。详见 `docs/product/09_合规边界与演示交付计划.md`。

部分工具代码继承自开源项目 MiroFish，见 `LICENSE.MiroFish`。
