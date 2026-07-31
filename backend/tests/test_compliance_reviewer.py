"""合规与 chart 校验验收。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.compliance_checker import (
    check_chart_blocks,
    check_compliance,
    check_citations,
)


def test_blacklist_blocks_advice():
    issues = check_compliance('我们认为建议买入该股，目标价 100 元')
    types = {i['type'] for i in issues}
    assert 'compliance' in types
    assert any('建议买入' in i['quote'] or '目标价' in i['quote'] for i in issues)


def test_blacklist_blocks_directional_debate_language():
    issues = check_compliance('结论形成看多观点，并给出 Alpha 信号和买卖倾向。')
    quotes = {item['quote'] for item in issues}
    assert {'看多', 'Alpha 信号', '买卖倾向'}.issubset(quotes)


@pytest.mark.parametrize('phrase', [
    '建议继续持有',
    '建议 买 入',
    '维持“买入”评级',
    '给予「强烈买入」评级',
    '逢低 买 入',
    '形成买 入信号',
    '结论看 空',
    '给出「买入」评级',
    '买入评级',
    '应当买入',
    '应该卖出',
    '可以买入',
    '可以卖出',
    '可以买/卖出',
    '评级为买入',
    '本机构评级：卖出',
    '评级为“强烈买入”',
    '建议投资者买入',
    '强烈买入',
    '投资者可考虑买入',
])
def test_blacklist_blocks_spacing_and_quote_variants(phrase):
    assert check_compliance(phrase), phrase


def test_citation_invalid_id():
    issues = check_citations('净利润增长[E999]', valid_ids={1, 2})
    assert any(i['type'] == 'citation_mismatch' for i in issues)


def test_chart_number_mismatch():
    text = '''```chart
{"type":"bar","title":"t","x":["A"],"series":[{"name":"净利润","data":[99999]}],"source_refs":["E1"]}
```'''
    issues = check_chart_blocks(text, {1: '归母净利润 100 亿元'})
    assert any(i['type'] == 'number_error' for i in issues)


def test_chart_ok_when_value_in_evidence():
    text = '''```chart
{"type":"bar","title":"t","x":["A"],"series":[{"name":"净利润","data":[100]}],"source_refs":["E1"]}
```'''
    issues = check_chart_blocks(text, {1: '归母净利润 100 亿元'})
    assert not any(i['type'] == 'number_error' for i in issues)


def test_chart_rejects_legacy_top_level_values_bypass():
    text = '''```chart
{"type":"bar","title":"错期混比","x":["H1","Q1"],"values":[100,90],"source_refs":["E1","E2"]}
```'''
    issues = check_chart_blocks(text, {1: 'H1 100', 2: 'Q1 90'})
    assert any(item['type'] == 'chart_schema' for item in issues)
