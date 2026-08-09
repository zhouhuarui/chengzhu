# Chengzhu AgentTeams 第三方依赖、版本与归属

## 使用原则

Chengzhu 原创及 MiroFish 派生部分按仓库中的 AGPL-3.0 边界发布；第三方组件、容器、模型、Skill、协议、商标和托管服务仍适用各自许可证或服务条款，不能被 Chengzhu 的许可证重新授权。

本表只记录仓库能证明的版本和许可证。写“待 SBOM/上游确认”不表示该组件没有许可证，而表示当前锁文件不足以支持公开分发或法律结论。交付前应由维护者和法务以实际拉取的镜像/包为准复核。

## 直接组件清单

| 组件 | 当前版本或边界 | 复现来源与完整性 | 许可证/归属边界 | 当前状态 |
|---|---|---|---|---|
| AgentTeams | tag `v1.2.0`；commit `793db242257a569d911b1aa59c1cd554af78511f` | [`../agentteams/UPSTREAM.lock`](../agentteams/UPSTREAM.lock) 锁定仓库、安装器 URL/SHA-256 和三类 OCI digest；上游 <https://github.com/agentscope-ai/AgentTeams> | 锁定源码根 `LICENSE` 为 Apache-2.0；版权及商标归上游贡献者，本仓库不重授权控制器、CLI、镜像或 notices | 已锁定；镜像内传递依赖仍需 SBOM |
| AgentTeams embedded Controller | `v1.2.0@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4` | `UPSTREAM.lock` 与启动/预检精确比对 | 顶层 AgentTeams 为 Apache-2.0；embedded image 内 Tuwunel、MinIO、Higress、Element 等保留各自条款 | 镜像已锁；内部清单待 SBOM |
| OpenClaw Manager | AgentTeams Manager `v1.2.0@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e` | `UPSTREAM.lock`；`competition-up`/preflight 拒绝不同镜像 | AgentTeams 集成代码受其 Apache-2.0；镜像内 OpenClaw 及传递包的精确版本/许可证需从实际镜像 SBOM 确认 | 镜像已锁；内部清单待 SBOM |
| TeamHarness | plugin `0.1.0`，随同一 AgentTeams commit | `UPSTREAM.lock` 与上游 `plugins/teamharness/plugin.yaml`；本仓库固定 9/7 节点 payload | 锁定源码位于 AgentTeams Apache-2.0 仓库，未见独立 license 覆盖；若镜像/发行包附带单独 notices，以发行包为准 | 已锁定源码边界 |
| CoPaw / 产品所称 QwenPaw Worker | manifest 枚举 `runtime: copaw`；镜像 `v1.2.0@sha256:dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc` | `UPSTREAM.lock`、八份 Worker manifest、预检精确比对。锁定源码中 `copaw-worker` 为 `1.0.3`，声明依赖 `copaw==1.0.2` | `copaw-worker` 的锁定 `pyproject.toml` 声明 Apache-2.0；底层 `copaw`、模型 SDK 及镜像传递依赖需 SBOM/包 metadata 复核 | Worker 镜像已锁；传递依赖待 SBOM |
| 独立 QwenPaw 包说明 | 锁定 AgentTeams 源码另含 `qwenpaw-worker 0.1.0` / `qwenpaw 1.1.11`，但 Chengzhu 当前 manifest **没有选择该独立 runtime/image** | 上游同一 commit 的 `qwenpaw/pyproject.toml` | 上游文件声明 Apache-2.0；不要把“源码中存在”写成“Chengzhu 已部署” | 非当前部署组件 |
| Matrix 协作面 | Matrix API 由 AgentTeams embedded image 提供；实际实现可从 `/_tuwunel/server_version` 识别为 Tuwunel 路径 | 复现边界是固定 embedded image digest；运行时保存版本端点输出与 SBOM | Matrix 是协议/生态；实际 Tuwunel 二进制版本和许可证未在 Chengzhu lock 中单独记录，须以镜像 SBOM/notices 为准 | 外层镜像已锁；内部版本/许可证待确认 |
| Element Web | 由固定 AgentTeams embedded image 在本地端口提供；Chengzhu 未单独锁 Element 版本 | 保存实际页面 build/version、镜像 digest 和 SBOM | Element 软件和商标归其上游；当前仓库未携带足以确认该内嵌构建许可证/版本的独立 lock | 待实机 SBOM/版本证据 |
| Higress | MCP/LLM 网关能力由 AgentTeams embedded 部署提供；Chengzhu 固定八条 route/Consumer 配置，但未单独锁 Higress build version | 固定 embedded image digest；保存控制台 build 信息、导出配置和 SBOM | 许可证及传递插件条款以实际 embedded image notices/SBOM 为准；Chengzhu 仅授权自有路由配置 | 待实机 SBOM/版本证据 |
| MinIO | 实时制品服务由 AgentTeams embedded image 提供；bucket `agentteams-storage` | 固定 embedded image digest；预检要求 put/get/delete；实机保存实际 server build/SBOM | 当前 lock 未单列 MinIO build 与许可证文件；不得按记忆臆造，交付时以镜像 notices/SBOM 为准 | 待实机 SBOM/版本证据 |
| Neo4j | Compose 使用 `neo4j:5.26`，未固定 patch 或 OCI digest | [`../docker-compose.yml`](../docker-compose.yml)；部署时记录解析后的 RepoDigest | 具体 edition、许可证及服务条款以实际镜像 metadata/notices 为准；不能仅凭 tag 推断 | **复现缺口**：需锁 digest 与确认许可证 |
| 百炼官方 `alibabacloud-bailian-image-creator` Skill | commit `92bd723f7cc217b252feab574c1883fa0aa46b3c`；固定四个文件 hash | [`../agentteams/OFFICIAL_SKILLS.lock`](../agentteams/OFFICIAL_SKILLS.lock)；上游 <https://github.com/aliyun/alibabacloud-aiops-skills>；启动时下载并逐文件校验 | 当前运行时只抓取四个文件，不含足以在本仓库独立判定许可证的顶层 license；必须保留上游归属并在该 commit 确认许可证。百炼服务、模型、商标和上传内容另受阿里云条款约束 | 文件已锁；许可证/服务条款待交付审核 |
| OpenTelemetry Python | `opentelemetry-api/sdk/exporter-otlp-proto-http >=1.25,<2.0`，未锁精确安装版 | [`../backend/requirements.txt`](../backend/requirements.txt)；部署时保存 `pip freeze` 和 SBOM | 实际安装包许可证以 Python package metadata 和上游 notices 为准；OTLP Collector 不随本栈交付 | **复现缺口**：需锁精确版本/SBOM |

模型 `qwen3-30b-a3b-instruct-2507`、`qwen3.5-plus`、DashScope API 不是随仓库再分发的软件；其可用区域、计费、数据处理和使用条款以比赛部署账号当时的阿里云合同为准。模型名称锁定不等于模型权重或服务行为可离线复现。

## 交付时生成 SBOM

在目标 Docker 主机完成拉取后，至少保存：

```bash
git rev-parse HEAD
bash agentteams/scripts/verify.sh
docker image inspect \
  higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.0@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4
docker image inspect \
  higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager:v1.2.0@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e
docker image inspect \
  higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.0@sha256:dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc
docker image inspect neo4j:5.26
backend/.venv/bin/python -m pip freeze
pnpm list --depth Infinity
```

再使用组织批准的 SBOM 工具对四类实际镜像生成 CycloneDX 或 SPDX 文件，记录工具名称、版本、命令、生成时间和 SBOM SHA-256。`docker sbom` 或 `syft` 仅在目标主机已安装并经批准时使用；本文不假定它们存在。

SBOM 审核至少回答：

- embedded image 中 Tuwunel、Element、Higress、MinIO 的精确版本和许可证；
- Manager 中 OpenClaw 的精确版本/许可证；
- CoPaw Worker 的底层 runtime、Matrix SDK、模型 SDK 版本与许可证；
- Neo4j 实际 edition、digest 和许可义务；
- Python/Node 锁文件与镜像内实际安装结果是否一致；
- 是否存在 copyleft、source-offer、NOTICE、商标或不可再分发条款；
- 已知高危漏洞、缓解措施和接受人。

## NOTICE 与提交边界

- 根目录 [`../NOTICE`](../NOTICE) 保留 AgentTeams 与官方 Skill 的来源和不重授权声明。
- 竞赛提交可包含 Chengzhu 自有代码、manifest、hash、SBOM 和必要 notices；不要未经核准重新分发第三方容器层、模型权重、私有数据或云服务响应全集。
- `demo_seed` 只能包含合成或明确可公开的数据；Datayes 私有原文和未授权上传材料不得进入提交包。
- 第三方版本升级必须同时更新 lock、hash、SBOM、NOTICE、威胁模型、运维恢复演练和 E2E 证据，不能只改 tag。
