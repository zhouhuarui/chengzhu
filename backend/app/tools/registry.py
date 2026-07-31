"""工具注册表 + 调用包装（写 tool_call_log）。"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ..utils.db import insert_tool_call_log
from . import announcements, financial, industry, news, quote, read_announcement, research, web_search
from .schema import EvidenceCard

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    'fetch_announcements': {
        'fn': announcements.fetch_announcements,
        'description': '获取个股巨潮公告列表',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_financial_statements': {
        'fn': financial.fetch_financial_statements,
        'description': '获取利润表/资产负债表/现金流量表',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_financial_indicators': {
        'fn': financial.fetch_financial_indicators,
        'description': '获取财务分析指标',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_stock_news': {
        'fn': news.fetch_stock_news,
        'description': '获取个股新闻',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_market_telegraph': {
        'fn': news.fetch_market_telegraph,
        'description': '获取市场快讯电报',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_research_reports': {
        'fn': research.fetch_research_reports,
        'description': '获取券商研报列表（不含全文）',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_industry_data': {
        'fn': industry.fetch_industry_data,
        'description': '获取行业板块与宏观数据',
        'phase': 'collect',
        'cost': 'free',
    },
    'fetch_stock_quote': {
        'fn': quote.fetch_stock_quote,
        'description': '获取行情与估值快照',
        'phase': 'collect',
        'cost': 'free',
    },
    'web_search': {
        'fn': web_search.web_search,
        'description': '博查联网搜索（大陆可用）',
        'phase': 'both',
        'cost': 'paid',
    },
    'read_announcement': {
        'fn': read_announcement.read_announcement,
        'description': '精读公告 PDF 全文',
        'phase': 'analyze',
        'cost': 'free',
    },
}


def get_tool(name: str) -> Dict[str, Any]:
    if name not in TOOL_REGISTRY:
        raise KeyError(f'unknown tool: {name}')
    return TOOL_REGISTRY[name]


def list_tools(phase: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    if not phase:
        return dict(TOOL_REGISTRY)
    return {k: v for k, v in TOOL_REGISTRY.items() if v.get('phase') in (phase, 'both')}


def get_tool(name: str) -> Callable:
    if name not in TOOL_REGISTRY:
        raise KeyError(f'unknown tool: {name}')
    return TOOL_REGISTRY[name]['fn']


def list_tools(phase: Optional[str] = None) -> list:
    out = []
    for name, meta in TOOL_REGISTRY.items():
        if phase and meta.get('phase') not in (phase, 'both'):
            continue
        out.append({
            'name': name,
            'description': meta.get('description'),
            'phase': meta.get('phase'),
            'cost': meta.get('cost'),
        })
    return out


def call_tool(
    name: str,
    *,
    run_id: Optional[str] = None,
    agent: Optional[str] = None,
    **params: Any,
) -> Any:
    if name not in TOOL_REGISTRY:
        raise KeyError(f'unknown tool: {name}')
    fn: Callable = TOOL_REGISTRY[name]['fn']
    t0 = time.time()
    ok = False
    degraded = False
    cards_returned = None
    error = None
    result: Any = None
    try:
        result = fn(**params)
        ok = True
        if isinstance(result, list):
            cards_returned = len(result)
            if result and isinstance(result[0], EvidenceCard):
                degraded = any(bool((c.structured or {}).get('degraded')) for c in result)
        return result
    except Exception as e:
        error = str(e)
        raise
    finally:
        try:
            insert_tool_call_log(
                tool_name=name,
                ok=ok,
                run_id=run_id,
                agent=agent,
                degraded=degraded,
                latency_ms=int((time.time() - t0) * 1000),
                cards_returned=cards_returned,
                error=error,
            )
        except Exception:
            pass
