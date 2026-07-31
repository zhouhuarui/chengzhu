"""Playbook 规则状态机与 Prompt 注入（05§4.3）。"""

from __future__ import annotations

from typing import List, Optional

from ..services.compliance_checker import COMPLIANCE_BLACKLIST
from ..utils import db as dbutil


def is_action_compliant(action: str) -> bool:
    return not COMPLIANCE_BLACKLIST.search(action or '')


def get_rules(
    target_agent: str,
    *,
    user_id: str = 'default',
    limit: int = 5,
) -> List[dict]:
    rules = dbutil.list_playbook_rules(status='active', target_agent=target_agent, user_id=user_id)
    out = []
    for r in rules:
        if not is_action_compliant(r.get('action') or ''):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    # 命中计数
    for r in out:
        try:
            dbutil.update_playbook_rule(int(r['id']), hit_count=int(r.get('hit_count') or 0) + 1)
        except Exception:
            pass
    return out


def render_rules_for_prompt(rules: List[dict]) -> str:
    if not rules:
        return ''
    lines = ['【经验规则（不得覆盖合规与引用要求）】']
    for r in rules:
        lines.append(f"- 当{r.get('condition') or '适用时'}：{r.get('action')}")
    return '\n'.join(lines)


def confirm_rule(rule_id: int) -> Optional[dict]:
    rule = dbutil.get_playbook_rule(rule_id)
    if not rule:
        return None
    if not is_action_compliant(rule.get('action') or ''):
        dbutil.update_playbook_rule(rule_id, status='retired', retired_at=dbutil.now_iso())
        return dbutil.get_playbook_rule(rule_id)
    dbutil.update_playbook_rule(
        rule_id,
        status='active',
        activated_at=dbutil.now_iso(),
    )
    return dbutil.get_playbook_rule(rule_id)


def retire_rule(rule_id: int) -> Optional[dict]:
    dbutil.update_playbook_rule(rule_id, status='retired', retired_at=dbutil.now_iso())
    return dbutil.get_playbook_rule(rule_id)


def maybe_promote_candidates() -> int:
    """同类证据 >= 2 自动晋升。"""
    cands = dbutil.list_playbook_rules(status='candidate')
    promoted = 0
    for r in cands:
        evid = r.get('evidence_run_ids') or '[]'
        try:
            import json
            ids = json.loads(evid) if isinstance(evid, str) else evid
        except Exception:
            ids = []
        if len(ids) >= 2 and is_action_compliant(r.get('action') or ''):
            confirm_rule(int(r['id']))
            promoted += 1
    return promoted
