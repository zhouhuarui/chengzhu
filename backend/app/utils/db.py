"""SQLite 连接与建表（无 ORM）。"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

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
CREATE INDEX IF NOT EXISTS idx_task_run_task_started
  ON task_run(task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS debate_run (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  current_round INTEGER DEFAULT 0,
  current_role TEXT,
  claim_count INTEGER DEFAULT 0,
  challenge_count INTEGER DEFAULT 0,
  withdrawn_count INTEGER DEFAULT 0,
  audit_failure_count INTEGER DEFAULT 0,
  verdict_json TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_debate_run_task_started
  ON debate_run(task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS llm_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  agent TEXT,
  finish_reason TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  cost_cny REAL DEFAULT 0,
  request_id TEXT,
  latency_ms INTEGER,
  retry_count INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 1,
  error TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_llm_call_run_time
  ON llm_call_log(run_id, created_at);

CREATE TABLE IF NOT EXISTS llm_budget_reservation (
  reservation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  amount_cny REAL NOT NULL CHECK (amount_cny >= 0),
  budget_cny REAL NOT NULL CHECK (budget_cny >= 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'released')),
  created_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_budget_reservation_run_status
  ON llm_budget_reservation(run_id, status);

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
    process_id = os.getpid()
    # A sqlite connection inherited across ``fork`` is unsafe.  Keep the
    # existing thread-local behaviour, but recreate the handle in a child
    # process so SQLite's file locks remain authoritative across processes.
    if conn is not None and getattr(_local, 'pid', None) != process_id:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
        _local.conn = None
    if conn is None:
        os.makedirs(os.path.dirname(_db_path()), exist_ok=True)
        conn = sqlite3.connect(
            _db_path(),
            check_same_thread=False,
            timeout=30.0,
        )
        conn.execute('PRAGMA busy_timeout = 30000')
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        _local.pid = process_id
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
    started_at: Optional[str] = None,
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO task_run
               (run_id, task_id, user_id, task_card_json, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET status = excluded.status""",
            (
                run_id, task_id, user_id,
                json.dumps(task_card, ensure_ascii=False), status,
                started_at or now_iso(),
            ),
        )


def get_task_run(run_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM task_run WHERE run_id = ?', (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_task_runs(task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """SELECT * FROM task_run WHERE task_id = ?
               ORDER BY started_at DESC, run_id DESC LIMIT ?""",
            (task_id, max(1, min(int(limit), 500))),
        )
        return [dict(row) for row in cur.fetchall()]


def latest_task_run(task_id: str) -> Optional[Dict[str, Any]]:
    rows = list_task_runs(task_id, limit=1)
    return rows[0] if rows else None


def has_task_run_with_status(task_id: str, statuses: Iterable[str]) -> bool:
    values = sorted({str(status) for status in statuses if str(status)})
    if not values:
        return False
    placeholders = ','.join('?' for _ in values)
    with db_cursor() as cur:
        cur.execute(
            f'SELECT 1 FROM task_run WHERE task_id = ? '
            f'AND status IN ({placeholders}) LIMIT 1',
            (task_id, *values),
        )
        return cur.fetchone() is not None


def update_task_run(run_id: str, status: Optional[str] = None, **fields: Any) -> None:
    allowed = {
        'finished_at', 'llm_calls', 'llm_tokens', 'web_search_calls',
        'stage_timings_json', 'collect_failures_json', 'reflected',
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if status is not None:
        updates['status'] = status
    if not updates:
        return
    cols = ', '.join(f'{key} = ?' for key in updates)
    with db_cursor() as cur:
        cur.execute(f'UPDATE task_run SET {cols} WHERE run_id = ?', (*updates.values(), run_id))


def _task_cleanup_manifest(cur: sqlite3.Cursor, task_id: str) -> Dict[str, List[str]]:
    cur.execute(
        'SELECT scenario_id FROM scenario_run WHERE task_id = ? ORDER BY scenario_id',
        (task_id,),
    )
    scenario_ids = [str(row['scenario_id']) for row in cur.fetchall()]
    cur.execute(
        'SELECT sub_id FROM tracking_sub WHERE task_id = ? ORDER BY sub_id',
        (task_id,),
    )
    tracking_sub_ids = [str(row['sub_id']) for row in cur.fetchall()]
    return {
        'scenario_ids': scenario_ids,
        'tracking_sub_ids': tracking_sub_ids,
    }


def get_task_cleanup_manifest(task_id: str) -> Dict[str, List[str]]:
    """Return only filesystem identifiers owned directly by ``task_id``."""

    with db_cursor() as cur:
        return _task_cleanup_manifest(cur, task_id)


def delete_task_runs(
    task_id: str,
    *,
    expected_manifest: Optional[Dict[str, List[str]]] = None,
    stage_filesystem: Optional[Callable[[], None]] = None,
) -> Dict[str, List[str]]:
    """删除任务的运行级及直接关联元数据；产物目录由 API 层删除。

    When the API already removed owned artifact directories, the manifest
    comparison prevents a concurrently-created scenario/subscription from
    being deleted without its filesystem artifact also being considered.
    """
    # Reserve the SQLite writer before checking ownership.  The optional
    # filesystem callback then atomically renames live directories while this
    # transaction prevents a concurrent scenario/subscription insert from
    # invalidating the manifest after the files have disappeared.
    with _immediate_cursor() as cur:
        manifest = _task_cleanup_manifest(cur, task_id)
        if expected_manifest is not None:
            expected = {
                'scenario_ids': sorted(str(value) for value in expected_manifest.get('scenario_ids', [])),
                'tracking_sub_ids': sorted(
                    str(value) for value in expected_manifest.get('tracking_sub_ids', [])
                ),
            }
            if manifest != expected:
                raise RuntimeError('task_related_state_changed_during_delete')
        if stage_filesystem is not None:
            stage_filesystem()
        # 早期日志直接以 task_id 作为 run_id，因此与真实 run 一并清理。
        cur.execute(
            """DELETE FROM llm_budget_reservation
                WHERE run_id = ? OR run_id IN (
                  SELECT run_id FROM task_run WHERE task_id = ?
                )""",
            (task_id, task_id),
        )
        for table in ('feedback', 'tool_call_log', 'llm_call_log'):
            cur.execute(
                f"""DELETE FROM {table}
                    WHERE run_id = ? OR run_id IN (
                      SELECT run_id FROM task_run WHERE task_id = ?
                    )""",
                (task_id, task_id),
            )
        cur.execute(
            'DELETE FROM brief WHERE sub_id IN '
            '(SELECT sub_id FROM tracking_sub WHERE task_id = ?)',
            (task_id,),
        )
        cur.execute('DELETE FROM tracking_sub WHERE task_id = ?', (task_id,))
        cur.execute('DELETE FROM scenario_run WHERE task_id = ?', (task_id,))
        cur.execute('DELETE FROM evidence_card WHERE task_id = ?', (task_id,))
        cur.execute('DELETE FROM debate_run WHERE task_id = ?', (task_id,))
        cur.execute('DELETE FROM task_run WHERE task_id = ?', (task_id,))
        return manifest


# ---------- debate_run ----------

def insert_debate_run(run_id: str, task_id: str, status: str = 'pending') -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO debate_run (run_id, task_id, status, started_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET status = excluded.status""",
            (run_id, task_id, status, now_iso()),
        )


def update_debate_run(run_id: str, **fields: Any) -> None:
    allowed = {
        'status', 'current_round', 'current_role', 'claim_count',
        'challenge_count', 'withdrawn_count', 'audit_failure_count',
        'verdict_json', 'error', 'finished_at',
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if 'verdict_json' in updates and not isinstance(updates['verdict_json'], str):
        updates['verdict_json'] = json.dumps(updates['verdict_json'], ensure_ascii=False)
    if not updates:
        return
    cols = ', '.join(f'{key} = ?' for key in updates)
    with db_cursor() as cur:
        cur.execute(f'UPDATE debate_run SET {cols} WHERE run_id = ?', (*updates.values(), run_id))


def finish_debate_run(
    run_id: str,
    status: str,
    *,
    verdict: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    fields: Dict[str, Any] = {'status': status, 'finished_at': now_iso()}
    if verdict is not None:
        fields['verdict_json'] = verdict
    if error is not None:
        fields['error'] = error
    update_debate_run(run_id, **fields)


def get_debate_run(run_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM debate_run WHERE run_id = ?', (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_debate_runs(task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """SELECT * FROM debate_run WHERE task_id = ?
               ORDER BY started_at DESC, run_id DESC LIMIT ?""",
            (task_id, max(1, min(int(limit), 500))),
        )
        return [dict(row) for row in cur.fetchall()]


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


# ---------- llm_call_log ----------

def insert_llm_call_log(
    run_id: Optional[str],
    provider: str,
    model: str,
    *,
    agent: Optional[str] = None,
    finish_reason: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_cny: float = 0.0,
    request_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    retry_count: int = 0,
    ok: bool = True,
    error: Optional[str] = None,
    budget_reservation_id: Optional[str] = None,
    settle_at_reserved_cost: bool = False,
) -> int:
    """仅保存调用元数据，并原子结算对应预算预留。

    接口故意不接受 prompt/思维链字段。若传入预留 ID，则日志写入与
    ``active -> released`` 必须在同一 ``BEGIN IMMEDIATE`` 事务成功；
    重复、跨 run 或不存在的预留都会回滚，避免重复计费或错误释放。
    """
    if settle_at_reserved_cost and not budget_reservation_id:
        raise ValueError('reserved-cost settlement requires a reservation')
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total_tokens = max(0, int(total_tokens or (prompt_tokens + completion_tokens)))
    settled_cost = _validated_cny(float(cost_cny or 0), 'cost_cny')
    cursor_factory = _immediate_cursor if budget_reservation_id else db_cursor
    with cursor_factory() as cur:
        if budget_reservation_id:
            cur.execute(
                """SELECT run_id, status, amount_cny FROM llm_budget_reservation
                   WHERE reservation_id = ?""",
                (budget_reservation_id,),
            )
            reservation = cur.fetchone()
            if reservation is None:
                raise ValueError('unknown llm budget reservation')
            if reservation['status'] != 'active':
                raise ValueError('llm budget reservation already settled')
            if run_id is None or reservation['run_id'] != run_id:
                raise ValueError('llm budget reservation run mismatch')
            # A provider can omit usage entirely or return only one token
            # field.  In that case the admitted maximum remains the only safe
            # billable bound, so persist it rather than releasing the
            # reservation into a zero/under-counted log row.
            if settle_at_reserved_cost:
                settled_cost = _validated_cny(
                    float(reservation['amount_cny']),
                    'reservation amount_cny',
                )
        cur.execute(
            """INSERT INTO llm_call_log
               (run_id, provider, model, agent, finish_reason, prompt_tokens,
                completion_tokens, total_tokens, cost_cny, request_id,
                latency_ms, retry_count, ok, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, provider, model, agent, finish_reason, prompt_tokens,
                completion_tokens, total_tokens, settled_cost, request_id,
                latency_ms, max(0, int(retry_count or 0)), 1 if ok else 0, error,
            ),
        )
        log_id = int(cur.lastrowid)
        if budget_reservation_id:
            cur.execute(
                """UPDATE llm_budget_reservation
                   SET status = 'released', released_at = ?
                   WHERE reservation_id = ? AND status = 'active'""",
                (now_iso(), budget_reservation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError('llm budget reservation settlement raced')
        return log_id


def list_llm_call_logs(run_id: str) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM llm_call_log WHERE run_id = ? ORDER BY id',
            (run_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def assign_pending_llm_logs(task_id: str, run_id: str) -> Dict[str, int]:
    """Atomically attach pre-confirmation Planner spend to the first run.

    Planner calls happen before a concrete ``run_id`` exists, so both their
    metadata and any crash-left active reservations initially belong to the
    task id.  Moving only the log would make the formal run under-report its
    worst-case spend.  Released historical reservations remain immutable.
    """

    with _immediate_cursor() as cur:
        cur.execute(
            'SELECT task_id FROM task_run WHERE run_id = ?',
            (run_id,),
        )
        target = cur.fetchone()
        if target is None or str(target['task_id']) != str(task_id):
            raise ValueError('pending llm metadata target run mismatch')

        # Only the earliest formal run owns pre-confirmation Planner spend.
        # A later A/B confirm must not reassign newly-orphaned task-scoped rows.
        cur.execute(
            """SELECT run_id FROM task_run
               WHERE task_id = ? AND run_id != ?
               ORDER BY started_at, run_id LIMIT 1""",
            (task_id, task_id),
        )
        first = cur.fetchone()
        if first is None or str(first['run_id']) != str(run_id):
            return {'logs': 0, 'active_reservations': 0}

        cur.execute(
            """SELECT COALESCE(SUM(CASE WHEN cost_cny > 0 THEN cost_cny ELSE 0 END), 0),
                      COUNT(*)
               FROM llm_call_log
               WHERE run_id = ? AND agent = 'planner'""",
            (task_id,),
        )
        pending_cost, pending_log_count = cur.fetchone()
        cur.execute(
            """SELECT COALESCE(SUM(CASE WHEN cost_cny > 0 THEN cost_cny ELSE 0 END), 0)
               FROM llm_call_log WHERE run_id = ?""",
            (run_id,),
        )
        target_cost = float(cur.fetchone()[0] or 0.0)
        cur.execute(
            """SELECT amount_cny, budget_cny FROM llm_budget_reservation
               WHERE run_id = ? AND status = 'active'""",
            (task_id,),
        )
        pending_reservations = cur.fetchall()
        cur.execute(
            """SELECT amount_cny, budget_cny FROM llm_budget_reservation
               WHERE run_id = ? AND status = 'active'""",
            (run_id,),
        )
        target_reservations = cur.fetchall()

        active_rows = [*pending_reservations, *target_reservations]
        if active_rows:
            total_after_move = (
                target_cost
                + float(pending_cost or 0.0)
                + sum(float(row['amount_cny'] or 0.0) for row in active_rows)
            )
            conservative_cap = min(float(row['budget_cny']) for row in active_rows)
            if total_after_move > conservative_cap + 1e-9:
                raise RuntimeError('pending llm budget migration exceeds run cap')

        cur.execute(
            "UPDATE llm_call_log SET run_id = ? WHERE run_id = ? AND agent = 'planner'",
            (run_id, task_id),
        )
        moved_logs = int(cur.rowcount)
        cur.execute(
            """UPDATE llm_budget_reservation SET run_id = ?
               WHERE run_id = ? AND status = 'active'""",
            (run_id, task_id),
        )
        moved_reservations = int(cur.rowcount)
        if moved_logs != int(pending_log_count or 0):
            raise RuntimeError('pending llm log migration raced')
        if moved_reservations != len(pending_reservations):
            raise RuntimeError('pending llm reservation migration raced')
        return {
            'logs': moved_logs,
            'active_reservations': moved_reservations,
        }


# ---------- llm_budget_reservation ----------

def _validated_cny(value: float, field: str) -> float:
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f'{field} must be a finite non-negative number')
    return amount


@contextmanager
def _immediate_cursor():
    """Serialize a read-check-write sequence across SQLite connections.

    ``BEGIN IMMEDIATE`` acquires the database write reservation before either
    cost sum is read.  A second thread or process therefore cannot observe the
    same remaining budget and insert another reservation concurrently.
    """

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute('BEGIN IMMEDIATE')
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def reserve_llm_budget(
    run_id: str,
    reservation_id: str,
    amount_cny: float,
    budget_cny: float,
) -> bool:
    """Atomically reserve LLM budget for one prospective provider call.

    The admission check includes settled ``llm_call_log.cost_cny`` plus every
    active reservation for the run.  Repeating an identical active
    reservation is idempotent; reusing its id with different attributes is
    rejected rather than mutating an already-admitted reservation.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('run_id must be a non-empty string')
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise ValueError('reservation_id must be a non-empty string')
    amount = _validated_cny(amount_cny, 'amount_cny')
    budget = _validated_cny(budget_cny, 'budget_cny')

    with _immediate_cursor() as cur:
        cur.execute(
            'SELECT * FROM llm_budget_reservation WHERE reservation_id = ?',
            (reservation_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            return bool(
                existing['status'] == 'active'
                and existing['run_id'] == run_id
                and float(existing['amount_cny']) == amount
                and float(existing['budget_cny']) == budget
            )

        cur.execute(
            """SELECT COALESCE(SUM(
                 CASE WHEN cost_cny > 0 THEN cost_cny ELSE 0 END
               ), 0)
               FROM llm_call_log WHERE run_id = ?""",
            (run_id,),
        )
        settled = float(cur.fetchone()[0] or 0.0)
        cur.execute(
            """SELECT COALESCE(SUM(amount_cny), 0)
               FROM llm_budget_reservation
               WHERE run_id = ? AND status = 'active'""",
            (run_id,),
        )
        reserved = float(cur.fetchone()[0] or 0.0)
        # A tiny epsilon prevents binary floating point noise from rejecting an
        # exact decimal boundary (for example 0.1 + 0.2 against 0.3).
        if settled + reserved + amount > budget + 1e-9:
            return False

        cur.execute(
            """INSERT INTO llm_budget_reservation
               (reservation_id, run_id, amount_cny, budget_cny, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (reservation_id, run_id, amount, budget, now_iso()),
        )
        return True


def get_llm_budget_reservation(reservation_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM llm_budget_reservation WHERE reservation_id = ?',
            (reservation_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_llm_budget_reservations(
    run_id: str,
    *,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        if active_only:
            cur.execute(
                """SELECT * FROM llm_budget_reservation
                   WHERE run_id = ? AND status = 'active'
                   ORDER BY created_at, reservation_id""",
                (run_id,),
            )
        else:
            cur.execute(
                """SELECT * FROM llm_budget_reservation
                   WHERE run_id = ?
                   ORDER BY created_at, reservation_id""",
                (run_id,),
            )
        return [dict(row) for row in cur.fetchall()]


def llm_budget_totals(run_id: str) -> Dict[str, float]:
    """Read settled cost and active reservations from one SQLite snapshot."""

    with db_cursor() as cur:
        cur.execute(
            """SELECT
                 COALESCE((
                   SELECT SUM(CASE WHEN cost_cny > 0 THEN cost_cny ELSE 0 END)
                   FROM llm_call_log WHERE run_id = ?
                 ), 0) AS settled_cny,
                 COALESCE((
                   SELECT SUM(amount_cny) FROM llm_budget_reservation
                   WHERE run_id = ? AND status = 'active'
                 ), 0) AS reserved_cny""",
            (run_id, run_id),
        )
        row = cur.fetchone()
        settled = float(row['settled_cny'] or 0.0)
        reserved = float(row['reserved_cny'] or 0.0)
        return {
            'settled_cny': settled,
            'reserved_cny': reserved,
            'committed_cny': settled + reserved,
        }


def release_llm_budget_reservation(reservation_id: str) -> bool:
    """Release an active reservation while retaining its audit record."""

    with db_cursor() as cur:
        cur.execute(
            """UPDATE llm_budget_reservation
               SET status = 'released', released_at = ?
               WHERE reservation_id = ? AND status = 'active'""",
            (now_iso(), reservation_id),
        )
        return cur.rowcount > 0


def delete_llm_budget_reservation(reservation_id: str) -> bool:
    """Permanently delete one reservation (used for explicit cleanup only)."""

    with db_cursor() as cur:
        cur.execute(
            'DELETE FROM llm_budget_reservation WHERE reservation_id = ?',
            (reservation_id,),
        )
        return cur.rowcount > 0


# ---------- user_preference ----------

def upsert_user_preference(key: str, value: Any, user_id: str = 'default') -> None:
    # 尊重 tombstone
    with db_cursor() as cur:
        cur.execute(
            'SELECT tombstone_until FROM user_preference WHERE user_id = ? AND key = ?',
            (user_id, key),
        )
        row = cur.fetchone()
        if row and row['tombstone_until']:
            try:
                if datetime.fromisoformat(row['tombstone_until']) > datetime.now():
                    return
            except Exception:
                pass
        cur.execute(
            """INSERT INTO user_preference (user_id, key, value_json, updated_at, tombstone_until)
               VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(user_id, key) DO UPDATE SET
                 value_json = excluded.value_json,
                 updated_at = excluded.updated_at,
                 tombstone_until = NULL""",
            (user_id, key, json.dumps(value, ensure_ascii=False), now_iso()),
        )


def list_user_preferences(user_id: str = 'default') -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM user_preference WHERE user_id = ? ORDER BY key',
            (user_id,),
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d['value'] = json.loads(d.get('value_json') or 'null')
            except Exception:
                d['value'] = d.get('value_json')
            rows.append(d)
        return rows


def get_user_preference(key: str, user_id: str = 'default') -> Optional[Any]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT value_json, tombstone_until FROM user_preference WHERE user_id = ? AND key = ?',
            (user_id, key),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row['tombstone_until']:
            try:
                if datetime.fromisoformat(row['tombstone_until']) > datetime.now():
                    return None
            except Exception:
                pass
        try:
            return json.loads(row['value_json'] or 'null')
        except Exception:
            return row['value_json']


def tombstone_user_preference(key: str, user_id: str = 'default', days: int = 7) -> None:
    until = datetime.now().timestamp() + days * 86400
    until_iso = datetime.fromtimestamp(until).isoformat(timespec='seconds')
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO user_preference (user_id, key, value_json, updated_at, tombstone_until)
               VALUES (?, ?, 'null', ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET
                 tombstone_until = excluded.tombstone_until,
                 updated_at = excluded.updated_at""",
            (user_id, key, now_iso(), until_iso),
        )


def clear_user_preferences(user_id: str = 'default') -> None:
    with db_cursor() as cur:
        cur.execute('DELETE FROM user_preference WHERE user_id = ?', (user_id,))


# ---------- evidence_card dedup ----------

def get_evidence_dedup(dedup_key: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM evidence_card WHERE dedup_key = ?', (dedup_key,))
        row = cur.fetchone()
        return dict(row) if row else None


def insert_evidence_dedup(dedup_key: str, task_id: str, card: Dict[str, Any]) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT OR IGNORE INTO evidence_card (dedup_key, task_id, card_json, ingested_at)
               VALUES (?, ?, ?, ?)""",
            (dedup_key, task_id, json.dumps(card, ensure_ascii=False), now_iso()),
        )


# ---------- tracking ----------

def insert_tracking_sub(
    sub_id: str,
    task_id: str,
    cron: str,
    hour: int = 8,
    watermark: Optional[str] = None,
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO tracking_sub
               (sub_id, task_id, cron, hour, status, watermark, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (sub_id, task_id, cron, hour, watermark, now_iso()),
        )


def get_tracking_sub(sub_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM tracking_sub WHERE sub_id = ?', (sub_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_tracking_subs(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        if status:
            cur.execute('SELECT * FROM tracking_sub WHERE status = ? ORDER BY created_at DESC', (status,))
        else:
            cur.execute('SELECT * FROM tracking_sub ORDER BY created_at DESC')
        return [dict(r) for r in cur.fetchall()]


def update_tracking_sub(sub_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with db_cursor() as cur:
        cur.execute(f'UPDATE tracking_sub SET {cols} WHERE sub_id = ?', (*fields.values(), sub_id))


def delete_tracking_sub(sub_id: str) -> None:
    with db_cursor() as cur:
        cur.execute('DELETE FROM tracking_sub WHERE sub_id = ?', (sub_id,))
        cur.execute('DELETE FROM brief WHERE sub_id = ?', (sub_id,))


def insert_brief(
    brief_id: str,
    sub_id: str,
    *,
    run_id: Optional[str] = None,
    date: Optional[str] = None,
    markdown_path: Optional[str] = None,
    new_facts: int = 0,
    changed_facts: int = 0,
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO brief
               (brief_id, sub_id, run_id, date, markdown_path, new_facts, changed_facts, read)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (brief_id, sub_id, run_id, date or now_iso()[:10], markdown_path, new_facts, changed_facts),
        )


def list_briefs(sub_id: str) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM brief WHERE sub_id = ? ORDER BY date DESC', (sub_id,))
        return [dict(r) for r in cur.fetchall()]


def list_unread_briefs() -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM brief WHERE read = 0 ORDER BY date DESC')
        return [dict(r) for r in cur.fetchall()]


def mark_brief_read(brief_id: str) -> None:
    with db_cursor() as cur:
        cur.execute('UPDATE brief SET read = 1 WHERE brief_id = ?', (brief_id,))


# ---------- playbook helpers ----------

def update_playbook_rule(rule_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with db_cursor() as cur:
        cur.execute(f'UPDATE playbook_rule SET {cols} WHERE id = ?', (*fields.values(), rule_id))


def playbook_stats() -> Dict[str, Any]:
    with db_cursor() as cur:
        cur.execute('SELECT status, COUNT(*) as n FROM playbook_rule GROUP BY status')
        by_status = {r['status']: r['n'] for r in cur.fetchall()}
        cur.execute('SELECT COALESCE(SUM(hit_count),0) as hits, COALESCE(SUM(hit_stars_sum),0) as stars FROM playbook_rule')
        row = cur.fetchone()
        hits = int(row['hits'] or 0)
        stars = float(row['stars'] or 0)
        avg_hit = (stars / hits) if hits else 0.0
        cur.execute(
            """SELECT AVG(stars) as avg FROM feedback WHERE kind = 'report_stars' AND stars IS NOT NULL"""
        )
        overall = cur.fetchone()
        avg_all = float(overall['avg'] or 0) if overall and overall['avg'] is not None else 0.0
        return {
            'by_status': by_status,
            'total': sum(by_status.values()),
            'hit_count': hits,
            'avg_stars_hit': round(avg_hit, 2),
            'avg_stars_all': round(avg_all, 2),
        }


# ---------- scenario_run ----------

def insert_scenario_run(scenario_id: str, task_id: str, config: Dict[str, Any], status: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO scenario_run
               (scenario_id, task_id, config_json, status, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            (scenario_id, task_id, json.dumps(config, ensure_ascii=False), status, now_iso()),
        )


def update_scenario_run(scenario_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with db_cursor() as cur:
        cur.execute(f'UPDATE scenario_run SET {cols} WHERE scenario_id = ?', (*fields.values(), scenario_id))


def get_scenario_run(scenario_id: str) -> Optional[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute('SELECT * FROM scenario_run WHERE scenario_id = ?', (scenario_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def finish_task_run(run_id: str, status: str, **extra: Any) -> None:
    fields = {'status': status, 'finished_at': now_iso()}
    fields.update(extra)
    cols = ', '.join(f'{k} = ?' for k in fields)
    with db_cursor() as cur:
        cur.execute(f'UPDATE task_run SET {cols} WHERE run_id = ?', (*fields.values(), run_id))


def list_unreflected_runs(limit: int = 20) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM task_run WHERE reflected = 0 ORDER BY started_at DESC LIMIT ?',
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def mark_reflected(run_id: str) -> None:
    with db_cursor() as cur:
        cur.execute('UPDATE task_run SET reflected = 1 WHERE run_id = ?', (run_id,))
