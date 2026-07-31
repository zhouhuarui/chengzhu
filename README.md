# 成竹 Foresketch

投研信息整理与仿真推演多 Agent 系统（Boundless Agents · AI+Finance 投研信息整理赛道）。

产品设计文档：`docs/product/`（同步自 `../goai2026/`）。

## 环境要求

| 工具 | 版本 | 说明 |
|------|------|------|
| Node.js | >=18 | 本机用 pnpm |
| Python | 3.9+（建议 3.11–3.12） | Graphiti 完整能力建议 3.11 |
| Neo4j | 5.26+（可选） | `brew install neo4j`；未安装时自动使用本地 JSON 图谱 |
| API Key | 可选 | DeepSeek 负责文本，百炼 Qwen-VL 负责视觉；无 Key 可载入 `demo_seed` 回放 |
| Datayes | 可选 | 已购结构化数据；未配置时自动保留原公开数据源链路 |

## 本地开发（零 Docker）

```bash
# 1. Neo4j（Homebrew；国内建议清华 bottle 镜像）
export HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles
bash scripts/setup_neo4j.sh
# Browser http://localhost:7474 密码 chengzhu2026

# 2. 环境变量
cp .env.example .env   # 按需填 TEXT_LLM_API_KEY / VISION_LLM_API_KEY / BOCHA_API_KEY

# 3. 依赖
pnpm run setup:all

# 4. 启动
pnpm run dev
```

- 前端：http://localhost:3000
- 后端：http://localhost:5001（`GET /api/health`）
- Neo4j：http://localhost:7474（可选）

### 演示数据（无 API Key）

```bash
backend/.venv/bin/python scripts/build_debate_demo_seed.py # 重建宁德时代 vs 比亚迪黄金案例
backend/.venv/bin/python scripts/build_demo_seed.py   # 可选：从当前 uploads 打包其他演示任务
python3 scripts/load_demo.py --force # 载入 demo_seed/
pnpm run dev
```

`demo_seed` 是只读演示回放：可查看包内已有的报告、证据、图谱、运行记录与推演产物，不会调用文本或视觉模型。黄金案例内置同一冻结证据下的 `direct` 与 `evidence_debate` 两个 run，可无 Key 切换 A/B。它不会凭空生成 seed 中未包含的新任务；需要实时采集或重新辩论时仍须配置对应 API Key。`--force` 会覆盖同名 uploads 目录，载入前请备份本地任务。

## Datayes 私有数据融合

Datayes 作为财务、行情估值、行业数据和公司结构化事件的优先 Provider；新闻、快讯、券商研报、宏观数据、公告 PDF 和通用搜索仍使用原数据源。Agent 只能访问审核后的接口白名单，不能提交任意 DataAPI 请求。

私有模式在 `.env` 中补充：

```dotenv
DATAYES_ENABLED=true
DATAYES_PROVIDER_MODE=warehouse_then_api
DATAYES_TOKEN=
DATAYES_DATA_DIR=/Users/zhouhuarui/Projects/Datayes/data
DATAYES_LICENSE_MODE=private_derived_only
DATAYES_PUBLIC_EXPORT=false
```

- 历史区间优先通过 DuckDB 只读查询本地 Parquet；仓库未覆盖的最新区间才调用 DataAPI。
- Token 仅存在后端环境变量中，不写入日志、EvidenceCard、Agent prompt 或前端响应。
- 没有公开 URL 的 Datayes 证据通过 `provider/api/record_key/as_of/row_fingerprint` 溯源，页面会显示授权提示。
- 授权边界未确认前固定使用 `private_derived_only`：不批量下载、不进入公开 `demo_seed`、不把原始采购数据或 Datayes 原始文档复制到本仓库。

### 无 Key 模式

保持 `DATAYES_ENABLED=false`。系统不会调用 DataAPI，也不要求本地 Datayes 仓库，继续使用公开来源或明确标记的合成演示数据。离线单元测试默认运行；只有同时配置 Token 并设置 `DATAYES_NETWORK_TESTS=true` 才运行真实接口一致性测试。`scripts/build_demo_seed.py` 会在覆盖现有 seed 前扫描 uploads；发现 Datayes 私有证据、图谱或数据库记录时直接拒绝打包。

### Docker 只读挂载

Compose 把宿主机 `.env` 中的 `DATAYES_DATA_DIR` 只读挂载到容器 `/data/datayes`。启动前请填写宿主机绝对路径；未使用 Datayes 时保持默认关闭即可。

`private_derived_only` 是私有后端模式，不是公网多用户部署模式。Compose 默认只绑定
`127.0.0.1`，后端 CORS 也只允许本地前端。如需远程访问，必须先在反向代理/VPN 层增加身份认证，
再显式配置 `CHENGZHU_BIND_ADDRESS` 和 `CORS_ALLOWED_ORIGINS`；不得将证据 API 直接暴露到公网。

```bash
docker compose up --build
```

上线前还需由数据采购负责人确认：原始字段展示、缓存期限、比赛录屏/公开 Demo 的派生数据展示、证据导出，以及正式 QPS、并发、日额度和 Token 有效期。

## 架构要点

```
需求 → Planner → 确认任务卡 → 5 采集 Agent 并行
     → 图谱摄入 → 冻结证据快照 → 财务事实标准化
     ├─ direct：Analyst（DeepSeek 证据门禁 + 确定性财务表达）
     └─ evidence_debate：两轮多视角辩论 → 硬审计 → Judge → Analyst 表达
     → Reviewer → 报告装配
     → Chat / 反馈 → Reflection → Playbook
可选：追踪订阅 · 情景推演（双情景沙盘）
```

### 分析模式与模型分工

- `summary`、`compare` 可在任务卡选择 `direct` 或 `evidence_debate`；旧任务默认 `direct`，`tracking` 固定走 `direct`。
- DeepSeek `deepseek-v4-flash` 用于 Planner、普通 Analyst、报告表达、Chat、Reflection 与 Scenario；`deepseek-v4-pro` 非思考模式用于 Reviewer，思考模式用于两名辩论 Agent 和 Judge。
- 百炼 `qwen3-vl-plus` 只处理扫描 PDF 或图表候选页，不作为 DeepSeek 文本失败时的静默备用模型。视觉失败时保留本地 PDF 文本/表格结果，并标记“视觉证据未完整解析”。
- 辩论固定两轮、最多四个研究维度。确定性 `EvidenceAuditor` 检查引用、数字、期间、单位、时点和合规；Judge 无权接受硬检查失败的观点，也不会展示或持久化模型思维链。

新配置见 [.env.example](.env.example)。只配置旧 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL_NAME*` 的部署仍可运行，但新部署应使用隔离的 `TEXT_LLM_*` 与 `VISION_LLM_*`。

### 不可变运行产物

每次确认任务都会返回新的 `run_id`，同一任务的 direct/debate A/B 运行互不覆盖：

```text
backend/uploads/tasks/{task_id}/runs/{run_id}/
  run.json
  evidence/                 # 本次采集原始卡片
  evidence_index.json       # 稳定 evidence_uid 与 E1…En 显示映射
  normalized_facts.jsonl    # 同口径 FinancialFact
  debate/                   # 仅 evidence_debate 运行产生
    claims.jsonl
    challenges.jsonl
    audit.jsonl
    challenge_audit.jsonl
    verdict.json
  report_publish_started.json # 报告三文件事务开始标记
  report.json
  report.md
  full_report.md
  report_commit.json        # 含三文件摘要；存在且校验通过才视为已发布
```

报告、证据、图谱和反馈接口都可携带 `run_id`；省略时读取 latest。任务根目录保留 latest 兼容副本，旧任务仍可读取。

### 比赛版非目标

证据辩论用于公开信息的交叉核验，不生成 Alpha 因子、PIT 全市场面板、IC/ICIR、回测、股票排序或买卖信号；不在 tracking 中运行完整辩论，也不在辩论阶段临时联网补采。缺口只会形成待补证据请求。

## API 蓝图

| 前缀 | 说明 |
|------|------|
| `/api/task` | 创建/确认/状态/证据/图谱/日志 |
| `/api/report` | 报告/Markdown/Chat/审校日志 |
| `/api/feedback` | 章节赞踩/星级 |
| `/api/memory` | 预填/偏好/Playbook/源健康度 |
| `/api/tracking` | 追踪订阅与简报 |
| `/api/scenario` | 情景推演 |

## 开发进度

- [x] Phase 0：骨架、health、SQLite、前端
- [x] Phase 1：十工具 + registry + source-health
- [x] Phase 2：图谱本体 + graph_client（Neo4j/本地双后端）+ ingest
- [x] Phase 3：Planner → 采集 → 分析 → 审校 → 报告 → Chat
- [x] Phase 4：反馈 / Reflection / Playbook / 记忆预填 / 追踪调度
- [x] Phase 5：前端路由与业务页（Home/Confirm/Run/Report/Tracking/Profile/Scenario）
- [x] Phase 6：PDF 表格解析 + chart 数据块（导出降级表格）
- [x] Phase 7：情景推演（双情景简化沙盘 + 采访）
- [x] Phase 8：demo_seed 脚本 + 合规声明
- [x] Phase 9：不可变 run、财务标准化、证据化基本面辩论、DeepSeek 文本 + Qwen-VL 视觉

```bash
curl -s -X POST http://localhost:5001/api/task/create \
  -H 'Content-Type: application/json' \
  -d '{"requirement":"对比宁德时代和比亚迪最近的财务与公告"}'
```

## 合规声明

本系统仅做信息整理与情景观察，**不构成投资建议**。启用 Datayes 时按已授权的私有派生数据边界运行；推演报告含模拟限定语与双情景对比。详见 `docs/product/09_合规边界与演示交付计划.md`。

部分工具与编排思路继承自开源项目 MiroFish，见 `LICENSE.MiroFish`。

## 启停 Neo4j

```bash
# 一键安装/启动（限制堆 1g）
bash scripts/setup_neo4j.sh

neo4j start
neo4j stop
neo4j status
# Browser: http://localhost:7474  密码与 .env 中 NEO4J_PASSWORD 一致（默认 chengzhu2026）
```

未安装 Neo4j 时系统自动使用本地 JSON 图谱，功能可完整演示；装好后会双写 Neo4j。

## 验收

```bash
# 默认离线测试（pytest.ini 自动排除 network）
cd backend && .venv/bin/pytest -q

# 端到端验收（需后端已启动）
python3 scripts/verify_acceptance.py
```

联网测试只用于手动检查外部数据源健康度，不进入默认 CI。巨潮接口若返回 405，应视为上游健康告警并单独处理，不得用它阻塞离线回归：

```bash
cd backend

# 巨潮公告 live health（严格要求返回公告；可独立失败）
.venv/bin/pytest -q -o addopts='' -m network \
  tests/tools/test_smoke.py::test_fetch_announcements

# 全部公开数据源 live health
.venv/bin/pytest -q -o addopts='' -m network tests/tools

# Datayes live health 还要求 DATAYES_NETWORK_TESTS=true 及相应 Token/数据目录
DATAYES_NETWORK_TESTS=true .venv/bin/pytest -q -o addopts='' -m network tests/providers
```
