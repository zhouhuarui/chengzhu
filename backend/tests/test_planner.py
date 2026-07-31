"""Planner 启发式解析验收（无 LLM Key）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services.planner import PlannerService, _heuristic_parse


def test_compare_ningde_byd():
    card = _heuristic_parse('对比宁德时代和比亚迪最近的财务表现')
    assert card.deliverable == 'compare'
    codes = {s.code for s in card.symbols}
    assert '300750' in codes
    assert '002594' in codes


def test_tracking_daily():
    card = _heuristic_parse('每天盯一下贵州茅台的公告和新闻')
    assert card.deliverable == 'tracking'
    assert any(s.code == '600519' for s in card.symbols)


def test_summary_code():
    card = _heuristic_parse('整理 600519 最近一个季度的公告和财报')
    assert card.deliverable == 'summary'
    assert any(s.code == '600519' for s in card.symbols)


def test_planner_service_no_key():
    svc = PlannerService()
    card = svc.plan('对比宁德和五粮液')
    assert card.deliverable == 'compare'
    codes = {s.code for s in card.symbols}
    assert '300750' in codes
    assert '000858' in codes


def test_unknown_needs_clarification():
    card = _heuristic_parse('帮我看看这家公司最近怎么样')
    assert card.clarifications


def test_owned_planner_client_reserves_against_task_budget(monkeypatch):
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'offline-key')
    service = PlannerService(budget_run_id='task-budget-owner')

    assert service.llm.budget_run_id == 'task-budget-owner'
