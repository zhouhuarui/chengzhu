"""SQLite 连接与建表（无 ORM）。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..config import Config

_local = threading.local()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_run (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT 'default',
  task_card_json TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  llm_calls INTEGER DEFAULT 0, llm_tokens INTEGER DEFAULT 0,
  web_search_calls INTEGER DEFAULT 0,
  stage_timings_json TEXT,
  collect_failures_json TEXT,
  reflected INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, tool_name TEXT NOT NULL, agent TEXT,
  ok INTEGER NOT NULL, degraded INTEGER DEFAULT 0,
  latency_ms INTEGER, cards_returned INTEGER,
  error TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tcl_tool_time ON tool_call_log(tool_name, created_at);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, user_id TEXT DEFAULT 'default',
  kind TEXT NOT NULL,
  section_index INTEGER, vote TEXT, stars INTEGER,
  comment TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS playbook_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_type TEXT NOT NULL,
  scope TEXT NOT NULL,
  user_id TEXT DEFAULT 'default',
  target_agent TEXT NOT NULL,
  condition TEXT, action TEXT NOT NULL,
  status TEXT DEFAULT 'candidate',
  confidence REAL DEFAULT 0.5,
  evidence_run_ids TEXT,
  hit_count INTEGER DEFAULT 0,
  hit_stars_sum REAL DEFAULT 0,
  created_at TEXT, activated_at TEXT, retired_at TEXT
);

CREATE TABLE IF NOT EXISTS user_preference (
  user_id TEXT DEFAULT 'default', key TEXT, value_json TEXT,
  updated_at TEXT, tombstone_until TEXT,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS evidence_card (
  dedup_key TEXT PRIMARY KEY,
  task_id TEXT, card_json TEXT, ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS tracking_sub (
  sub_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  cron TEXT NOT NULL, hour INTEGER DEFAULT 8,
  status TEXT DEFAULT 'active',
  watermark TEXT,
  last_run_at TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS brief (
  brief_id TEXT PRIMARY KEY, sub_id TEXT NOT NULL,
  run_id TEXT, date TEXT, markdown_path TEXT,
  new_facts INTEGER, changed_facts INTEGER,
  read INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scenario_run (
  scenario_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  config_json TEXT,
  status TEXT NOT NULL,
  cost REAL DEFAULT 0,
  started_at TEXT, finished_at TEXT
);
"""


def _db_path() -> str:
    return Config.DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = getattr(_local, 'conn', None)
    if conn is None:
        import os
        os.makedirs(os.path.dirname(_db_path()), exist_ok=True)
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    with db_cursor() as cur:
        cur.executescript(SCHEMA_SQL)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


# ---------- task_run ----------

def insert_task_run(
    run_id: str,
    task_id: str,
    task_card: Dict[str, Any],
    status: str,
    user_id: str = 'default',
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO task_run
               (run_id, task_id, user_id, task_card_json, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, task_id, user_id, json.dumps(task_card, ensure_ascii=False), status, now_iso()),
        )


def get_task_run(run_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM task_run WHERE run_id = ?', (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ---------- feedback ----------

def insert_feedback(
    run_id: str,
    kind: str,
    *,
    section_index: Optional[int] = None,
    vote: Optional[str] = None,
    stars: Optional[int] = None,
    comment: Optional[str] = None,
    user_id: str = 'default',
) -> int:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO feedback
               (run_id, user_id, kind, section_index, vote, stars, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, user_id, kind, section_index, vote, stars, comment),
        )
        return int(cur.lastrowid)


def list_feedback(run_id: str) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM feedback WHERE run_id = ? ORDER BY id', (run_id,))
        return [dict(r) for r in cur.fetchall()]


# ---------- playbook_rule ----------

def insert_playbook_rule(
    rule_type: str,
    scope: str,
    target_agent: str,
    action: str,
    *,
    condition: str = '',
    confidence: float = 0.5,
    evidence_run_ids: Optional[Iterable[str]] = None,
    user_id: str = 'default',
    status: str = 'candidate',
) -> int:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO playbook_rule
               (rule_type, scope, user_id, target_agent, condition, action,
                status, confidence, evidence_run_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_type, scope, user_id, target_agent, condition, action,
                status, confidence,
                json.dumps(list(evidence_run_ids or []), ensure_ascii=False),
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def get_playbook_rule(rule_id: int) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM playbook_rule WHERE id = ?', (rule_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_playbook_rules(
    *,
    status: Optional[str] = None,
    target_agent: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    sql = 'SELECT * FROM playbook_rule WHERE 1=1'
    params: List[Any] = []
    if status:
        sql += ' AND status = ?'
        params.append(status)
    if target_agent:
        sql += ' AND target_agent = ?'
        params.append(target_agent)
    if user_id:
        sql += ' AND (scope = "global" OR user_id = ?)'
        params.append(user_id)
    sql += ' ORDER BY confidence DESC, id DESC'
    with db_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------- tool_call_log ----------

def insert_tool_call_log(
    tool_name: str,
    ok: bool,
    *,
    run_id: Optional[str] = None,
    agent: Optional[str] = None,
    degraded: bool = False,
    latency_ms: Optional[int] = None,
    cards_returned: Optional[int] = None,
    error: Optional[str] = None,
) -> int:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO tool_call_log
               (run_id, tool_name, agent, ok, degraded, latency_ms, cards_returned, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, tool_name, agent, 1 if ok else 0, 1 if degraded else 0,
                latency_ms, cards_returned, error,
            ),
        )
        return int(cur.lastrowid)
