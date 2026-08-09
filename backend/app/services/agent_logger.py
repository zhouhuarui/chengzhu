"""Agent 过程日志（移植 MiroFish ReportLogger，泛化 task_id + agent）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from ..models.research_task import task_artifact_folder
from ..team.redaction import redact_event_payload


def _safe_log_value(value: Any, key: str = '') -> Any:
    """Use the same bounded privacy gate as SQLite/Matrix team events."""

    return redact_event_payload(value, _key=str(key or ''))


class AgentLogger:
    def __init__(
        self,
        task_id: str,
        agent: str = 'system',
        run_id: Optional[str] = None,
        *,
        team_task_id: Optional[str] = None,
        matrix_event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.agent = agent
        self.team_task_id = team_task_id
        self.matrix_event_id = matrix_event_id
        self.trace_id = trace_id
        self.span_id = span_id
        self.start_time = datetime.now()
        self.log_file_path = os.path.join(
            task_artifact_folder(task_id, run_id), 'agent_log.jsonl'
        )
        os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)

    def _elapsed(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()

    def log(
        self,
        action: str,
        stage: str,
        details: Dict[str, Any],
        section_title: Optional[str] = None,
        section_index: Optional[int] = None,
        agent: Optional[str] = None,
        team_task_id: Optional[str] = None,
        matrix_event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> None:
        try:
            from ..observability import current_trace
            active_trace = current_trace()
        except Exception:
            active_trace = None
        entry = {
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'elapsed_seconds': round(self._elapsed(), 2),
            'task_id': self.task_id,
            'run_id': self.run_id,
            'agent': agent or self.agent,
            'action': action,
            'stage': stage,
            'section_title': section_title,
            'section_index': section_index,
            'team_task_id': team_task_id or self.team_task_id,
            'matrix_event_id': matrix_event_id or self.matrix_event_id,
            'trace_id': trace_id or self.trace_id or (
                active_trace.trace_id if active_trace else None
            ),
            'span_id': span_id or self.span_id or (
                active_trace.span_id if active_trace else None
            ),
            'details': _safe_log_value(details),
        }
        with open(self.log_file_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    @classmethod
    def read_from_line(
        cls,
        task_id: str,
        from_line: int = 0,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = os.path.join(task_artifact_folder(task_id, run_id), 'agent_log.jsonl')
        if not os.path.exists(path):
            return {'lines': [], 'next_line': 0, 'finished': False}
        with open(path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        slice_ = all_lines[from_line:]
        parsed = []
        for line in slice_:
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        finished = any(p.get('action') in ('task_complete', 'task_failed') for p in parsed)
        return {
            'lines': parsed,
            'next_line': from_line + len(slice_),
            'finished': finished,
        }
