"""Reflection Agent：反馈 → playbook candidate + 偏好更新。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils import db as dbutil
from .playbook import is_action_compliant


def _heuristic_rules(run_id: str, feedbacks: List[Dict[str, Any]]) -> Dict[str, Any]:
    rules = []
    prefs = []
    for fb in feedbacks:
        comment = (fb.get('comment') or '').strip()
        vote = fb.get('vote')
        if vote == 'down' and ('表格' in comment or 'table' in comment.lower()):
            rules.append({
                'rule_type': 'style',
                'scope': 'user',
                'target_agent': 'analyst',
                'condition': '用户对章节表达偏好表格',
                'action': '相关章节优先使用 Markdown 表格呈现关键指标，少写长段落',
                'evidence': comment or 'downvote+表格',
                'confidence': 0.85,
            })
            prefs.append({'key': 'deliverable_style.broker_view', 'value': 'table'})
        if '太长' in comment or '简短' in comment:
            rules.append({
                'rule_type': 'style',
                'scope': 'user',
                'target_agent': 'analyst',
                'condition': '用户反馈篇幅过长',
                'action': '每章正文控制在 400 字以内，优先要点列表',
                'evidence': comment,
                'confidence': 0.7,
            })
            prefs.append({'key': 'report_length', 'value': 'brief'})
        if fb.get('kind') == 'correction':
            rules.append({
                'rule_type': 'prompt_patch',
                'scope': 'user',
                'target_agent': 'analyst',
                'condition': '用户指出事实错误',
                'action': '引用数字前再次核对证据卡原文，禁止估算',
                'evidence': comment,
                'confidence': 0.75,
            })
    # 最多 3 条
    return {'rules': rules[:3], 'user_preference_updates': prefs}


def reflect_on_run(run_id: str) -> Dict[str, Any]:
    feedbacks = dbutil.list_feedback(run_id)
    if not feedbacks:
        dbutil.mark_reflected(run_id)
        return {'rules': [], 'prefs': []}

    result: Dict[str, Any]
    if Config.TEXT_LLM_API_KEY:
        llm = None
        try:
            from ..utils.llm_client import LLMClient
            llm = LLMClient(
                api_key=Config.TEXT_LLM_API_KEY,
                base_url=Config.TEXT_LLM_BASE_URL,
                model=Config.TEXT_LLM_FAST_MODEL,
                provider=Config.TEXT_LLM_PROVIDER,
                budget_run_id=run_id,
            )
            digest = {
                'run_id': run_id,
                'feedback': feedbacks,
                'task_run': dbutil.get_task_run(run_id),
            }
            prompt = (
                '你是多 Agent 投研系统的流程优化分析师。根据运行记录与反馈归纳可复用规则。'
                '输出 JSON：{"rules":[{"rule_type":"style|routing|prompt_patch|source_health",'
                '"scope":"user|global","target_agent":"...","condition":"...","action":"...",'
                '"evidence":"...","confidence":0.0}],"user_preference_updates":[{"key","value"}]}。'
                '一次最多 3 条；action 必须具体可执行；禁止与合规红线冲突。\n'
                f'运行记录：{json.dumps(digest, ensure_ascii=False)[:6000]}'
            )
            llm_result = llm.chat_json_result(
                [{'role': 'user', 'content': prompt}],
                temperature=0.2,
                max_tokens=2048,
                max_attempts=2,
                thinking=False,
            )
            from ..utils.llm_audit import record_llm_result
            record_llm_result(run_id, 'reflection', llm_result)
            result = llm_result.parsed_json or {}
        except Exception as error:
            from ..utils.llm_audit import record_llm_client_error
            record_llm_client_error(run_id, 'reflection', llm, error)
            result = _heuristic_rules(run_id, feedbacks)
    else:
        result = _heuristic_rules(run_id, feedbacks)

    created = []
    for r in result.get('rules') or []:
        action = r.get('action') or ''
        if not is_action_compliant(action):
            continue
        rid = dbutil.insert_playbook_rule(
            rule_type=r.get('rule_type') or 'style',
            scope=r.get('scope') or 'user',
            target_agent=r.get('target_agent') or 'analyst',
            action=action,
            condition=r.get('condition') or '',
            confidence=float(r.get('confidence') or 0.5),
            evidence_run_ids=[run_id],
            status='candidate',
        )
        created.append(rid)

    for p in result.get('user_preference_updates') or []:
        if p.get('key'):
            dbutil.upsert_user_preference(p['key'], p.get('value'))

    dbutil.mark_reflected(run_id)
    return {'rules_created': created, 'raw': result}


def reflect_async(run_id: str) -> None:
    import threading

    def _run():
        try:
            reflect_on_run(run_id)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
