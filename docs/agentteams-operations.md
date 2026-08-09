# Chengzhu AgentTeams 竞赛运维手册

## 适用范围与状态

本文适用于 AgentTeams `v1.2.0` 单机 Docker 竞赛栈。实时任务只使用 AgentTeams；`demo_seed` 是独立的只读回放路径。

状态标记：

- **已实现**：仓库已有脚本、配置或确定性校验；
- **需实机验证**：实现已存在，但必须在目标 Docker 主机、真实模型与网络上留存证据；
- **未完成**：当前仓库没有可声称完成的闭环。

截至 2026-08-09，目标 Docker 主机已通过 `competition-up`：固定八角色 Team、两类真实模型一 Token 探针、MinIO 读写删除、八条角色隔离 MCP 路由、认证 `tools/list`、匿名拒绝、回环端口和容器网络隔离均已实测；原生 Neo4j 的 473 节点/462 关系也已通过校验和、单事务和数量/约束校验迁入 Docker 卷。真实付费任务的同一 run 全闭环、完整恢复演练、密钥轮换和故障注入仍是**需实机验证**。

## 主机与准备

- 推荐 8 CPU / 16 GB RAM，最低 4 CPU / 8 GB RAM；Docker 24+ 与 Compose v2。
- 安装 Bash、Ruby、`zip`、`unzip`、`curl`、`jq`、`openssl` 和 Python 3。
- 复制 `.env.example` 为 `.env`，填入 AgentTeams 管理密码、MinIO、MCP 和 DashScope 等服务端凭证；执行 `chmod 600 .env`。
- Controller、Element、Higress 控制台、MinIO、Neo4j、后端和前端固定只绑定本机回环地址；如需远程演示，使用 SSH 隧道，或先加 VPN/反向代理身份认证。
- 实时模式必须有可用模型 Key；无 Key 时不要关闭预检或伪造实时结果，应使用只读 replay。

启动前先执行不联网的合同检查：

```bash
bash agentteams/scripts/verify.sh
```

`make competition-verify` 同时包含实时预检，因此用于服务启动后复验，在服务尚未启动时失败是预期行为。

## 正常启停

### 启动

```bash
cp .env.example .env
# 填写服务端凭证并收紧权限
chmod 600 .env
make competition-up
```

`competition-up` 会：

1. 建立或验证仅回环绑定的 `agentteams-net`；
2. 校验固定 AgentTeams v1.2.0 镜像、安装器 SHA-256 和官方 Skill 文件哈希；
3. 安装控制面，或复用镜像和单次 token 上限完全匹配的健康控制面；
4. 启动 Chengzhu、Neo4j、MCP 等 Compose 服务；
5. 首次创建八 Worker/一 Team；已有 Team 默认不重放 manifests；
6. 配置八条角色隔离的 Higress MCP 路由；
7. 自动运行完整预检。

看到 `Chengzhu AgentTeams competition stack is ready.` 才表示本次启动预检通过。保存完整终端输出，但必须先做密钥脱敏。

### 停止

```bash
make competition-down
```

停止过程先核验固定八角色，再让 Worker 休眠并停止 Compose 服务。只有 `.agentteams/owner` 能证明当前项目拥有 Controller/Manager 时，脚本才停止该控制面；共享或无法证明归属的控制面保持运行。该命令不删除容器数据卷、SQLite、MinIO 制品或历史报告。

如果脚本报告 Worker 未完全休眠，它会留下需要人工检查的失败状态；不要直接强杀或删除数据卷，应先检查控制面、Team 与当前 run。

## 健康判定与预检

运行中的环境可重复执行：

```bash
make competition-verify
```

完整通过必须同时满足：

| 层 | 健康标准 |
|---|---|
| 静态包 | 8 Worker、1 Team、8 Skills、9 节点 debate 与 7 节点 direct DAG、ZIP/SHA256SUMS 全部一致 |
| 容器与网络 | Controller/Manager 运行；固定 digest；所有发布端口只绑定 `127.0.0.1`/`::1`；`agentteams-net` 是带回环绑定选项的 bridge |
| Chengzhu | `http://127.0.0.1:5001/health` 返回 `status=ok`；前端 `http://127.0.0.1:3000/` 可访问 |
| MCP | 容器内 `/health` 报告协议 `2024-11-05`；八条 Wasm 规则均启用；匿名请求被 401/403 拒绝；Leader 认证后只看到 4 个角色专属工具 |
| Team | `chengzhu-research-team` phase 为 `Active`；Leader Ready；恰好八角色；唯一 Leader 是 `research-lead` |
| Worker | 模型、runtime、镜像和初始生命周期与 manifest 完全一致；同时活跃不超过 3 |
| Higress | Chengzhu service source 指向 Docker 网络别名 `chengzhu-mcp.agentteams.io:5002`；每条 MCP 路由只有对应 `worker-<role-id>` Consumer |
| MinIO | 临时对象 put/stat/get/content compare/delete 全部成功 |
| 模型 | `qwen3-30b-a3b-instruct-2507` 与 `qwen3.5-plus` 均完成不可禁用的一 Token 探针 |

预检不等于业务 E2E。还需创建一条真实 `evidence_debate` run，检查 TeamHarness 交接、freeze bridge、审计、Reviewer 与 Vue 审批后，才能称该主机可用于主演示。

## Manifest reconcile 安全窗口

已有 Team 默认跳过 manifest 应用，避免把活跃 Worker 重置到声明的初始 `Sleeping` 状态。只有满足以下全部条件才能 reconcile：

1. 已完成一致性备份；
2. Vue 和后端确认没有非终态 `agent_team_run`；
3. Controller 显示除 Leader 外没有 `running/ready/starting` Worker；
4. Matrix/Element 没有尚未验收的任务交接；
5. 操作人记录维护窗口、当前 Team/Worker JSON 和预期变更。

然后仅对本次命令设置：

```bash
AGENTTEAMS_RECONCILE_MANIFESTS=1 make competition-up
```

脚本会再次检查活跃 Worker 和后端 run；任一不满足即拒绝。不要为了通过检查而直接改数据库、强制 sleep 或删除 Team。reconcile 是 upsert，不是 prune，也不是状态恢复工具。

## 备份与恢复

### 必须备份的状态

| 位置 | 内容 | 机密性 |
|---|---|---|
| `backend/uploads/` | `chengzhu.db`、任务/run、本地兼容镜像、日志 | 含用户材料与研究数据 |
| Docker volume `agentteams-data` | AgentTeams 控制面、Tuwunel/Matrix、MinIO、Higress 与协作状态 | 高度敏感，可能含凭证/消息 |
| Compose 的 `neo4j_data` volume | 图谱数据库 | 含研究关系与来源 |
| `.agentteams/` | Manager workspace、host share、env、token 文件和 owner 标记 | 高度敏感 |
| `.env` | 服务端密钥与部署参数 | 最高敏感，不得进入普通竞赛包 |
| Git commit、`UPSTREAM.lock`、`OFFICIAL_SKILLS.lock`、镜像 digest、SBOM | 恢复所需的软件来源 | 可作为脱敏交付证据 |

### 一致性备份流程

1. 记录所有非终态 run；有活跃写入时先正常完成或进入明确失败终态。
2. 执行 `make competition-down`，确认没有 Worker/后端继续写入。
3. 创建权限为 `0700` 的专用备份目录；分别归档 `backend/uploads/`、`.agentteams/` 和 `.env`，生成 SHA-256 清单。
4. 使用组织批准且**固定 digest**的归档工具，以只读挂载方式导出 `agentteams-data` 和实际的 Neo4j volume。不要在文档中硬编码 Compose 自动生成的 Neo4j volume 名；先通过 Docker label/inspect 确认唯一目标。
5. 加密包含密钥、Matrix 消息或用户上传内容的归档，并与解密材料分开保存。公开提交只包含脱敏清单、hash 和恢复演练结果。

卷名发现示例（只读）：

```bash
docker volume inspect agentteams-data
docker volume ls --filter label=com.docker.compose.volume=neo4j_data
```

归档镜像、命令和最终 digest 必须写入本次备份记录。当前仓库未提供固定归档镜像或自动备份脚本，因此该流程为**需实机验证**。

### 恢复流程

1. 在隔离主机检出备份记录中的同一 Git commit，先运行 `bash agentteams/scripts/verify.sh`。
2. 保持所有服务停止，校验每个归档的 SHA-256；恢复 SQLite/上传目录与两个 Docker volume。
3. `.agentteams/owner` 是“本机项目拥有当前容器 ID”的证明，不能跨主机直接当作新所有权。跨主机恢复时保留它作为审计附件，但让新安装生成新的 owner 标记；同主机原容器恢复才保留原标记。
4. 以最小凭证启动 `make competition-up`；不要先开 manifest reconcile。
5. 运行完整预检，再核对 Team/run 状态、MinIO ArtifactRef/hash、SQLite `latest` 指针、Matrix room ID 和 Neo4j 关键实体。
6. 用一个非发布测试 run 验证幂等恢复；确认没有重复 EvidenceCard、重复制品或重复扣费后再开放新任务。

不得把单独恢复 SQLite、单独恢复 MinIO 或重放 Matrix 消息当成完整恢复。Matrix 是协作镜像，不是恢复依据；SQLite 状态版本、幂等结果和不可变制品必须匹配。

## 常见故障处置

| 现象 | 处置 | 禁止做法 |
|---|---|---|
| 控制面镜像或 token 上限不匹配 | 停止启动，核对 lock、owner 和现有部署来源，在维护窗做显式升级/重建 | 自动替换未知控制面 |
| Team 非 `Active` 或 roster 不精确 | 保存 Team/Worker JSON 与日志，检查 manifest/package apply 和 Leader；修复后重跑预检 | 删除 Team 规避状态 |
| MCP 401/403（合法 Worker） | 检查对应 Consumer、角色路由、服务端 token 文件和 header 覆写；轮换后同步重启/重配 | 给所有 Worker 共用管理员 token |
| MCP CAS 冲突 | 读取最新 state version，保持原幂等键并重新决策 | 盲重试或手工改版本 |
| MinIO round trip 失败 | 暂停实时任务，检查 volume、凭证、bucket、空间和网络；恢复后校验 hash | 在竞赛实时模式静默降级到可变本地文件 |
| 官方视觉 Skill 超时/429 | 确认仅本次视觉解析标记 `visual_skill=degraded` 且使用本地解析；保留 request/fallback 事件 | 把 Key 注入 Worker 或上传私有原文重试 |
| 一个采集 Worker 失败 | 按同 task/idempotency key 重试一次；另一组成功时走 `completed_partial` 并披露缺口 | 重复创建 EvidenceCard |
| 两个采集 Worker 均失败 | 将 run 置失败并保留证据缺口 | 用模型臆造采集结果 |
| Worker 崩溃 | 从 SQLite/TeamHarness 持久状态恢复并最多重试一次，核对原 ArtifactRef | 仅凭 Matrix 文本推断完成状态 |
| 审计拒绝 Claim | 保持拒绝，确保 Writer 输入集中不存在该 Claim | Judge/Reviewer 覆盖硬失败 |
| Vue 审批卡住 | 核对 awaiting 状态、精确报告 hash、expected_version 和 pending approval | 在 Matrix 发“批准”或调用 Worker 发布 |
| 时间/费用接近上限 | 后端停止可见的新调用，生成证据缺口；记录 Worker 外部用量可见性缺口 | 声称跨 Worker 2 元硬闸门已完成 |

演示受控降级时，只能在服务端设置
`AGENTTEAMS_DEMO_VISUAL_FAILURE_ONCE=true`。它以 SQLite 事件原子占用每个
Team run 的第一次视觉调用，后续调用自动恢复；本地 fallback 禁止再创建
第二个远程视觉客户端。演示完成后恢复 `false`。

## 密钥轮换

所有轮换都在维护窗口进行：完成备份、停止新任务、记录旧凭证标识（不记录密钥）、轮换、重启依赖方、跑完整预检，再撤销旧密钥。

- **MCP gateway token**：备份当前 token 文件的 hash，生成新的 32 字节随机值写入权限 `0600` 的临时文件，再原子替换 `.agentteams/chengzhu-mcp-token`；重启 backend/MCP 并重新配置 Higress，验证八条合法路由和八条匿名拒绝。保留旧文件只应采用加密、限时、可恢复的方式。
- **DashScope/AgentTeams 模型 Key**：在服务端密钥库/`.env` 更新，使用 AgentTeams v1.2.0 支持的凭证更新流程重启 Manager/Worker。当前脚本不会把“运行中容器已换 Key”当作自动保证；必须以双模型一 Token 探针和一次 Worker 调用证明。
- **MinIO、Matrix 管理员或 Higress 管理凭证**：这些凭证存在于 AgentTeams 持久卷和多个消费者之间。当前仓库没有原子轮换脚本，应遵循固定上游版本的管理流程，逐一更新服务与客户端并做恢复演练；不要只改 `.env` 的一侧。
- **OTLP headers**：在 Collector 与 `.env` 两侧轮换，确认 trace 到达后撤销旧值；任何 header 都不得出现在 AgentLog 或竞赛截图。

轮换自动化仍是**未完成/需实机补齐**的运维能力，不能只凭环境变量模板宣称完成。

## OpenTelemetry

即使没有 Collector，后端也会生成并持久化安全的 `trace_id/span_id`。启用 OTLP 导出时配置：

```dotenv
OTEL_SDK_DISABLED=false
OTEL_SERVICE_NAME=chengzhu-backend
OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-from-container-network>:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=
OTEL_TRACES_EXPORTER=otlp
```

容器中的 `localhost` 指向容器自身，Collector 地址必须从 backend 和 `chengzhu-mcp` 容器可达。重启后以同一 `run_id/trace_id` 检查 dispatch、MCP、LLM、freeze、artifact、review、approval/rollback spans；禁止导出原始 prompt、隐藏推理、Base64、密钥和私有原文。

仓库已实现 OTLP bridge 和关键 spans，但没有随栈提供 Collector/存储/仪表盘；端到端 trace 连通、留存期、采样和告警属于**需实机验证**。

## 无 Key 只读回放

无 AgentTeams、MinIO 和模型 Key 时：

```bash
python3 scripts/load_demo.py --force
pnpm run dev
```

`--force` 会覆盖同名本地任务，执行前先备份 `backend/uploads/`。回放任务明确为 `execution_mode=replay`，只能浏览已有报告、证据、图谱和事件；不能继续执行、重新审批或伪装成实时 run。主演示实时链路故障时可把 replay 作为透明标注的备用展示，但不能将其录制成“AgentTeams 实时 E2E”证据。
