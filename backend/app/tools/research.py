"""券商研报列表（04§3.6）— 不下载全文 PDF。"""

from __future__ import annotations

from typing import List

from ._helpers import retry_call, run_with_timeout, to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard
from .symbol import normalize_symbol, to_market_symbol


def fetch_research_reports(symbol: str, max_count: int = 20) -> List[EvidenceCard]:
    import akshare as ak

    code = normalize_symbol(symbol)
    market = to_market_symbol(symbol)
    limiter.wait('eastmoney')

    def _call():
        return ak.stock_research_report_em(symbol=code)

    df = retry_call(lambda: run_with_timeout(_call, 30))
    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    name_col = next((c for c in df.columns if '名称' in str(c) or '标题' in str(c) or '报告' in str(c)), None)
    org_col = next((c for c in df.columns if '机构' in str(c) or '券商' in str(c)), None)
    rating_col = next((c for c in df.columns if '评级' in str(c)), None)
    date_col = next((c for c in df.columns if '日期' in str(c) or '时间' in str(c)), None)
    url_col = next((c for c in df.columns if '链接' in str(c) or 'pdf' in str(c).lower()), None)

    for _, row in df.head(max_count).iterrows():
        title = str(row[name_col]) if name_col else '研报'
        org = str(row[org_col]) if org_col and row[org_col] == row[org_col] else '未知机构'
        rating = str(row[rating_col]) if rating_col and row[rating_col] == row[rating_col] else ''
        url = str(row[url_col]) if url_col and row[url_col] == row[url_col] else 'https://data.eastmoney.com/report/'
        # Rating labels such as “买入/卖出” are retained only as structured
        # source metadata.  Repeating them in the prose excerpt can be mistaken
        # for this system's own recommendation downstream.
        excerpt = f'{org}发布研报《{title}》（机构研究观点，仅作来源记录）'
        cards.append(EvidenceCard(
            source_type='research_report',
            title=title,
            url=url,
            publish_time=to_iso(row[date_col]) if date_col else '',
            source_name='东方财富研报中心',
            symbol=market,
            excerpt=truncate(excerpt, 800),
            structured={'rating': rating, 'org': org, 'degraded': False},
            reliability=4,
            fetch_tool='fetch_research_reports',
        ))
    return cards
