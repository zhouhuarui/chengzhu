"""反馈 → Reflection → Playbook 验收。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.utils.db import init_db, insert_feedback, list_playbook_rules, get_user_preference
from app.services.reflection import reflect_on_run
from app.services.playbook import confirm_rule, is_action_compliant, get_rules
from app.config import Config


def test_downvote_table_creates_style_rule(monkeypatch):
    # This is a deterministic fallback test. Never let a developer's configured
    # model key turn it into a live, billable request.
    monkeypatch.setattr(Config, 'LLM_API_KEY', None)
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', None)
    init_db()
    run_id = 'run_test_reflect_table'
    insert_feedback(run_id, 'section_vote', section_index=0, vote='down', comment='只要表格')
    out = reflect_on_run(run_id)
    assert out.get('rules_created')
    rules = list_playbook_rules(status='candidate', target_agent='analyst')
    assert any(r['rule_type'] == 'style' and '表格' in (r.get('action') or '') for r in rules)
    assert get_user_preference('deliverable_style.broker_view') == 'table'


def test_noncompliant_action_filtered():
    assert not is_action_compliant('请给出建议买入提示')
    assert is_action_compliant('相关章节优先使用 Markdown 表格')


def test_confirm_promotes_to_active():
    init_db()
    from app.utils import db as dbutil
    rid = dbutil.insert_playbook_rule(
        'style', 'user', 'analyst', '用表格呈现财务对比',
        condition='财务对比', confidence=0.9, status='candidate',
    )
    rule = confirm_rule(rid)
    assert rule and rule['status'] == 'active'
    active = get_rules('analyst')
    assert any(int(r['id']) == rid for r in active)
