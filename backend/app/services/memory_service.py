"""用户记忆：预填、偏好、L2 episode。"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List

from ..utils import db as dbutil
from ..utils.graph_client import get_graph_client, user_group_id
from ..models.research_task import ResearchTask


def compute_watch_symbols(user_id: str = 'default', limit_tasks: int = 10) -> List[str]:
    tasks = ResearchTask.list_tasks(limit=limit_tasks)
    counter: Counter = Counter()
    for t in tasks:
        if t.user_id != user_id:
            continue
        card = t.task_card or {}
        for s in card.get('symbols') or []:
            code = s.get('code')
            if code:
                counter[code] += 1
    return [c for c, n in counter.most_common() if n >= 2][:10]


def get_prefill(user_id: str = 'default') -> Dict[str, Any]:
    watch = dbutil.get_user_preference('watch_symbols', user_id) or compute_watch_symbols(user_id)
    if watch and not dbutil.get_user_preference('watch_symbols', user_id):
        dbutil.upsert_user_preference('watch_symbols', watch, user_id)
    return {
        'watch_symbols': watch or [],
        'watch_industries': dbutil.get_user_preference('watch_industries', user_id) or [],
        'default_time_window': dbutil.get_user_preference('default_time_window', user_id) or '1Q',
        'deliverable_style': dbutil.get_user_preference('deliverable_style.broker_view', user_id) or 'prose',
        'report_length': dbutil.get_user_preference('report_length', user_id) or 'normal',
        'recent_focus_points': dbutil.get_user_preference('recent_focus_points', user_id) or [],
    }


def style_directives(user_id: str = 'default') -> str:
    parts = []
    style = dbutil.get_user_preference('deliverable_style.broker_view', user_id)
    if style == 'table':
        parts.append("「券商观点汇总」章节用 Markdown 表格呈现，不写长段落。")
    length = dbutil.get_user_preference('report_length', user_id)
    if length == 'brief':
        parts.append('报告偏简短：每章以要点列表为主，控制篇幅。')
    return '\n'.join(parts)


def remember_task_episode(task_id: str, user_id: str = 'default') -> None:
    task = ResearchTask.load(task_id)
    if not task:
        return
    fbs = dbutil.list_feedback(task_id)
    body = (
        f'任务需求：{task.requirement}\n'
        f'任务卡：{json.dumps(task.task_card or {}, ensure_ascii=False)[:1500]}\n'
        f'反馈：{json.dumps(fbs, ensure_ascii=False)[:1500]}'
    )
    client = get_graph_client(user_group_id(user_id))
    client.add_episode(body=body, meta={'task_id': task_id})
    # 更新 watch_symbols
    codes = [s.get('code') for s in (task.task_card or {}).get('symbols') or [] if s.get('code')]
    if codes:
        existing = set(dbutil.get_user_preference('watch_symbols', user_id) or [])
        existing.update(codes)
        dbutil.upsert_user_preference('watch_symbols', list(existing)[:20], user_id)


def clear_user_memory(user_id: str = 'default') -> None:
    dbutil.clear_user_preferences(user_id)
    get_graph_client(user_group_id(user_id)).delete_group()
