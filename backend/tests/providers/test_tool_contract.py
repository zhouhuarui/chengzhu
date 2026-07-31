import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import app.providers
from app.providers.datayes import (
    DatayesApiClient,
    DatayesApiProvider,
    DatayesWarehouse,
    DatayesWarehouseProvider,
)
from app.tools import announcements, financial, industry, quote


def _params(fn):
    return list(inspect.signature(fn).parameters)


def test_public_tool_signatures_remain_compatible():
    assert _params(financial.fetch_financial_statements) == ['symbol', 'statement', 'period_count']
    assert _params(financial.fetch_financial_indicators) == ['symbol', 'start_year']
    assert _params(quote.fetch_stock_quote) == ['symbol', 'days']
    assert _params(industry.fetch_industry_data) == ['industry', 'macro_indicators', 'symbol']
    assert _params(announcements.fetch_announcements) == [
        'symbol', 'start_date', 'end_date', 'category', 'max_count'
    ]


def test_plan_provider_names_are_public_aliases():
    assert DatayesApiProvider is DatayesApiClient
    assert DatayesWarehouseProvider is DatayesWarehouse


def test_keyless_mode_preserves_public_fallbacks(monkeypatch):
    class DisabledRouter:
        enabled = False

    monkeypatch.setattr(app.providers, 'get_provider_router', lambda: DisabledRouter())
    sentinel = object()

    monkeypatch.setattr(financial, '_fetch_financial_statements_public', lambda *args: sentinel)
    monkeypatch.setattr(financial, '_fetch_financial_indicators_public', lambda *args: sentinel)
    monkeypatch.setattr(quote, '_fetch_stock_quote_public', lambda *args: sentinel)
    monkeypatch.setattr(industry, '_fetch_industry_data_public', lambda *args: sentinel)
    monkeypatch.setattr(announcements, '_fetch_announcements_public', lambda *args: sentinel)

    assert financial.fetch_financial_statements('000001') is sentinel
    assert financial.fetch_financial_indicators('000001') is sentinel
    assert quote.fetch_stock_quote('000001') is sentinel
    assert industry.fetch_industry_data(symbol='000001') is sentinel
    assert announcements.fetch_announcements('000001', '2026-01-01', '2026-07-01') is sentinel
