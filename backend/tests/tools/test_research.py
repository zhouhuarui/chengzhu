"""Research-report evidence must not restate broker rating labels as prose."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

pd = pytest.importorskip('pandas')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))


def test_rating_is_structured_metadata_but_not_excerpt(monkeypatch):
    from app.services.compliance_checker import check_compliance
    from app.tools import research

    frame = pd.DataFrame([{
        '报告名称': '盈利质量跟踪',
        '机构': '示例证券',
        '评级': '买入',
        '日期': '2026-01-02',
        '链接': 'https://example.test/research/1',
    }])
    fake = SimpleNamespace(stock_research_report_em=lambda **_kwargs: frame)
    monkeypatch.setitem(sys.modules, 'akshare', fake)
    monkeypatch.setattr(research.limiter, 'wait', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(research, 'retry_call', lambda fn, *args, **kwargs: fn())
    monkeypatch.setattr(research, 'run_with_timeout', lambda fn, *_args, **_kwargs: fn())

    card = research.fetch_research_reports('600519', max_count=1)[0]

    assert card.structured['rating'] == '买入'
    assert '买入' not in card.excerpt
    assert '评级' not in card.excerpt
    assert not check_compliance(card.excerpt)
