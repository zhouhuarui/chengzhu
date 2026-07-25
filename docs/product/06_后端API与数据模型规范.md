# 06 · 后端 API 与数据模型规范

> 接口形态刻意保持与 MiroFish 一致（task_id 轮询、agent-log 增量拉取 from_line 协议），使前端骨架可以最小改动复用。
> 统一响应包裹：`{"success": true, "data": {...}}` / `{"success": false, "error": "..."}`；所有接口 `/api` 前缀；Demo 阶段单用户，`user_id` 固定为 `"default"`（接口层已带出参数，便于后续多用户）。

---

## 1. REST API 定义

### 1.1 任务蓝图 `/api/task`（api/task.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/create` | multipart：`requirement`(str, 必填)、`files[]`(可选 pdf/md/txt)。同步执行 Planner，返回 `{task_id, task_card, clarifications}`，状态 `awaiting_confirm` |
| POST | `/{task_id}/confirm` | body=修改后的 task_card（可原样回传）。校验后启动后台管线，返回 `{task_id, status: "collecting"}` |
| GET | `/{task_id}/status` | **前端 2s 轮询主接口**。返回 `{status, progress: 0-100, progress_detail: {stage, message, collectors: {announcement: {state, cards}, ...}, sections: [{title, state}]}, error}` |
| GET | `/{task_id}/agent-log?from_line=N` | 增量拉取 agent_log.jsonl（协议与 MiroFish `/api/report/{id}/agent-log` 相同：返回 `{lines: [...], next_line: M, finished: bool}`） |
| GET | `/{task_id}/evidence?source_type=&page=` | 证据卡列表（分页 50），前端证据计数器与角标悬浮用 |
| GET | `/{task_id}` | 任务全量元数据（task.json 内容） |
| GET | `/list?limit=20` | 历史任务列表（对应 MiroFish `/api/simulation/history`） |
| DELETE | `/{task_id}` | 删除任务目录与图谱 group（调 graphiti 删 group_id） |
| GET | `/{task_id}/graph` | 图谱可视化数据 `{nodes: [{id, name, type, summary}], edges: [{source, target, name, fact, valid, created_at}]}`（GraphPanel 直接消费，格式对齐 MiroFish `/api/graph/data`） |

### 1.2 报告蓝图 `/api/report`（api/report.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{task_id}` | `{title, outline, sections: [{index, title, content_md, review: {verdict, issues_count}}], sources: [EvidenceCard...], integrity_notes, disclaimer}` |
| GET | `/{task_id}/markdown` | 纯 markdown 全文（导出用） |
| POST | `/chat` | `{task_id, message, chat_history: [{role, content}]}` → `{response, tool_calls: [{name, params}], correction_detected: bool}`（同步，超时 120s） |
| GET | `/{task_id}/review-log` | 审校记录（演示"合规拦截"特写用） |

### 1.3 反馈蓝图 `/api/feedback`（api/feedback.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/section` | `{task_id, section_index, vote: "up"|"down", comment?: str}` → 写 feedback 表 → 异步触发 Reflection |
| POST | `/report` | `{task_id, stars: 1-5, comment?: str}` |
| GET | `/{task_id}` | 该任务已有反馈（前端回显） |

### 1.4 记忆蓝图 `/api/memory`（api/memory.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/prefill` | 新建任务页预填：`{watch_symbols, default_time_window, deliverable_style, recent_focus_points}` |
| GET | `/preferences` | 全部结构化偏好 + L2 图谱事实摘要（"我的偏好"页） |
| DELETE | `/preferences/{key}` | 删除单条偏好（写 tombstone，7 天内不再学回） |
| DELETE | `/user` | 清空用户记忆（L2 图谱 group + 偏好表），09 合规要求 |
| GET | `/playbook` | 规则列表 `[{id, rule_type, scope, target_agent, condition, action, status, confidence, hit_count}]` |
| POST | `/playbook/{id}/confirm` | 用户手动确认 candidate→active |
| DELETE | `/playbook/{id}` | 规则退休（status=retired） |
| GET | `/playbook/stats` | 见 05 文档 §4.4 |
| GET | `/source-health` | 各工具 7 日健康度（04 文档 §5） |

### 1.5 推演蓝图 `/api/scenario`（api/scenario.py）

接口定义见 10 文档 §6（create / start / status / run-status / agent-log / interview / report），轮询协议与本文档 §4 完全一致；SQLite 增表 `scenario_run`（建表 SQL 同样放 utils/db.py，字段见 10§6）。

### 1.6 追踪蓝图 `/api/tracking`（api/tracking.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/subscribe` | `{task_id, cron: "daily"|"weekly", hour: 8}` → 创建订阅，返回 sub_id |
| GET | `/list` | 订阅列表 + 各自最新简报摘要 |
| POST | `/{sub_id}/pause` / `/resume` / DELETE `/{sub_id}` | 订阅管理 |
| GET | `/{sub_id}/briefs` | 历次简报 `[{date, title, new_facts_count, changed_facts_count, markdown}]` |
| POST | `/{sub_id}/run-now` | 立即重跑一次（演示用，同 confirm 一样走后台管线） |
| GET | `/notifications` | 未读简报通知（前端顶栏小红点，Demo 用轮询 30s） |

## 2. SQLite 数据模型（`uploads/chengzhu.db`，utils/db.py 用 sqlite3 + 建表 SQL，不引 ORM）

```sql
-- 任务运行记录（历史学习 L3 的原始数据）
CREATE TABLE task_run (
  run_id TEXT PRIMARY KEY,          -- = task_id，追踪重跑时为 {task_id}_r{n}
  task_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT 'default',
  task_card_json TEXT NOT NULL,
  status TEXT NOT NULL,             -- completed | completed_partial | failed
  started_at TEXT, finished_at TEXT,
  llm_calls INTEGER DEFAULT 0, llm_tokens INTEGER DEFAULT 0,
  web_search_calls INTEGER DEFAULT 0,
  stage_timings_json TEXT,          -- {"collecting": 95.2, "analyzing": 210.4, ...}
  collect_failures_json TEXT,       -- [{"agent": "news", "error": "..."}]
  reflected INTEGER DEFAULT 0       -- 反思 Agent 是否已处理
);

CREATE TABLE tool_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, tool_name TEXT NOT NULL, agent TEXT,
  ok INTEGER NOT NULL, degraded INTEGER DEFAULT 0,
  latency_ms INTEGER, cards_returned INTEGER,
  error TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_tcl_tool_time ON tool_call_log(tool_name, created_at);

CREATE TABLE feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, user_id TEXT DEFAULT 'default',
  kind TEXT NOT NULL,               -- section_vote | report_stars | correction
  section_index INTEGER, vote TEXT, stars INTEGER,
  comment TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE playbook_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_type TEXT NOT NULL,          -- style | routing | prompt_patch | source_health
  scope TEXT NOT NULL,              -- user | global
  user_id TEXT DEFAULT 'default',
  target_agent TEXT NOT NULL,
  condition TEXT, action TEXT NOT NULL,
  status TEXT DEFAULT 'candidate',  -- candidate | active | retired
  confidence REAL DEFAULT 0.5,
  evidence_run_ids TEXT,            -- JSON array
  hit_count INTEGER DEFAULT 0,
  hit_stars_sum REAL DEFAULT 0,     -- 命中任务的星级累计（效果度量）
  created_at TEXT, activated_at TEXT, retired_at TEXT
);

CREATE TABLE user_preference (
  user_id TEXT DEFAULT 'default', key TEXT, value_json TEXT,
  updated_at TEXT, tombstone_until TEXT,   -- 删除后 7 天内禁止重学
  PRIMARY KEY (user_id, key)
);

CREATE TABLE evidence_card (         -- 入图去重索引
  dedup_key TEXT PRIMARY KEY,        -- md5(url + title)
  task_id TEXT, card_json TEXT, ingested_at TEXT
);

CREATE TABLE tracking_sub (
  sub_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  cron TEXT NOT NULL, hour INTEGER DEFAULT 8,
  status TEXT DEFAULT 'active',      -- active | paused
  watermark TEXT,                    -- 上次采集水位线 ISO8601
  last_run_at TEXT, created_at TEXT
);

CREATE TABLE brief (
  brief_id TEXT PRIMARY KEY, sub_id TEXT NOT NULL,
  run_id TEXT, date TEXT, markdown_path TEXT,
  new_facts INTEGER, changed_facts INTEGER,
  read INTEGER DEFAULT 0             -- 通知已读
);
```

## 3. 文件持久化布局（沿用 MiroFish uploads 模式）

```
backend/uploads/
  tasks/{task_id}/
    task.json                 # ResearchTask 状态机主档（仿 project.json）
    files/                    # 用户上传原始文件
    extracted_text.txt
    evidence/{agent}.jsonl    # 各采集 Agent 的 EvidenceCard
    agent_log.jsonl           # 全 Agent 过程日志（前端增量拉取）
    outline.json
    sections/section_{XX}.md
    review_log.jsonl
    full_report.md
    progress.json             # TaskManager 快照（进程重启后前端可读到最后进度）
  subscriptions/{sub_id}/briefs/{date}.md
  cache/announcements/{md5}.txt
  chengzhu.db
```

## 4. 关键协议细节（联调必读）

1. **状态轮询节奏**：前端 status 2s、agent-log 2s、graph 15s（任务运行页）；completed/failed 后停止全部轮询。
2. **agent_log 行格式**（沿用 MiroFish 字段 + 扩展 agent 字段）：`{"timestamp", "elapsed_seconds", "task_id", "agent": "collector_news", "action": "tool_call", "stage": "collecting", "section_title": null, "details": {...}}`。前端按 agent 字段分泳道渲染。
3. **角标映射**：EvidenceCard 全局自增 id（任务内），报告 md 中 `[E23]` 由前端正则替换为悬浮组件，数据取自 `/evidence` 接口的 id 索引；`/{task_id}` 报告接口的 sources 数组即按 id 排序的全量卡片。
4. **进程重启恢复**：GET status 时 TaskManager 无此 task → 读 task.json + progress.json 返回最后已知状态；处于中间态（collecting/analyzing 等）的任务标记为 failed（Demo 不做断点续跑，08 文档已列入迭代项）。
5. **超时约定**：Planner 60s、单采集 Agent 180s、单章节生成 300s、审校 120s、chat 120s；全任务硬上限 20 分钟（超时置 failed，写明超时阶段）。
