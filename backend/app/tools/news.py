"""个股新闻 + 市场电报（04§3.4-3.5）。"""

from __future__ import annotations

import difflib
from typing import List, Optional

from ._helpers import retry_call, run_with_timeout, to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard
from .symbol import normalize_symbol, to_market_symbol


def _dedupe_by_title(cards: List[EvidenceCard], threshold: float = 0.85) -> List[EvidenceCard]:
    kept: List[EvidenceCard] = []
    for c in sorted(cards, key=lambda x: x.publish_time or '', reverse=True):
        if any(difflib.SequenceMatcher(None, c.title, k.title).ratio() > threshold for k in kept):
            continue
        kept.append(c)
    return kept


def fetch_stock_news(symbol: str, max_count: int = 20) -> List[EvidenceCard]:
    import akshare as ak

    code = normalize_symbol(symbol)
    market = to_market_symbol(symbol)
    limiter.wait('eastmoney')

    def _call():
        return ak.stock_news_em(symbol=code)

    df = retry_call(lambda: run_with_timeout(_call, 30))
    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    title_col = next((c for c in df.columns if '标题' in str(c)), None)
    content_col = next((c for c in df.columns if '内容' in str(c) or '摘要' in str(c)), None)
    time_col = next((c for c in df.columns if '时间' in str(c) or '日期' in str(c)), None)
    src_col = next((c for c in df.columns if '来源' in str(c)), None)
    url_col = next((c for c in df.columns if '链接' in str(c) or '网址' in str(c)), None)

    for _, row in df.iterrows():
        title = str(row[title_col]) if title_col else str(row.iloc[0])
        content = str(row[content_col]) if content_col and row[content_col] == row[content_col] else title
        src = str(row[src_col]) if src_col and row[src_col] == row[src_col] else '东方财富'
        url = str(row[url_col]) if url_col and row[url_col] == row[url_col] else 'https://finance.eastmoney.com/'
        cards.append(EvidenceCard(
            source_type='news',
            title=title,
            url=url,
            publish_time=to_iso(row[time_col]) if time_col else '',
            source_name=src,
            symbol=market,
            excerpt=truncate(content, 500),
            structured={'degraded': False},
            reliability=4,
            fetch_tool='fetch_stock_news',
        ))
    return _dedupe_by_title(cards)[:max_count]


def fetch_market_telegraph(keyword: Optional[str] = None, max_count: int = 30) -> List[EvidenceCard]:
    import akshare as ak

    degraded = False
    limiter.wait('cls')

    def _cls():
        return ak.stock_telegraph_cls()

    try:
        df = retry_call(lambda: run_with_timeout(_cls, 30))
        source_name = '财联社'
    except Exception:
        degraded = True
        limiter.wait('eastmoney')

        def _em():
            return ak.stock_info_global_em()

        df = retry_call(lambda: run_with_timeout(_em, 30))
        source_name = '东方财富'

    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    title_col = next((c for c in df.columns if '标题' in str(c) or '内容' in str(c)), list(df.columns)[0])
    time_col = next((c for c in df.columns if '时间' in str(c) or '日期' in str(c)), None)

    for _, row in df.iterrows():
        title = str(row[title_col])
        text = ' '.join(str(row[c]) for c in df.columns if row[c] == row[c])
        if keyword and keyword not in title and keyword not in text:
            continue
        cards.append(EvidenceCard(
            source_type='news',
            title=truncate(title, 120),
            url='https://www.cls.cn/' if not degraded else 'https://finance.eastmoney.com/a/cywjh.html',
            publish_time=to_iso(row[time_col]) if time_col else '',
            source_name=source_name,
            symbol=None,
            excerpt=truncate(text, 500),
            structured={'degraded': degraded, 'keyword': keyword},
            reliability=4,
            fetch_tool='fetch_market_telegraph',
        ))
        if len(cards) >= max_count:
            break
    return cards
