# Chengzhu AgentTeams 三分钟主演示脚本

## 演示目标与当前状态

固定案例：**宁德时代 vs 比亚迪——最近同口径财务与公告的增长质量对比**。主演示必须创建 `execution_mode=agentteams`、`analysis_mode=evidence_debate` 的实时 run，展示八角色、一次百炼官方 Skill 降级、一次确定性审计拒绝和一次 Vue 人工批准。

当前状态：

- 八角色声明、9 节点 DAG、事件 UI、审计拒绝夹具、Skill 本地降级逻辑和 Vue 审批代码：**已实现**；
- 可控“每个 run 仅视觉 Skill 第一次调用失败、随后恢复”的服务端注入：**已实现并有持久化单元测试**；
- 同一目标 Docker 主机上的完整实时 E2E 和 480 秒内完成：**需实机验证**；
- 三分钟正式录屏：**未完成**；
- 覆盖所有 Worker 模型调用的 2 元/run 硬闸门：**未完成**。

不能用 `demo_seed` 画面冒充本次实时 E2E。回放只作为故障备用，并始终显示 `replay / 合成夹具 / 0 LLM calls` 标签。

## 演示前准备

1. 在推荐规格主机运行 `make competition-up`，保存成功预检输出。
2. 准备公开财报/公告和一张公开或明确授权上传的低文本密度页面；确认视觉处理 consent 已持久化。
3. 仅在演示环境把 `.env` 的 `AGENTTEAMS_DEMO_VISUAL_FAILURE_ONCE=true`，再运行 `make competition-up`。服务端会为每个 Team run 原子记录一次 `demo_visual_failure_injected`，仅第一次 `bailian-visual-proxy` 在上游调用前失败并走本地 fallback，后续调用自动恢复。演示结束立即恢复 `false`；不要通过泄漏/删除长期 Key、影响全部 Worker 模型或上传私有材料来制造降级。
4. 固定审计反例为“将宁德时代 H1 营收与比亚迪 Q1 营收直接横向比较”。预期 `comparability_pass=false`，该 Claim 不得进入报告。
5. 浏览器预先登录本地 Vue 和 Element；关闭通知、终端历史、密码管理器弹窗，不展示 `.env`、token、完整 prompt 或隐藏推理。
6. 先开始真实 run。若端到端可能接近 480 秒，现场演示提前启动；三分钟视频用明确跳切并保留同一 `run_id`、事件 cursor 和真实时间戳，不能伪装为三分钟内完成。

建议任务卡：

```text
对比宁德时代与比亚迪最近可比报告期的营收、盈利质量、现金流与经营变化。
只使用截止时点前的公开财报、公告和授权材料；所有事实必须引用 EvidenceCard。
分析模式：evidence_debate。输出：带风险与证据缺口的对比报告。
```

## 三分钟镜头与讲解

| 时间 | 画面操作 | 讲解要点 | 必须留下的证据 |
|---|---|---|---|
| 00:00–00:20 | Vue 打开已确认任务与实时 run；显示 `agentteams`、`evidence_debate`、run ID | “成竹把投研拆成受权限约束的八人 Agent Team；AgentTeams 管协作，后端管事实、状态与发布。” | TaskCard、run ID、模式、480 秒/2 元预算说明 |
| 00:20–00:45 | 展开 Team/DAG 面板与 Element 深链 | 指出 OpenClaw Manager、Research Lead、双采集、双分析、Judge、Writer、Reviewer；同时活跃不超过 3。Matrix 只镜像摘要，不是恢复或审批依据。 | exact 8 roster、唯一 Leader、9 节点依赖、当前负责人 |
| 00:45–01:10 | 时间线筛选 Skill/MCP；展示 Disclosure Researcher 视觉调用一次失败并回退 | “官方 `alibabacloud-bailian-image-creator` 在服务端固定版本执行，Key 不进 Worker。本次只注入一次 429，系统记录 `visual_skill=degraded`，本地解析接管而不阻断研究。” | Skill commit/hash、一次失败、fallback reason、后续成功/本地结果；不得出现 Key |
| 01:10–01:35 | 展示双采集交接、freeze 系统节点和 MinIO ArtifactRef | “Lead 调 `freeze_evidence`，校验 state version、manifest hash 与 ArtifactRef，再在 TeamHarness 接受系统节点；它不是第九个 Worker。” | collector handoff、freeze bridge、content/provenance hash、CAS 版本 |
| 01:35–02:05 | 展开 Quality/Growth 交叉质疑与 Evidence Judge 审计 | 高亮 H1/Q1 混比 Claim。说明确定性 Auditor 以 `comparability_pass=false` 拒绝，Judge 无权覆盖；另一个同口径 Claim 通过。 | rejected claim ID、issue code、accepted claim ID、审计失败未进入 allowed set |
| 02:05–02:30 | Writer/Reviewer 状态与报告引用跳转 | “Writer 只能使用裁决通过的 claim；Reviewer 校验精确 hash，报告中的事实可回到 EvidenceCard。” | draft/review hash 相同、引用定位、失败 Claim 在正式报告中数量为 0 |
| 02:30–02:50 | Vue 点击批准，显示 expected version；刷新 published 状态与 Matrix 镜像事件 | “只有 Vue 能批准。Matrix 的人类消息没有发布权；批准后后端发布不可变版本，回滚也只切 latest 指针。” | approval actor/decision/version、published artifact hash、`human.approved` 镜像事件 |
| 02:50–03:00 | 指标卡与架构总览 | “后端可见调用受 480 秒/2 元账本约束；Worker 单次输出有 token 上限。但 v1.2.0 缺少按 run 汇总全部 Worker 用量，跨 Worker 2 元硬闸门尚未完成。” | 阶段耗时、重试/降级、后端可见费用、Worker 用量缺口标签 |

## 预算说明的固定口径

演示中只能说：

> 本 run 的 Chengzhu 后端/MCP 可见调用受 480 秒和 2 元账本约束，Worker 单次调用受 `AGENTTEAMS_MODEL_MAX_TOKENS` 限制。AgentTeams v1.2.0 尚不能按 Chengzhu run 汇总所有 Worker 模型费用，因此完整跨 Worker 2 元硬闸门仍需带 run 维度的 Higress/LLM 预算代理。

不得把任务预算分配、三 Worker 并发限制或单次 token 上限描述为已完成的总费用硬门。如果视频中的总成本只覆盖后端账本，标签必须写“backend/MCP visible cost”，不能写“all agents total”。

## 录制与提交证据清单

- `make competition-up` 与 `make competition-verify` 的脱敏成功输出；
- Git commit、AgentTeams/Skill lock、三类镜像 digest、8 个 ZIP `SHA256SUMS`；
- 同一 `task_id/run_id/team_id/project_id/trace_id` 贯穿全部镜头；
- Team `Active`、八角色、唯一 Leader、最多三活跃 Worker；
- 9 节点 Project/Task durable state 和 Matrix room/event ID；
- 一次受控视觉 Skill 失败、`visual_skill=degraded` 与本地 fallback；
- freeze MCP 请求的角色、幂等键/CAS 结果、MinIO manifest/hash；
- H1/Q1 Claim 的确定性拒绝和正式报告“拒绝 Claim 数量=0”；
- Writer/Reviewer 精确 hash、Vue approval CAS 和 published 版本；
- 阶段耗时、后端可见 token/费用、重试和审批耗时；
- 日志/视频经过密钥、Base64、私有数据、原始 prompt 和隐藏推理扫描；
- 视频说明文件明确跳切点、真实 run 开始/结束时间和当前预算能力缺口。

## 备用回放

如果现场模型或网络不可用，可透明切换：

```bash
python3 scripts/load_demo.py --force
pnpm run dev
```

打开 `task_demo_catl_byd_debate`，可只读展示八角色/九节点 Team 快照、交接、一次合成降级、同口径通过、H1/Q1 混比拒绝、观点撤回和历史 Vue 批准记录；所有记录均带 `replay`/`synthetic_fixture` 边界，API 会拒绝重新执行、审批和回滚。备用回放只证明 UI、引用、状态与审计叙事可浏览，不证明 AgentTeams、官方 Skill、Matrix、MinIO 或模型当场运行成功；录屏和讲解必须明确这一点。
