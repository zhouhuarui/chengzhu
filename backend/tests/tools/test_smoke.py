"""Phase 1 工具冒烟：真实网络，symbol=600519。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.tools.schema import EvidenceCard
from app.utils.db import init_db
from app.config import Config


@pytest.fixture(scope='module', autouse=True)
def _db():
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    init_db()


def _assert_cards(cards, min_n=1):
    assert isinstance(cards, list)
    assert len(cards) >= min_n
    for c in cards:
        assert isinstance(c, EvidenceCard)
        assert c.title
        assert c.source_type
        assert c.fetch_tool
        assert 1 <= c.reliability <= 5


@pytest.mark.network
def test_fetch_announcements():
    from app.tools.announcements import fetch_announcements
    cards = fetch_announcements('600519', '2026-01-01', '2026-07-25', max_count=10)
    _assert_cards(cards, 1)


@pytest.mark.network
def test_fetch_financial_statements_income():
    from app.tools.financial import fetch_financial_statements
    cards = fetch_financial_statements('600519', statement='income', period_count=4)
    _assert_cards(cards, 1)
    assert cards[0].source_type == 'financial_report'


@pytest.mark.network
def test_fetch_stock_news():
    from app.tools.news import fetch_stock_news
    cards = fetch_stock_news('600519', max_count=5)
    _assert_cards(cards, 1)


@pytest.mark.network
def test_fetch_research_reports():
    from app.tools.research import fetch_research_reports
    cards = fetch_research_reports('600519', max_count=5)
    _assert_cards(cards, 1)


@pytest.mark.network
def test_fetch_stock_quote():
    from app.tools.quote import fetch_stock_quote
    cards = fetch_stock_quote('600519', days=60)
    _assert_cards(cards, 1)
    assert 'latest_close' in cards[0].structured


@pytest.mark.network
def test_registry_wrapper_logs():
    from app.tools.registry import call_tool
    from app.utils.db import get_connection
    cards = call_tool('fetch_stock_quote', run_id='smoke_run', agent='test', symbol='600519', days=30)
    _assert_cards(cards, 1)
    cur = get_connection().cursor()
    cur.execute("SELECT ok FROM tool_call_log WHERE tool_name='fetch_stock_quote' ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    assert row is not None
    assert row['ok'] == 1


def test_web_search_no_key_graceful(monkeypatch):
    from app.tools.web_search import reset_web_search_budget, web_search
    # Keep this no-key contract deterministic even when the local .env has a
    # real Bocha credential configured.
    monkeypatch.setattr(Config, 'BOCHA_API_KEY', None)
    reset_web_search_budget()
    # 无 key 时不抛异常
    cards = web_search('贵州茅台 2026 一季报')
    assert cards
    assert cards[0].structured.get('search_unavailable') is True
