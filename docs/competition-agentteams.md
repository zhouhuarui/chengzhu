# Chengzhu × AgentTeams 竞赛重构说明

## 结论

Chengzhu 参赛赛道保持 **Boundless Agents · AI+Finance**。赛题对 AgentTeams、多角色协作、Skill 和阿里云生态有明确技术栈与加分导向；差异化来自 Chengzhu 是否把投研做成可运行、可审计、可失败恢复、有人类发布门的 Agent Team，而不是只做 PPT 或套一层多 Agent 文案。

本仓库竞赛包固定 AgentTeams `v1.2.0`，提供 8 个 Worker CR（含 Team Leader）、1 个 Team CR、8 套 SOUL/Skill、与后端完全同键的双模式 TeamHarness DAG（`evidence_debate` 9 节点、`direct` 7 节点）、校验和锁定的百炼官方视觉 Skill、安全安装/打包/应用/验证脚本，以及 Chengzhu MCP 的角色级 Higress 路由、鉴权和 CAS 边界。

入口与上游：

- 比赛赛道页：<https://goaihz.com/#tracks>
- AgentTeams 仓库：<https://github.com/agentscope-ai/AgentTeams>
- 固定版本：<https://github.com/agentscope-ai/AgentTeams/tree/v1.2.0>
- 本地上游锁：[`../agentteams/UPSTREAM.lock`](../agentteams/UPSTREAM.lock)

## AgentTeams 架构是什么

AgentTeams 不是一个 Planner Prompt，而是多智能体运行/协作平台：

1. **声明式资源**：`Worker`、`Team`、`Human`、`Manager` 使用 `agentteams.io/v1beta1`。v1.2 的 Team 通过 `spec.workerMembers` 引用独立 Worker，并且必须恰好一个 `team_leader`。
2. **控制面**：Controller/Manager 管理身份、成员关系、生命周期、资源、运行时和状态收敛。Chengzhu 的 Manager 保持 OpenClaw，并且不计入 8 个 Worker。
3. **隔离执行面**：每个 Worker 在独立容器/Pod 中运行，可锁定模型、运行时、镜像、CPU/内存和 AgentSpec/Skill 包。
4. **通信面**：Matrix Team/Task room 留下分派、结果、退回、人工介入和跨角色交接轨迹。
5. **存储与网关**：MinIO 保存实时运行的不可变制品，SQLite 保存索引、状态版本和幂等结果；Higress 统一模型、MCP、官方百炼 Skill 服务端代理和凭证边界。
6. **业务工作流**：TeamHarness 用 Project/Task DAG、`ready_nodes`、`accept_task_result` 和 durable state 控制依赖、恢复与回报。DAG 是 `projectflow.plan_dag` 载荷，不是伪造的 Team CR 字段。

Core 回答“哪些独立身份在什么环境和权限下协作”；TeamHarness 回答“本次任务如何依赖、验收和恢复”；Chengzhu 后端回答“哪些确定性副作用和事实状态真正发生”。

## 锁定的 8 个 Worker Identity

| Worker ID | AgentTeams/业务 Role | 输入 | 输出 | 关键边界 |
|---|---|---|---|---|
| `research-lead` | `team_leader` / 研究规划 | 人工确认 TaskCard、请求路由、state version | 研究计划、DAG、分派/验收、最终回执 | 可编排，不能替代系统 freeze、证据裁决、合规或 Vue 发布 |
| `disclosure-researcher` | 披露/财务来源研究 | 计划、公司/交易所 ID、公告类型、截止日 | 官方披露与财务证据、百炼审计/降级记录 | 只采集/解析，不伪造、不过截止日，不声称已完成系统 freeze |
| `market-context-researcher` | 市场背景研究 | 计划、新闻主题、研报授权、行业/同业定义 | 去重事件、授权研报、行业/政策/同业证据 | 不绕付费墙、不混口径、不把传闻/观点当事实 |
| `quality-analyst` | 质量与护城河分析 | 已冻结证据 manifest、财务事实、质量 rubric | 质量 claim-evidence 矩阵、反证、计算、置信度 | 只能用 freeze 后证据，不写最终报告 |
| `growth-analyst` | 增长与变化分析 | 已冻结证据、历史事实、情景规则 | 驱动拆解、多情景/敏感性、反证与缺口 | 不伪造预测，不隐藏假设或截止日 |
| `evidence-judge` | 证据审计与裁决 | `evidence_debate` 使用两份已验收分析，`direct` 只使用冻结上下文；另含 freeze manifest、rubric | 接受/限定/驳回/阻断裁决、允许写入报告的 claim 集 | 不改证据、不写报告、不兼任合规批准 |
| `report-writer` | 证据约束报告撰写 | 已验收裁决、允许 claim 集、模板 | 报告草稿、引用映射、披露、精确 hash | 不新增 claim、不自审/发布 |
| `compliance-reviewer` | 合规与引用复核 | 草稿/hash、裁决矩阵、全部冻结证据 | `accept/revise`、问题清单、批准 hash | 有 critical/high 不放行，不批准不同 hash |

每个 Worker manifest 都显式声明 `Name / Role / Capabilities / Inputs / Outputs / Dependencies / Decision Boundary / Trace`；角色包都含 `SOUL.md`、`AGENTS.md`、可复用 Skill 和 `agents/openai.yaml`。

## 与后端一致的双模式 DAG

主演示使用 `evidence_debate`，包含 9 个 TeamHarness 节点：

```text
人工确认 TaskCard
        │
        ▼
research-plan (research-lead)
        │
        ├── disclosure-research ───────┐
        └── market-context-research ───┤
                                       ▼
                 evidence-freeze (chengzhu-backend，系统节点)
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
                quality-analysis              growth-analysis
                        └──────────────┬──────────────┘
                                       ▼
                            evidence-judgement
                                       ▼
                               report-draft
                                       ▼
                            compliance-review
                                       ▼
                          Vue 人工批准 / 发布 / 回滚
```

`direct` 跳过双分析师辩论，使用 7 个 TeamHarness 节点：

```text
research-plan
      ├── disclosure-research ───────┐
      └── market-context-research ───┤
                                     ▼
                              evidence-freeze
                                     ▼
                            evidence-judgement
                                     ▼
                               report-draft
                                     ▼
                            compliance-review
```

为保持状态 API、历史查询和审计口径兼容，Chengzhu 后端仍为 `quality-analysis`、`growth-analysis` 写入两个持久化的 `skipped` 状态；它们不是 direct 模式的 TeamHarness 节点，也不会唤醒对应 Worker。

任务 key、依赖和角色与 [`../backend/app/team/contracts.py`](../backend/app/team/contracts.py) 一致。`evidence-freeze` 是确定性系统节点，但 `chengzhu-backend` 不是 Worker、Team member 或 Matrix 用户。Research Lead 必须先通过自己的角色绑定 MCP 路由调用 `freeze_evidence`，验证返回的 state version、manifest hash 和 ArtifactRef，再调用 TeamHarness `accept_task_result` 接受该系统节点。任何 Worker 都不能领取、模拟或绕过 freeze。

Worker 的 `RESULT_READY` 不等于验收。Leader 只有在 schema、角色、state version、证据/hash、缺口和审计通过后才能 `accept_task_result(accepted=true)`，从而释放依赖。任何阻断缺口都不能通过“标完成”绕过。

实际载荷和验收规则：

- [`../agentteams/teamharness/dag-plan.json`](../agentteams/teamharness/dag-plan.json)
- [`../agentteams/teamharness/dag-plan-direct.json`](../agentteams/teamharness/dag-plan-direct.json)
- [`../agentteams/teamharness/task-contracts.md`](../agentteams/teamharness/task-contracts.md)
- [`../agentteams/teamharness/TEAMS.md`](../agentteams/teamharness/TEAMS.md)

## 运行时与 MCP 边界

锁定的 Worker CR 使用官方兼容枚举 `runtime: copaw`，用户界面称 QwenPaw，并固定 `agentteams-copaw-worker:v1.2.0`；Manager 仍是 OpenClaw 控制面。不要把 Manager 算作 Worker，也不要在 CR 中写不存在的自定义字段。

每个 Worker CR 都通过原生 `spec.mcpServers` 注册同名 `chengzhu` MCP，但 URL 指向独立的 Higress 角色路由，例如 `mcp-chengzhu-quality-analyst`。权限链固定为：

1. AgentTeams 为每个 Worker 注入自己的网关 Consumer 凭证；
2. Higress 只允许 `worker-<role-id>` 访问对应 MCP Server；
3. 网关把服务端 Bearer 加到后端请求，并把请求转发到 `/mcp/<role-id>`；
4. Chengzhu 后端以路由中的角色为权威身份；若请求还携带不一致的 `X-AgentTeams-Worker`，立即拒绝；
5. 每个 mutation 都校验 `task_id + run_id + idempotency_key + expected_version`。

`AGENTTEAMS_MCP_GATEWAY_TOKEN` 只存在于 Higress 与 Chengzhu MCP 之间，不进入 Worker。任何 Token 都不能进入 TaskCard、Prompt、Skill 实例输入、Matrix、manifest、URL、共享工件或日志。CAS 冲突意味着刷新再决策，不能盲重试。完整契约见 [`../agentteams/teamharness/runtime-contract.md`](../agentteams/teamharness/runtime-contract.md)。

## Chengzhu 如何重构

### 1. 保留确定性事实系统

保留 TaskCard 确认、数据连接器、知识图谱、财务归一化、冻结证据、报告 artifact、状态版本、审批/回滚和 UI。AgentTeams 负责身份、隔离、Team 通信和 TeamHarness 编排；不是把数据库副作用交给 LLM。

### 2. 使用稳定交接契约

- `ConfirmedTaskCard`：只有持久化的人类确认可开 Project；
- `TaskSpec`：task/run/team/project ID、role ID、不可变输入、截止日、授权、验收规则、expected version；
- `EvidenceCard`：来源、时间、hash、页面/表格定位、事实、解析/版本链；
- `TaskResultEnvelope`：状态、artifact、证据 ID、gap、runtime/model/role/state trace；
- `ReviewDecision`：批准/退回、issue 与准确被审 hash；
- `HumanApproval`：Vue 是唯一发布权威，并保留拒绝/回滚/replay。

### 3. 将 Planner 收缩为确认前能力

现有 Planner 负责草拟 TaskCard 和交互确认；确认后 `research-lead` 接管固定 DAG。两类 Researcher 采集，后端原子 freeze，两个 Analyst 并行分析，Judge 裁决，Writer 组织，Reviewer 门禁，Vue 决定发布。

### 4. 固定单一实时运行时与只读回放

新建实时任务的 `execution_mode` 固定为 `agentteams`；确认后只由 `AgentTeamsDispatcher` 创建 Team run 并派发 Manager，不保留旧 pipeline 的第二套实时编排、影子执行或失败降级路径。旧任务缺少字段时按实时模式解释，避免静默回落到另一套状态机。

`demo_seed` 明确标记为 `replay`，只通过 `LocalReplayArtifactStore` 展示已经生成的脱敏制品；它在没有 AgentTeams、MinIO 和 API Key 时也可浏览，但不能继续、审批或伪装成一次实时运行。旧 pipeline 代码仅承担历史回放数据兼容，不接收新的实时任务。

Vue 是唯一人工审批权威。Matrix/Element 仅镜像分派、交接、降级与 `human.approved/rejected` 事件，Matrix 文本不能触发发布。批准后由后端执行发布；回滚只切换 `latest` 指针并追加审计事件，不删除旧版本。

### 5. 不可变制品与一键部署

实时运行将 MinIO 对象按内容 hash 与来源 hash 共同寻址：`chengzhu/<task>/<run>/sha256/<content_sha256>/<provenance_sha256>/<name>`。`artifact_manifest` 同时保存 URI、内容 SHA-256、生产者、schema 版本与来源信息；同一字节但不同来源不会被错误合并，本地报告只作为现有接口的兼容镜像。

竞赛环境首选单一命令启动：

```bash
make competition-up
```

该入口负责锁定版本检查、AgentTeams 控制面安装或复用、Chengzhu/Neo4j/MinIO/Higress/MCP 启动、角色清单应用和实时预检。如果 Team 已存在，默认只核验而不重放初始 Worker 状态；只有确认没有活跃任务并显式设置 `AGENTTEAMS_RECONCILE_MANIFESTS=1` 时才执行清单 reconcile。

## 百炼官方 Skill 契约

`disclosure-researcher` 对扫描或低文本密度 PDF 页调用阿里云官方仓库中的 `alibabacloud-bailian-image-creator` Skill。竞赛包锁定上游 commit `92bd723f7cc217b252feab574c1883fa0aa46b3c`，并逐文件校验 `SKILL.md`、`scripts/image_understanding.py`、`scripts/api_key.py` 和 `scripts/requirements.txt` 的 SHA-256。服务端 `bailian-visual-proxy` 执行官方提供的 `image_understanding.py`，模型固定为 `qwen3.5-plus`；不重新实现或偷偷替换官方调用路径。

`make competition-up` 先运行 [`../agentteams/scripts/fetch-official-skills.sh`](../agentteams/scripts/fetch-official-skills.sh)，校验成功后才构建后端镜像。`DASHSCOPE_API_KEY` 只存在于服务端代理的受限子进程环境，Worker 看不到长期 Key。官方调用失败、超时或限流时以受控方式回退现有本地 PDF/视觉解析，并记录 `visual_skill=degraded`；仅允许公开资料或用户明确上传并授权的页面进入百炼。

完整字段与官方链接见 [`../agentteams/roles/disclosure-researcher/skills/collect-disclosures/SKILL.md`](../agentteams/roles/disclosure-researcher/skills/collect-disclosures/SKILL.md)。

## 时间与费用预算的真实边界

- Chengzhu 后端/MCP 对自己可观测的调用维护持久化 run 预算账本，可在 480 秒或 2 元总预算耗尽后拒绝新的领域工具/模型操作；各 Team task 还获得明确预算分配。
- AgentTeams Worker 的模型调用设置 `AGENTTEAMS_MODEL_MAX_TOKENS`，它是**单次调用**的输出上限，用于限制一次异常请求，不能替代 run 总费用计量。
- AgentTeams v1.2.0 没有按 Chengzhu `run_id` 汇总全部 Worker token/费用的 API。固定的 per-Worker Higress Consumer 也只能识别角色，不能天然区分 run。因此，当前后端账本无法看见并硬拦截所有 Worker 自身的 Qwen 调用；任务预算、最多三名活跃 Worker 和 Leader 停止指令只是控制措施，不是完整的跨 Worker 2 元硬闸门。
- 要实现全链路硬闸门，仍需让所有 Worker 模型流量通过带 `run_id` 维度的 Higress/LLM 预算代理，聚合输入/输出 token 与费用，并在超限时 fail closed，再把用量回写 Chengzhu 账本。在该链路和故障注入验收完成前，竞赛材料不得声称“所有 Worker 调用均已受 2 元硬预算强制”。

## 竞赛可验证证据

只提交 manifest、PPT 或录屏不足以证明架构有效。至少保留：

- `agentteams/scripts/verify.sh` 通过记录、8 个 ZIP 的 `SHA256SUMS`；
- AgentTeams v1.2.0、Manager 与 CoPaw/QwenPaw Worker 固定镜像/版本；
- 8 Worker Ready、Team 恰好 1 个 `research-lead` Leader；
- Team room 的确认、模式对应的 9/7 节点 DAG、Worker 结果、Leader freeze bridge/验收、Reviewer 决策；
- `shared/projects/<id>/` 与 `shared/tasks/<id>/` durable state；
- claim → evidence ID → 原始页面/hash 的回放；
- MCP 角色头、CAS/idempotency 审计且没有 Token；
- 百炼官方 Skill 显式版本、request ID、延迟、fallback reason；
- 故障注入：CAS 冲突、采集失败不释放 freeze、百炼降级、Judge/Reviewer 退回、新 hash 再审、Vue 回滚/replay；
- `demo_seed` 在无 AgentTeams、无 API Key 时的只读回放，以及实时任务不会进入旧 pipeline 的证明。

## 竞赛交付文档

- [AgentTeams 运维手册](agentteams-operations.md)：启停、预检、健康判定、备份恢复、reconcile、故障处置、密钥轮换、OTel 与无 Key replay；
- [AgentTeams 威胁模型](agentteams-threat-model.md)：资产、信任边界、主要威胁、控制、残余风险与验证清单；
- [第三方依赖与归属](agentteams-third-party.md)：锁定版本、镜像边界、许可证未知项和部署时 SBOM；
- [三分钟主演示脚本](agentteams-demo-script.md)：固定金融案例、八角色、Skill 降级、审计拒绝、Vue 审批与录制证据。

## 交付里程碑与当前状态（2026-08-09）

状态含义：**已实现**仅表示仓库代码、配置、文档或自动化测试已有证据；**需实机验证**表示必须在目标 Docker/模型/网络环境运行并保存证据；**未完成**表示不能在竞赛材料中声称具备。

### 2026-08-16 可演示原型

| 交付项 | 状态 | 还需完成 |
|---|---|---|
| AgentTeams v1.2.0 锁、八 Worker、一 Team、9/7 节点 DAG、角色 Skill/MCP 权限 | **已实机验证** | 目标主机已保存 `Active` Team、exact roster、八条启用的 Wasm 规则及认证角色工具证据 |
| 实时任务只走 AgentTeamsDispatcher；`demo_seed` 只读 replay | **已实现** | 实机验证新任务无旧 pipeline 回落 |
| 双采集 → freeze bridge → 双分析 → Judge → Writer → Reviewer → Vue 审批 | **已实现（代码/合同）** | **需实机验证**完整同一 run E2E、Worker 唤醒/休眠和 480 秒上限 |
| 官方百炼 Skill 固定版本、服务端 Key、本地降级 | **已实现（代码/哈希）** | 服务端 `AGENTTEAMS_DEMO_VISUAL_FAILURE_ONCE` 已提供每 run 一次的持久化受控降级；真实百炼调用与目标主机恢复链仍**需实机验证** |
| Vue 八角色、DAG、事件筛选、预算/降级、Element 深链、审批/回滚 | **已实现** | 在真实 Matrix/Team event 上视觉验收 |
| 固定宁德时代 vs 比亚迪案例与一次确定性审计拒绝 | **已实现（无 Key 合成夹具）** | 主演示实时案例、Skill 降级和同一 run 证据仍需实机 |
| 完整 trace 与三分钟录像 | trace 字段/关键 spans **已实现** | OTLP Collector 实机链路**需验证**；正式录像**未完成** |
| 480 秒/2 元预算 | 后端/MCP 可见账本与单次 token 上限**已实现** | 跨 Worker 2 元硬闸门**未完成**，不得作为原型已完成项 |

### 2026-09-03 可复现竞赛版本

| 交付项 | 状态 | 还需完成 |
|---|---|---|
| CAS、幂等、一次 Worker 重试、审批驳回周期、版本回滚、日志脱敏 | **已实现（代码/自动化测试）** | 目标 Docker 主机故障注入、重启与恢复演练**需实机验证** |
| `make competition-up/down/verify`、固定安装器/镜像/Skill 校验 | **已实机验证（8C/10G 目标主机）** | 全新 4C/8G 与推荐 8C/16G 两档独立主机记录仍**未生成** |
| 无 Key replay、竞赛 README、运维手册、威胁模型、第三方清单、NOTICE | **已实现** | 法务/数据负责人审核签字与演练记录 |
| OTel dispatch/MCP/LLM/artifact/review/approval 关联 | **已实现（应用 spans）** | Collector、存储、指标仪表盘、采样/留存/告警**需实机验证** |
| 完整 SBOM 和许可证清单 | 版本边界文档**已实现** | embedded 内 Matrix/Element/Higress/MinIO、Manager/Worker 传递依赖和 Neo4j digest 的 SBOM **未完成** |
| 主演示 480 秒内完成、重启不重复制品、未经 Vue 不能发布 | 防线与测试**已实现** | 同一目标环境的端到端验收和录像**未完成** |
| 所有 Worker 模型调用纳入 2 元/run fail-closed 预算 | **未完成** | 实现带 `run_id` 的 Higress/LLM 用量聚合、定价与超限拒绝，再做故障/计费验收 |

## 当前风险与边界

| 风险 | 当前控制 | 仍需运行时证明 |
|---|---|---|
| 上游快速变化 | v1.2.0 tag/commit、installer SHA、三类镜像 digest、TeamHarness 版本锁 | SBOM 与后续升级回归 |
| TeamHarness/QwenPaw 集成 | `copaw` 兼容枚举、固定镜像、9/7 节点 payload | 真实模型与 `projectflow` 端到端探针 |
| 包/后端漂移 | verify 同时检查 8 role ID、双模式 task key、system identity | CI 中增加完整依赖语义对比 |
| MCP 越权/泄密 | 原生 `spec.mcpServers`、8 条 Higress Consumer 路由、服务端 Token、权威角色路径 | 实机轮换和拒绝 impersonation 证据 |
| 外部源/百炼失败 | 幂等重试、熔断、本地 fallback、EvidenceGap | 故障注入和恢复演示 |
| 多 Agent 幻觉扩散 | backend freeze、hash、依赖验收、Judge、Reviewer、Vue | 定量引用正确率和反例集 |
| 重复/丢失 mutation | state-version CAS、幂等键、durable TeamHarness state | 重启/重复消息/并发冲突测试 |
| 授权/版权 | entitlement gate、最小引用、无全文传播 | 实际连接器权限审计 |
| 跨 Worker 总预算 | 后端/MCP run 账本、任务预算、单次 token 上限 | 带 run 维度的 Higress/LLM 聚合计量与 2 元 fail-closed 闸门 |

静态包建立了可审核边界；在真实容器、模型、MCP/Higress、Matrix、对象存储和故障恢复证据齐备前，不应宣称“生产级完成”。
