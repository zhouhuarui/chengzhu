# 成竹 Foresketch — 投研信息整理与仿真推演多 Agent 系统 · 产品设计文档集

> 参赛赛道：无界应用 Boundless Agents · 4.3.3 AI+金融 · 投研信息整理 Agent
> 基础框架：基于开源项目 [MiroFish](https://github.com/666ghj/MiroFish) 的多 Agent 编排框架改造
> 文档目的：本文档集面向**执行开发的工程 Agent**，要求按文档逐条实现，不需要自行做产品决策。

---

## 一句话定位

成竹 是一个面向 A 股投研场景的多 Agent 信息整理系统：用户用自然语言提出需求，系统并行采集公告、财报、新闻、研报和行业数据，生成**可溯源**的摘要 / 对比 / 追踪报告。摘要与对比任务还可选择“证据化基本面辩论”，由两个中性研究视角交叉质询、确定性程序做证据与财务口径硬审计，再由 Judge 综合共识与分歧；每次执行以不可变 `run_id` 隔离，支持 direct/debate A/B。

**本系统只做信息整理与呈现，不输出任何确定性投资建议。**（详见 09 合规文档）

## 文档目录与阅读顺序

| 编号 | 文件 | 内容 | 读者 |
|------|------|------|------|
| 00 | `README.md`（本文件） | 项目定位、文档索引、术语表 | 所有人 |
| 01 | `01_产品需求文档PRD.md` | 目标用户、场景痛点、任务闭环、交互流程、功能清单 | 产品/开发 |
| 02 | `02_多Agent编排架构设计.md` | 整体架构、MiroFish 继承与改造对照、编排流程图、状态机 | 开发（必读） |
| 03 | `03_Agent规格书.md` | 每个 Agent 的职责、系统 Prompt、工具清单、输入输出契约 | 开发（必读） |
| 04 | `04_数据源与工具接口规范.md` | 中国大陆可用数据源清单、每个工具函数的签名与实现要点 | 开发（必读） |
| 05 | `05_记忆与历史学习系统设计.md` | 时序知识图谱、用户偏好记忆、反馈闭环、经验规则库 | 开发（必读） |
| 06 | `06_后端API与数据模型规范.md` | REST API 定义、SQLite 数据模型、目录结构、状态轮询协议 | 后端开发 |
| 07 | `07_前端设计规范.md` | 页面结构、组件清单、与后端交互时序 | 前端开发 |
| 08 | `08_开发任务拆解与验收标准.md` | 分阶段任务卡（可直接作为开发 TODO）、每个任务的验收标准 | 开发（施工图） |
| 09 | `09_合规边界与演示交付计划.md` | 合规边界、风险提示、数据授权说明、比赛演示脚本 | 所有人 |
| 10 | `10_仿真推演模块设计.md` | 情景推演模块：市场角色沙盘、双情景推演、采访互动（MiroFish OASIS 链路移植） | 开发（必读） |

## 与 MiroFish 的关系（30 秒版）

MiroFish 的管线是：**种子材料 → Zep 图谱构建 → 生成仿真环境（人设/参数）→ OASIS 群体仿真 → ReportAgent（ReACT+工具）生成报告 → 深度对话**。它面向"预测"，核心资产是：
1. 分阶段异步管线 + 文件化任务状态 + 前端轮询的工程骨架；
2. 时序知识图谱（Zep/Graphiti）作为 Agent 共享记忆；
3. ReportAgent 的"规划大纲 → 逐章节 ReACT 工具循环 → 反思 → 装配"报告生成范式；
4. 全程 JSONL 过程日志（agent_log.jsonl）带来的可观测性。

成竹 **保留 1/2/3/4 的骨架**，主管线把"OASIS 社会仿真"替换为"**并行专家采集 Agent 群 + 审校 Agent**"，交付面向投研的**摘要/对比/追踪**三类报告，并新增 MiroFish 没有的**历史学习层**；同时把 OASIS 仿真链路**移植保留为"情景推演"支线模块**（10 文档）——以研究图谱为种子世界，推演"如果发生 X，市场舆论会怎样"，这正是 MiroFish"数字沙盘预演"本源能力在投研场景的重定向。逐文件继承对照表见 02 文档第 6 节。

## 术语表（全文档统一用语）

| 术语 | 含义 |
|------|------|
| 任务卡 TaskCard | Planner Agent 将用户自然语言需求解析出的结构化 JSON（标的、时间窗、信息类型、交付物类型） |
| 证据卡 EvidenceCard | 采集 Agent 输出的标准化信息单元（来源、URL、发布时间、原文摘录、结构化字段、可信度） |
| 研究图谱 ResearchGraph | 每个项目一张的时序知识图谱（Graphiti/Neo4j），存实体、关系、事实及其有效期 |
| 用户记忆 UserMemory | 跨项目的用户偏好图谱（关注标的、行业、格式偏好、历史评价） |
| 经验规则 Playbook | 从历史任务成败与用户反馈中归纳出的规则条目，注入 Agent Prompt |
| 交付物 Deliverable | 摘要报告 / 对比报告 / 追踪简报 三种之一 |
| 追踪订阅 TrackingSub | 用户对某任务卡开启的定时重跑订阅（如每日盯盘简报） |
| 运行 Run | 一次确认后的不可变执行单元；拥有独立 TaskCard、证据、标准化事实、辩论记录和报告 |
| ClaimCard | 辩论中的版本化论断，永久引用 evidence_uid/fact_uid，可被挑战、修订、撤回或裁决 |
| EvidenceAuditor | 不使用 LLM 的硬审计器，检查引用、数字、期间、口径、时点和合规；Judge 无权覆盖失败 |

## 技术栈（决策已定，执行 Agent 不得更换）

| 层 | 选型 | 理由 |
|----|------|------|
| 文本 LLM | DeepSeek `deepseek-v4-flash`（普通文本）+ `deepseek-v4-pro`（Reviewer/辩论/Judge），OpenAI 兼容接口 | 文本角色统一；辩论/Judge 显式开启 thinking，其余显式关闭 |
| 多模态 | 阿里百炼 `qwen3-vl-plus`（公告扫描件/图表候选页） | 与文本 Key 隔离；失败保留本地解析，不接管文本 |
| 仿真引擎 | OASIS（camel-ai，pip 本地运行） | 继承 MiroFish 推演能力，LLM 走百炼，大陆无障碍 |
| Embedding | 阿里百炼 `text-embedding-v4` | 图谱向量化独立能力，与 DeepSeek 文本凭证隔离 |
| 图谱记忆 | 自托管 Graphiti（graphiti-core）+ Neo4j 5.26+（**本地开发用 Homebrew 原生安装，不依赖 Docker**） | 替代境外 Zep Cloud，数据不出境 |
| 联网搜索 | 博查 Bocha Web Search API | 大陆合规的谷歌/Bing 替代，¥0.036/次 |
| 金融数据 | akshare（东方财富/巨潮/新浪等公开数据封装） | 免费、无 Key、大陆直连 |
| 后端 | Python 3.11 + Flask（沿用 MiroFish backend 骨架） | 最大化复用 |
| 业务库 | SQLite（任务、反馈、Playbook、订阅） | 轻量，比赛 Demo 够用 |
| 前端 | 沿用 MiroFish frontend（Vue3 + Vite）改造 | 最大化复用 |
| 部署 | 本地开发：`brew install neo4j` + `npm run dev`（零 Docker）；交付评委：docker-compose 可选 | 控制本机资源占用，Docker 仅作为交付形态 |

## 环境变量总表（.env）

```env
# 文本 LLM（DeepSeek，OpenAI 兼容模式）
TEXT_LLM_PROVIDER=deepseek
TEXT_LLM_API_KEY=
TEXT_LLM_BASE_URL=https://api.deepseek.com
TEXT_LLM_FAST_MODEL=deepseek-v4-flash
TEXT_LLM_REASONING_MODEL=deepseek-v4-pro

# 视觉 LLM（阿里百炼，独立凭证）
VISION_LLM_PROVIDER=dashscope
VISION_LLM_API_KEY=
VISION_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_LLM_MODEL=qwen3-vl-plus

LLM_CONNECT_TIMEOUT_SECONDS=10
LLM_READ_TIMEOUT_SECONDS=180
LLM_MAX_RETRIES=1
VISION_MAX_PAGES=8
EMBEDDING_MODEL_NAME=text-embedding-v4

# 图谱（自托管 Graphiti + Neo4j）
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=chengzhu2026

# 联网搜索（博查）
BOCHA_API_KEY=

# 可选：Tushare（如需更稳的财务数据，免费注册 token）
TUSHARE_TOKEN=

# 业务配置
FLASK_DEBUG=false
COLLECTOR_MAX_PARALLEL=5        # 采集 Agent 最大并行数
ANALYST_MAX_TOOL_CALLS=6        # 分析 Agent 每章节最大工具调用次数
REVIEWER_MAX_ROUNDS=2           # 审校最大轮数
TRACKING_CRON_ENABLED=true      # 是否启用追踪订阅调度

# 仿真推演（10 文档）
SCENARIO_ENABLED=true
SCENARIO_AGENT_SCALE=30         # 每情景仿真角色数
SCENARIO_MAX_ROUNDS=10          # 每情景推演轮数
```

> **API Key 与无 Key 演示**：`TEXT_LLM_API_KEY`、`VISION_LLM_API_KEY` 和 `BOCHA_API_KEY` 都可留空，使用 `scripts/load_demo.py --force` 回放 `demo_seed` 已包含的产物。实时文本生成需要 DeepSeek Key；扫描件/图表候选页增强需要独立百炼 Key。旧 `LLM_*` 名称仅作为兼容回退。

## 快速导航：如果你是执行开发的 Agent

1. 先通读 `02_多Agent编排架构设计.md` 建立全局观；
2. 按 `08_开发任务拆解与验收标准.md` 的阶段顺序施工；
3. 写每个 Agent 时打开 `03_Agent规格书.md` 复制 Prompt 与 IO 契约；
4. 写每个数据工具时打开 `04_数据源与工具接口规范.md` 复制函数签名；
5. 所有对外话术（前端文案、报告尾注）必须包含 `09` 文档规定的免责声明。
