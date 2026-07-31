"""财报工具冒烟测试（需网络）。"""

import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

pd = pytest.importorskip('pandas')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.config import Config
from app.utils.db import init_db


@pytest.fixture(scope='module', autouse=True)
def _db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    Config.DB_PATH = path
    from app.utils import db as dbmod
    if getattr(dbmod._local, 'conn', None):
        dbmod._local.conn.close()
        dbmod._local.conn = None
    init_db()


@pytest.mark.network
def test_fetch_income_maotai():
    from app.tools.financial import fetch_financial_statements
    cards = fetch_financial_statements('600519', statement='income', period_count=4)
    assert isinstance(cards, list)
    if cards:
        c = cards[0]
        assert c.source_type == 'financial_report'
        assert c.symbol == '600519'
        assert c.excerpt


def _install_public_fake(monkeypatch, fake_akshare):
    from app.tools import financial

    monkeypatch.setitem(sys.modules, 'akshare', fake_akshare)
    monkeypatch.setattr(financial.limiter, 'wait', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(financial, 'retry_call', lambda fn, *args, **kwargs: fn())
    monkeypatch.setattr(financial, 'run_with_timeout', lambda fn, *_args, **_kwargs: fn())
    return financial


@pytest.mark.parametrize(('statement', 'expected_basis'), [
    ('income', 'cumulative'),
    ('cashflow', 'cumulative'),
    ('balance', 'point_in_time'),
])
def test_eastmoney_uses_notice_date_and_explicit_report_type_metadata(
    monkeypatch,
    statement,
    expected_basis,
):
    row = {
        'REPORT_DATE': '2025-06-30',
        'NOTICE_DATE': '2025-08-01',
        'TOTAL_OPERATE_INCOME': 10_000_000_000,
        'TOTAL_ASSETS': 20_000_000_000,
        'NETCASH_OPERATE': 3_000_000_000,
    }
    frame = pd.DataFrame([row])
    fake = SimpleNamespace(
        stock_profit_sheet_by_report_em=lambda **_kwargs: frame,
        stock_balance_sheet_by_report_em=lambda **_kwargs: frame,
        stock_cash_flow_sheet_by_report_em=lambda **_kwargs: frame,
    )
    financial = _install_public_fake(monkeypatch, fake)

    card = financial._fetch_financial_statements_public(
        '600519', statement=statement, period_count=1,
    )[0]

    assert card.publish_time == '2025-08-01T00:00:00+08:00'
    assert '2025-06-30' in card.title
    assert card.structured['report_period'] == '2025-06-30T00:00:00+08:00'
    assert card.structured['report_type'] == 1
    assert card.structured['source_report_type'] == 1
    assert card.structured['consolidation_scope'] == 'consolidated'
    assert card.structured['merged_flag'] == 1
    assert card.structured['currency'] == 'CNY'
    assert card.structured['accumulation_basis'] == expected_basis


def test_eastmoney_never_substitutes_report_date_for_missing_notice_date(monkeypatch):
    frame = pd.DataFrame([{
        'REPORT_DATE': '2025-12-31',
        'TOTAL_OPERATE_INCOME': 10_000_000_000,
    }])
    fake = SimpleNamespace(stock_profit_sheet_by_report_em=lambda **_kwargs: frame)
    financial = _install_public_fake(monkeypatch, fake)

    card = financial._fetch_financial_statements_public(
        '600519', statement='income', period_count=1,
    )[0]

    assert card.publish_time == ''
    assert card.structured['publish_date'] == ''
    assert card.structured['report_period'].startswith('2025-12-31')


def test_sina_fallback_uses_only_source_announcement_currency_and_type(monkeypatch):
    frame = pd.DataFrame([
        {
            '报告日': '2025-12-31',
            '公告日期': '2026-03-20',
            '币种': '人民币',
            '类型': '合并报表',
            '营业收入': 10_000_000_000,
        },
        {
            '报告日': '2025-09-30',
            '公告日期': None,
            '币种': None,
            '类型': None,
            '营业收入': 7_000_000_000,
        },
    ])

    def unavailable(**_kwargs):
        raise RuntimeError('eastmoney unavailable')

    fake = SimpleNamespace(
        stock_profit_sheet_by_report_em=unavailable,
        stock_financial_report_sina=lambda **_kwargs: frame,
    )
    financial = _install_public_fake(monkeypatch, fake)

    first, second = financial._fetch_financial_statements_public(
        '600519', statement='income', period_count=2,
    )

    assert first.source_name == '新浪财经'
    assert first.publish_time == '2026-03-20T00:00:00+08:00'
    assert first.structured['currency'] == 'CNY'
    assert first.structured['source_report_type'] == '合并报表'
    assert first.structured['consolidation_scope'] == 'consolidated'
    assert first.structured['merged_flag'] == 1

    assert second.publish_time == ''
    assert second.structured['currency'] == ''
    assert second.structured['source_report_type'] == 'unknown'
    assert second.structured['consolidation_scope'] == 'unknown'
    assert second.structured['merged_flag'] is None


def test_public_financial_indicators_leave_publish_time_empty_without_disclosure(monkeypatch):
    frame = pd.DataFrame([{
        '日期': '2025-06-30',
        '加权净资产收益率': 12.3,
    }])
    fake = SimpleNamespace(stock_financial_analysis_indicator=lambda **_kwargs: frame)
    financial = _install_public_fake(monkeypatch, fake)

    card = financial._fetch_financial_indicators_public('600519')[0]

    assert card.publish_time == ''
    assert card.structured['publish_date'] == ''
    assert card.structured['report_period'].startswith('2025-06-30')
    assert '2025-06-30' in card.title
