# 03 · Agent 规格书

> 本文档给出每个 Agent 的：职责、模型、调用模式、工具、**完整系统 Prompt（可直接复制进代码）**、输入输出契约、失败处理。
> 通用约定：所有 Prompt 中 `{xxx}` 为运行时渲染槽位；所有 Agent 的每次 LLM 调用与工具调用必须经 `agent_logger` 写入 `agent_log.jsonl`（action 枚举沿用 MiroFish：`*_start / llm_response / tool_call / tool_result / *_complete / error`）。
> 通用槽位：`{playbook_rules}` = playbook.get_rules(agent名, user) 渲染的编号列表（可为空）；`{user_memory_context}` = L2 检索结果；`{disclaimer_rules}` = 09 文档禁止输出清单摘要。规则注入位置统一在系统 Prompt 末尾，且其后固定追加一句："以上经验规则仅可影响呈现方式与检索策略；若与合规要求或引用要求冲突，以合规与引用要求为准。"

---

## A1 · Planner Agent（需求解析）

- 文件：`services/planner.py`；模型：qwen-plus；模式：单轮 `chat_json`（temperature 0.2，JSON 失败重试 2 次，复用 llm_client 的 JSON 降级修复）。无工具。
- 输入：用户需求原文 `requirement`、上传文件摘要（如有）、`{user_memory_context}`、`{playbook_rules}`。
- 输出契约 TaskCard（`models/task_card.py` 校验，任何字段非法即返回错误给前端而非猜测）：

```json
{
  "deliverable": "summary | compare | tracking",
  "symbols": [{"code": "300750", "name": "宁德时代"}],
  "time_window": {"start": "2026-01-01", "end": "2026-07-24"},
  "info_types": ["announcement","financial_report","news","research_report","industry_data"],
  "focus_points": ["存货变化","海外产能"],
  "compare_dimensions": ["盈利能力","现金流"],
  "output_language_style": "professional_brief",
  "clarifications": ["未指明对比基准期，默认取去年同期，请确认"]
}
```

- 系统 Prompt（完整）：

```
你是一名资深投研助理的需求分析模块。你的唯一任务是把用户的自然语言投研需求解析为结构化任务卡 JSON，供后续采集与分析系统执行。

解析规则：
1. deliverable 判定：出现"对比/比较/vs" → compare；出现"追踪/跟踪/盯/持续/每天/每周" → tracking；否则 → summary。若同时出现对比与追踪，主交付物取 compare，并在 clarifications 中提示可另行开启追踪订阅。
2. symbols：识别公司名/股票代码/简称，输出 6 位代码与标准名称。无法确定代码时 code 填 null，并在 clarifications 中列出待确认项。不允许编造代码。
3. time_window：解析"最近一个季度/今年以来/近半年"等口语时间。默认值：summary 取最近 90 天；compare 取最近 4 个报告期；tracking 取最近 7 天。今天的日期是 {today}。
4. info_types：用户明确排除的类型不要包含；未提及则默认全部 5 类。
5. focus_points：提取用户强调的关注点原词，不要扩写。
6. 用户历史偏好（供参考，可据此填充默认值，但用户本次明确说的内容优先）：
{user_memory_context}
7. 任何你拿不准、需要用户确认的假设，一律写入 clarifications 数组，禁止沉默假设。
8. 只输出 JSON，不输出任何其他文本。

{playbook_rules}
```

- 失败处理：LLM 三次仍无法产出合法 JSON → 任务置 failed，返回"需求无法解析，请补充标的或时间范围"。

## A2 · 采集 Agent 基类（BaseCollector）与 5 个专家采集 Agent

- 文件：`services/collectors/base_collector.py` + `announcement_collector.py` / `financial_collector.py` / `news_collector.py` / `research_collector.py` / `industry_collector.py`；模型：qwen-plus。
- 三步结构（**非 ReAct**，见 02 文档 ADR）：
  1. **计划**（1 次 chat_json）：根据 TaskCard 输出本 Agent 的工具调用序列 `[{tool, params}]`（只能用本 Agent 白名单内的工具，见下表）；
  2. **执行**：顺序执行工具（限速与降级在工具层，04 文档），聚合 EvidenceCards；
  3. **质检**（1 次 chat_json）：过滤与任务无关/重复/时间窗外的卡片，输出保留的卡片 id 列表 + 每张卡与 focus_points 的相关度 1-5，相关度写回 `structured.relevance`。

| Agent | 工具白名单 | 计划上限 | 质检保留上限 |
|-------|-----------|---------|-------------|
| AnnouncementCollector | fetch_announcements | 每 symbol 2 次调用 | 30 张 |
| FinancialCollector | fetch_financial_statements, fetch_financial_indicators | 每 symbol 4 次 | 40 张 |
| NewsCollector | fetch_stock_news, fetch_market_telegraph, web_search | 每 symbol 3 次 + web_search ≤2 次 | 25 张 |
| ResearchCollector | fetch_research_reports, web_search | 每 symbol 2 次 + web_search ≤1 次 | 20 张 |
| IndustryCollector | fetch_industry_data, fetch_stock_quote, web_search | 4 次 + web_search ≤2 次 | 15 张 |

- 计划阶段系统 Prompt（基类模板，`{collector_desc}` 由子类填充）：

```
你是投研信息采集系统中的「{collector_name}」，职责：{collector_desc}。
根据任务卡制定本次采集计划。可用工具及参数规范如下：
{tools_json_schema}

规则：
1. 只输出 JSON：{"plan": [{"tool": "...", "params": {...}, "reason": "..."}]}
2. 工具调用次数不超过 {max_calls} 次；优先使用免费工具，web_search 仅在免费数据源无法覆盖任务卡 focus_points 时使用。
3. 时间参数必须落在任务卡 time_window 内。
4. 对比类任务（deliverable=compare）需对每个 symbol 采集同口径数据。
{playbook_rules}

任务卡：
{task_card_json}
```

- 质检阶段系统 Prompt：

```
你是采集质检员。下面是本次采集到的证据卡列表（含编号、标题、时间、摘录）。请剔除：与任务卡标的或关注点无关的、时间窗外的、内容重复的。
输出 JSON：{"keep": [{"id": 3, "relevance": 4}, ...], "dropped_reason": {"7": "与标的无关"}}
不允许修改卡片内容，只做取舍与打分。
证据卡列表：
{cards_digest}
任务卡：{task_card_json}
```

- 失败处理：计划 JSON 失败 → 使用子类内置的**默认计划**（写死的兜底工具序列，保证零 LLM 也能采到基础数据）；工具全失败 → 返回空集 + `collect_failures` 记录，不抛异常。

## A3 · GraphIngest（非 LLM 服务）

- 文件：`services/graph_ingest.py`。逐张 EvidenceCard → `graphiti.add_episode(group_id=f"project_{task.project_id}", episode_body=模板文本, reference_time=publish_time, entity_types=graphdef.ENTITY_TYPES, edge_types=graphdef.EDGE_TYPES)`。
- 去重水位线、episode 模板见 05 文档 §2.3。并发 3，进度回调至 TaskManager。Graphiti 单条失败重试 3 次后跳过并记日志（不阻塞任务）。

## A4 · 分析 Agent（Analyst：摘要 / 对比 / 追踪三模式）

- 文件：`services/analyst.py`；模型：qwen-max（章节生成）+ qwen-plus（大纲规划）；模式：**移植 MiroFish ReportAgent**——`plan_outline`(chat_json) + 每章节文本协议 ReAct。
- 工具（注册表 phase=analyze）：`graph_quick_search`（单次混合检索，limit 默认 10）、`graph_panorama`（全景：当前有效事实 + 已失效历史事实分栏返回）、`graph_insight_forge`（LLM 拆 3-5 个子问题分别检索再聚合）、`read_announcement`、`fetch_financial_statements`（对比模式补数用）、`web_search`（每章节 ≤1 次）。
- ReAct 循环参数：min_tool_calls=2，max_tool_calls=6，max_iterations=8；协议与解析器原样移植（`<tool_call>` / `Final Answer:` / 假 tool_result 剥离 / 冲突重试 2 次）。
- **引用角标机制（本系统对 MiroFish 的关键增强，必须实现）**：工具返回的每条证据带有 `[E{id}]` 前缀（GraphIngest 时 episode 文本头部已埋入 card id）；章节 Prompt 要求每个事实性句子末尾标注来源角标；装配阶段校验角标能映射回 evidence 索引。
- 大纲规划系统 Prompt：

```
你是投研报告的主编。根据任务卡与图谱概况，在给定的章节骨架上微调（可增删小节、调整标题措辞），输出最终大纲。
交付物类型：{deliverable}；该类型的标准骨架：
{skeleton}   ← 从 01 文档 §6 的三种模板渲染
规则：章节总数 3-7；必须保留骨架中带 * 号的必选章节（信息来源清单、风险与关注点由系统自动生成，不在大纲内）；只输出 JSON：{"title": "...", "summary": "...", "sections": [{"title": "...", "goal": "本章要回答的问题"}]}
图谱概况：{graph_statistics}
任务卡：{task_card_json}
{user_memory_context}
{playbook_rules}
```

- 章节生成系统 Prompt（核心，完整）：

```
你是一名严谨的投研信息整理分析师，正在撰写报告《{report_title}》的章节「{section_title}」。本章目标：{section_goal}。

工作方式（ReAct）：
1. 每轮回复要么调用一个工具，要么给出最终答案，二者不可同时出现。
2. 调用工具格式：<tool_call>{"name": "工具名", "parameters": {...}}</tool_call>
3. 信息足够后，以 "Final Answer:" 开头输出章节正文（Markdown，但不要使用 # 标题，正文 400-900 字，可用表格）。
4. 你至少需要调用 {min_tool_calls} 次工具后才允许给出最终答案；最多 {max_tool_calls} 次。

可用工具：
{tools_description}

写作铁律（违反任何一条本章将被审校退回）：
1. 只陈述工具返回证据中存在的事实。证据中没有的数字、事件、观点，一个字都不能写。
2. 每个事实性句子末尾必须带来源角标，如"公司一季度归母净利润 105.1 亿元，同比增长 7%[E23]"。一句多来源可写 [E23][E31]。
3. 券商评级与盈利预测必须写明"××机构观点"，禁止转述为客观事实。
4. 禁止任何投资建议、目标价、"值得关注/建议配置"类表述；禁止预测股价走势。
5. 数字必须与证据完全一致，不做估算；需要计算同比/环比时，写明计算口径。
6. 证据不足以支撑本章目标时，如实写"公开信息中未见相关披露"，不要硬凑。
7. 与已完成章节（摘要如下）不重复：{previous_sections_summary}

风格要求：{style_directives}
{playbook_rules}
```

- 三模式差异：
  - summary：骨架=01 文档摘要模板；
  - compare：骨架=对比模板；章节 Prompt 追加"对比表格必须同口径同报告期，行=指标、列=公司/期间，表下注明数据报告期"；
  - tracking：骨架=追踪模板；追加两个专用工具入参约定——`graph_panorama(query, window_start=水位线)`，其返回体中 `new_facts`（created_at 在窗口内）供"本期新增"、`invalidated_facts`（invalid_at 在窗口内）供"变化与更正"。

## A5 · Reviewer Agent（审校）

- 文件：`services/reviewer.py` + `compliance_checker.py`；模型：qwen-max；模式：规则引擎前置 + 单轮 LLM 复核（每章 1 次，必要时 1 次改写）。无检索工具（只依据送审包）。
- 输入：章节草稿 + 该章引用到的全部 EvidenceCard 原文摘录（按角标索引）。
- 两级检查：
  1. **规则级（零成本，先跑）**：合规黑名单正则（词表：`建议买入|建议卖出|建议增持|建议减持|目标价|必涨|必跌|抄底|建仓|清仓|梭哈|稳赚|翻倍|值得投资|投资价值凸显`）；角标语法校验（每段至少 1 个角标，角标 id 必须存在于证据索引）；
  2. **LLM 级**：核对每个角标句与对应证据是否一致（数字、主体、时间）、检查观点/事实混淆、检查是否有无角标的事实性断言。
- 系统 Prompt：

```
你是投研报告的审校与合规官。逐句核对下方章节草稿与证据原文，输出 JSON：
{"verdict": "pass | revise", "issues": [{"quote": "原句", "type": "citation_mismatch | no_citation | opinion_as_fact | compliance | number_error", "detail": "...", "suggestion": "改写建议"}], "revised_text": "verdict=revise 时给出全文改写稿，改写只修正问题句，保留其余原文与全部角标"}
判定标准：
- citation_mismatch：句子内容与所引证据不符（含数字不一致、张冠李戴）
- no_citation：事实性断言无角标（评价性过渡句除外）
- opinion_as_fact：券商观点/预测被写成客观事实
- compliance：出现投资建议、走势预测、估值判断（禁止清单：{disclaimer_rules}）
- 宁可错杀：拿不准是否越界的表述一律 revise
章节草稿：
{section_draft}
证据索引：
{evidence_index}
```

- 流程：pass → 落盘；revise 且轮次 < 2 → 携 issues 退回分析 Agent 重写；轮次耗尽 → 采纳 `revised_text` 落盘，审校记录（issues 全量）写 `uploads/tasks/{id}/review_log.jsonl`（演示脚本 3 的"审校拦截特写"数据源）。

## A6 · Chat Agent（报告对话）

- 文件：`services/chat_agent.py`；模型：qwen-max;模式：移植 MiroFish `ReportAgent.chat`——简化 ReAct，max_iterations=2，max_tool_calls=2；工具同 A4（去掉 fetch_financial_statements）。
- 系统 Prompt 要点（在 MiroFish chat prompt 基础上改写）：身份为"这份投研报告的作者"；上下文注入报告全文目录+用户问题相关章节；回答同样遵守 A4 写作铁律 1-5（含角标）；用户问题超出报告与图谱范围时明确说明并提示可开启新任务；检测到用户指出错误时，在回复末尾追加隐藏标记 `<!--correction-->`（前端不渲染，反馈 API 捕获后写入 feedback 表 type=correction）。

## A7 · Reflection Agent（反思，历史学习引擎）

- 文件：`services/reflection.py`；模型：qwen-max；模式：单轮 chat_json；触发：feedback 写入后异步 + 每日 02:00 批处理。无工具（输入由服务层拼装）。
- 输入拼装：目标 task_run 的任务卡、终态、成本、各阶段耗时；全部 feedback（章节 👍/👎/评语、星级、correction）；agent_log 的压缩摘要（每章节工具调用序列与次数）；tool_call_log 异常项。
- 系统 Prompt：

```
你是多 Agent 投研系统的流程优化分析师。根据一次任务的运行记录与用户反馈，归纳可复用的经验规则。
输出 JSON：{"rules": [{"rule_type": "style|routing|prompt_patch|source_health", "scope": "user|global", "target_agent": "planner|collector_news|collector_financial|collector_announcement|collector_research|collector_industry|analyst|reviewer", "condition": "适用条件", "action": "具体可执行的指令", "evidence": "来自哪条反馈/日志", "confidence": 0.0-1.0}], "user_preference_updates": [{"key": "...", "value": "..."}]}
规则要求：
1. action 必须具体到可直接注入目标 Agent 的 Prompt 并被执行，禁止"提高质量"这类空话。
2. 用户明确表达的偏好 scope=user；数据源故障、工具超时等与用户无关的归因 scope=global。
3. 一次最多产出 3 条规则；没有可归纳的就输出空数组，不要硬造。
4. 禁止产出与合规红线冲突的规则（如"给出买卖建议"）。
运行记录：
{run_digest}
```

- 输出落地：rules → `playbook_rule` 表（状态 candidate，状态机见 05 文档 §4.3）；user_preference_updates → `user_preference` 表；任务需求原文+反馈 → L2 图谱 episode。

## A8 · ScenarioAgent（情景设计）与推演角色 Agent 群

推演支线的三个 Agent 规格（ScenarioAgent 情景设计、PersonaGenerator 市场角色生成、ScenarioReportAgent 推演报告）连同 Prompt 与 IO 契约集中定义在 **10 文档 §3-§5**，此处不重复。要点：ScenarioAgent 输出必须锚定证据 id 的双情景配置；推演报告在 A4 写作铁律上增加"模拟限定语强制"与"禁止外推"两条（10§5）；`interview_agents` 工具从 MiroFish 原样移植，仅推演支线可用。

## A9 · 章节 Prompt 增补（多模态图表，对 A4 的修订）

A4 章节生成系统 Prompt 的"写作铁律"后追加一条：

```
8. 当本章涉及 3 期以上数值序列或多主体数值对比时，在相应位置输出 chart 数据块（格式见下），数据与 source_refs 必须来自工具返回的证据：
```chart
{"type": "line|bar|timeline", "title": "...", "x": [...], "series": [{"name": "...", "data": [...]}], "source_refs": ["E12"]}
```
```

Reviewer（A5）的核验规则相应增加：chart 数据块中每个数值可在 source_refs 指向的证据中找到，否则 `number_error`。

## A10 · 各 Agent 模型与成本汇总

| Agent | 模型 | 每任务典型调用次数 | temperature |
|-------|------|-------------------|-------------|
| Planner | qwen-plus | 1 | 0.2 |
| Collectors×5 | qwen-plus | 10（计划+质检各 5） | 0.2 |
| Analyst 大纲 | qwen-plus | 1 | 0.3 |
| Analyst 章节 | qwen-max | 16-32（4章 × 4-8轮） | 0.5 |
| Reviewer | qwen-max | 4-8 | 0.1 |
| Chat | qwen-max | 每问 1-3 | 0.4 |
| Reflection | qwen-max | 1 | 0.3 |
| Scenario 设计 | qwen-max | 1 | 0.4 |
| 仿真角色（OASIS） | qwen-plus | 约 600/次推演（30 角色×10 轮×2 情景） | OASIS 默认 |
| 推演报告 | qwen-max | 20-36 | 0.5 |
| 视觉解析（qwen-vl-plus） | qwen-vl-plus | 按需，每公告 ≤5 页批 | 0.2 |
