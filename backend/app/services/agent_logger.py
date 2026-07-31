"""Agent 过程日志（移植 MiroFish ReportLogger，泛化 task_id + agent）。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

from ..models.research_task import task_artifact_folder


_SENSITIVE_KEYS = {
    'prompt', 'messages', 'api_key', 'authorization', 'reasoning_content',
    'chain_of_thought', 'thought', 'raw_thought',
}


def _safe_log_value(value: Any, key: str = '') -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): _safe_log_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    if isinstance(value, str):
        if 'data:image/' in value.lower() and 'base64,' in value.lower():
            return '[image payload omitted]'
        value = re.sub(r'(?i)bearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
        return re.sub(
            r'(?i)(api[_-]?key|token|authorization)\s*[:=]\s*[^\s,;]+',
            r'\1=[REDACTED]',
            value,
        )
    return value


class AgentLogger:
    def __init__(
        self,
        task_id: str,
        agent: str = 'system',
        run_id: Optional[str] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.agent = agent
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
    ) -> None:
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
