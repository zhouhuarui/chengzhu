# 06 · 后端 API 与数据模型规范

> 接口形态保持 MiroFish 的 task_id 轮询与 agent-log 增量协议，同时以不可变 `run_id` 标识每次执行。省略 run_id 时读 latest；显式 run 必须属于当前 task。
> 统一响应包裹：`{"success": true, "data": {...}}` / `{"success": false, "error": "..."}`；所有接口 `/api` 前缀；Demo 阶段单用户，`user_id` 固定为 `"default"`（接口层已带出参数，便于后续多用户）。

---

## 1. REST API 定义

### 1.1 任务蓝图 `/api/task`（api/task.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/create` | multipart：`requirement`(str, 必填)、`files[]`(可选 pdf/md/txt)。同步执行 Planner，返回 `{task_id, task_card, clarifications}`，状态 `awaiting_confirm` |
| POST | `/{task_id}/confirm` | body=修改后的 task_card（含 `analysis_mode`）。校验后创建不可变 run 并启动管线，返回 `{task_id, run_id, status: "collecting"}` |
| GET | `/{task_id}/status` | **前端 2s 轮询主接口**。返回 status/progress/run_id；辩论时 `progress_detail.debate` 含回合、角色及 Claim/Challenge/撤回/硬失败计数 |
| GET | `/{task_id}/agent-log?from_line=N` | 增量拉取 agent_log.jsonl（协议与 MiroFish `/api/report/{id}/agent-log` 相同：返回 `{lines: [...], next_line: M, finished: bool}`） |
| GET | `/{task_id}/evidence?run_id=&source_type=` | 指定 run 的证据卡与 E 显示映射，前端证据计数器与角标悬浮用 |
| GET | `/{task_id}/runs` | 历史运行列表，含 TaskCard、analysis_mode、状态、成本/时长及 is_current |
| GET | `/{task_id}/debate?run_id=` | Claim、Challenge、Audit、Verdict 与安全进度元数据；不返回 chain-of-thought |
| GET | `/{task_id}` | 任务全量元数据（task.json 内容） |
| GET | `/list?limit=20` | 历史任务列表（对应 MiroFish `/api/simulation/history`） |
| DELETE | `/{task_id}` | 删除任务目录与图谱 group（调 graphiti 删 group_id） |
| GET | `/{task_id}/graph?run_id=` | 指定 run 的图谱可视化数据；省略 run_id 读 latest |

### 1.2 报告蓝图 `/api/report`（api/report.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{task_id}?run_id=` | 指定 run 的报告、sources、analysis_mode 与 debate_status；省略时 latest |
| GET | `/{task_id}/markdown?run_id=` | 指定 run 的纯 Markdown（chart 自动降级表格） |
| POST | `/chat` | `{task_id, run_id?, message, chat_history}` → 安全回复；只读取该 run 的报告与证据 |
| GET | `/{task_id}/review-log?run_id=` | 指定 run 的审校记录 |

### 1.3 反馈蓝图 `/api/feedback`（api/feedback.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/section` | `{task_id, run_id?, section_index, vote, comment?}` → 反馈绑定解析后的 run 并触发 Reflection |
| POST | `/report` | `{task_id, run_id?, stars: 1-5, comment?}` |
| GET | `/{task_id}?run_id=` | 指定 run 的反馈（前端回显） |

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
  run_id TEXT PRIMARY KEY,          -- 每次 confirm/rerun 生成，不复用 task_id
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

CREATE TABLE debate_run (
  run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
  current_round INTEGER DEFAULT 0, current_role TEXT,
  claim_count INTEGER DEFAULT 0, challenge_count INTEGER DEFAULT 0,
  withdrawn_count INTEGER DEFAULT 0, audit_failure_count INTEGER DEFAULT 0,
  verdict_json TEXT, error TEXT, started_at TEXT, finished_at TEXT
);

CREATE TABLE llm_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL, agent TEXT, finish_reason TEXT,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0, cost_cny REAL DEFAULT 0,
  request_id TEXT, latency_ms INTEGER, retry_count INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 1, error TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
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
    runs/{run_id}/
      run.json                # 冻结 TaskCard 与创建时间
      evidence/{agent}.jsonl
      evidence_index.json     # evidence_uid ↔ E1…En
      normalized_facts.jsonl
      debate/claims.jsonl
      debate/challenges.jsonl
      debate/audit.jsonl
      debate/verdict.json
      sections/
      review_log.jsonl
      report.json
      report.md
    report.json               # latest 兼容副本；旧任务仍从根目录读取
    report.md
  subscriptions/{sub_id}/briefs/{date}.md
  cache/announcements/{md5}.txt
  chengzhu.db
```

## 4. 关键协议细节（联调必读）

1. **状态轮询节奏**：前端 status/agent-log/debate 2s、graph 15s；completed/completed_partial/failed 后停止。所有业务读取保留当前 run_id。
2. **agent_log 行格式**：沿用 MiroFish 字段并扩展 agent/run_id；details 只含工具摘要或最终业务产物，不含 Prompt、图片 Base64 或模型原始思维链。
3. **角标映射**：Claim 永久引用 evidence_uid；报告中的 `[E23]` 由本 run 的 evidence_index 映射，跨 run 不保证 E 编号相同。
4. **进程重启恢复**：GET status 时 TaskManager 无此 task → 读 task.json + progress.json 返回最后已知状态；处于中间态（collecting/analyzing 等）的任务标记为 failed（Demo 不做断点续跑，08 文档已列入迭代项）。
5. **超时约定**：LLM connect/read 为 10/180s、传输最多重试一次；摘要/对比全任务硬上限 8 分钟。辩论失败时同快照降级 direct 并披露；没有报告则 failed，不能 completed_partial。
