"""数据源健康度（04§5）。"""

from __future__ import annotations

from typing import Any, Dict

from ..utils.db import db_cursor


def get_source_health(tool_name: str, window_days: int = 7) -> Dict[str, Any]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(ok) AS ok_n,
              AVG(latency_ms) AS avg_latency_ms,
              AVG(degraded) AS degraded_rate
            FROM tool_call_log
            WHERE tool_name = ?
              AND created_at >= datetime('now', ?, 'localtime')
            """,
            (tool_name, f'-{int(window_days)} days'),
        )
        row = cur.fetchone()
    total = int(row['total'] or 0) if row else 0
    if total == 0:
        return {
            'tool_name': tool_name,
            'success_rate': None,
            'avg_latency_ms': None,
            'degraded_rate': None,
            'samples': 0,
        }
    ok_n = int(row['ok_n'] or 0)
    return {
        'tool_name': tool_name,
        'success_rate': round(ok_n / total, 4),
        'avg_latency_ms': round(float(row['avg_latency_ms'] or 0), 1),
        'degraded_rate': round(float(row['degraded_rate'] or 0), 4),
        'samples': total,
    }


def get_all_source_health(window_days: int = 7) -> Dict[str, Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT tool_name FROM tool_call_log
            WHERE created_at >= datetime('now', ?, 'localtime')
            """,
            (f'-{int(window_days)} days',),
        )
        names = [r['tool_name'] for r in cur.fetchall()]
    return {n: get_source_health(n, window_days) for n in names}
