"""fetch_announcements — 巨潮公告（04§3.1）。"""

from __future__ import annotations

import re
from typing import List, Optional

import requests

from ._helpers import retry_call, run_with_timeout, to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard, reliability_for
from .symbol import normalize_symbol, to_market_symbol

CATEGORY_KEYWORDS = {
    '年报': ['年度报告', '年报'],
    '半年报': ['半年度报告', '半年报'],
    '一季报': ['一季度报告', '第一季度'],
    '三季报': ['三季度报告', '第三季度'],
    '业绩预告': ['业绩预告', '业绩快报', '业绩预增', '业绩预减'],
    '重大事项': ['重大事项', '重大合同', '重大资产'],
    '股权变动': ['权益变动', '股权变动', '增持', '减持', '回购'],
}


def _infer_category(title: str) -> str:
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in title for k in kws):
            return cat
    return '其他'


def _infer_event_type(title: str) -> str:
    """把公开公告标题映射到与 Datayes 一致的公司事件枚举。"""
    rules = (
        ('earnings_forecast', ('业绩预告', '业绩预增', '业绩预减')),
        ('earnings_flash', ('业绩快报',)),
        ('shareholder_change', ('增持', '减持', '权益变动', '股权变动')),
        ('buyback', ('回购',)),
        ('major_contract', ('重大合同',)),
        ('regulatory', ('监管函', '问询函', '监管')),
        ('violation', ('违规', '处罚')),
        ('lawsuit', ('诉讼', '仲裁')),
        ('board_resolution', ('董事会', '议案')),
        ('dividend', ('分红', '派息', '权益分派')),
    )
    for event_type, keywords in rules:
        if any(keyword in title for keyword in keywords):
            return event_type
    return 'announcement'


def _via_akshare(symbol: str, start_date: str, end_date: str) -> List[EvidenceCard]:
    import akshare as ak

    code = normalize_symbol(symbol)
    limiter.wait('cninfo')

    def _call():
        # 兼容不同 akshare 版本的接口名
        if hasattr(ak, 'stock_zh_a_disclosure_report_cninfo'):
            return ak.stock_zh_a_disclosure_report_cninfo(
                symbol=code, market='沪深京', start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
            )
        if hasattr(ak, 'stock_notice_report'):
            return ak.stock_notice_report(symbol=code, date=end_date.replace('-', ''))
        raise RuntimeError('akshare 无可用公告接口')

    df = retry_call(lambda: run_with_timeout(_call, 30))
    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    # 列名兼容
    cols = {c: c for c in df.columns}
    title_col = next((c for c in df.columns if '标题' in str(c) or 'title' in str(c).lower()), None)
    time_col = next((c for c in df.columns if '时间' in str(c) or '日期' in str(c) or 'date' in str(c).lower()), None)
    url_col = next((c for c in df.columns if '链接' in str(c) or 'url' in str(c).lower() or '网址' in str(c)), None)
    id_col = next((
        c for c in df.columns
        if str(c).lower().replace('_', '') in ('公告id', 'announcementid', 'annoid')
    ), None)

    for _, row in df.iterrows():
        title = str(row[title_col]) if title_col else str(row.iloc[0])
        pub = to_iso(row[time_col]) if time_col else ''
        url = str(row[url_col]) if url_col and row[url_col] == row[url_col] else f'http://www.cninfo.com.cn/new/disclosure'
        announcement_id = str(row[id_col]) if id_col and row[id_col] == row[id_col] else None
        if not announcement_id:
            match = re.search(r'(?i)(?:announcementId|annoID)=([^&#]+)', url)
            announcement_id = match.group(1) if match else None
        if not announcement_id:
            match = re.search(r'(?i)/(\d{6,})\.(?:pdf|html?)(?:[?#]|$)', url)
            announcement_id = match.group(1) if match else None
        cards.append(EvidenceCard(
            source_type='announcement',
            title=title,
            url=url,
            publish_time=pub,
            source_name='巨潮资讯网',
            symbol=to_market_symbol(code),
            excerpt=truncate(title, 200),
            structured={
                'category': _infer_category(title),
                'canonical_event_type': _infer_event_type(title),
                'announcement_id': announcement_id,
                'degraded': False,
            },
            reliability=5,
            fetch_tool='fetch_announcements',
        ))
    return cards


def _via_cninfo_http(symbol: str, start_date: str, end_date: str) -> List[EvidenceCard]:
    code = normalize_symbol(symbol)
    limiter.wait('cninfo')
    se_date = f"{start_date.replace('-', '')}~{end_date.replace('-', '')}"
    # 巨潮 stock 参数常需 orgId；简化用 keyword 搜代码
    url = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': 'http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search',
    }
    data = {
        'pageNum': '1',
        'pageSize': '30',
        'column': 'szse',
        'tabName': 'fulltext',
        'plate': '',
        'stock': '',
        'searchkey': code,
        'secid': '',
        'category': '',
        'trade': '',
        'seDate': se_date,
        'sortName': '',
        'sortType': '',
        'isHLtitle': 'true',
    }

    def _call():
        r = requests.post(url, data=data, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    payload = retry_call(lambda: run_with_timeout(_call, 30))
    anns = payload.get('announcements') or []
    cards: List[EvidenceCard] = []
    for a in anns:
        title = a.get('announcementTitle') or a.get('title') or ''
        adj_url = a.get('adjunctUrl') or ''
        full_url = f'http://static.cninfo.com.cn/{adj_url}' if adj_url else url
        ts = a.get('announcementTime') or a.get('announcementDate')
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone, timedelta
            pub = datetime.fromtimestamp(ts / 1000, tz=timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        else:
            pub = to_iso(ts)
        cards.append(EvidenceCard(
            source_type='announcement',
            title=title,
            url=full_url,
            publish_time=pub,
            source_name='巨潮资讯网',
            symbol=to_market_symbol(code),
            excerpt=truncate(title, 200),
            structured={
                'category': _infer_category(title),
                'canonical_event_type': _infer_event_type(title),
                'announcement_id': a.get('announcementId'),
                'degraded': True,
            },
            reliability=5,
            fetch_tool='fetch_announcements',
        ))
    return cards


def _fetch_announcements_public(
    symbol: str,
    start_date: str,
    end_date: str,
    category: str = 'all',
    max_count: int = 30,
) -> List[EvidenceCard]:
    degraded = False
    try:
        cards = _via_akshare(symbol, start_date, end_date)
    except Exception:
        cards = _via_cninfo_http(symbol, start_date, end_date)
        degraded = True

    if category and category != 'all':
        kws = CATEGORY_KEYWORDS.get(category, [category])
        cards = [c for c in cards if any(k in c.title for k in kws)]

    if degraded:
        for c in cards:
            c.structured['degraded'] = True

    return cards[:max_count]


DATAYES_EVENT_APIS = {
    'getFdmtEfNew': lambda code, start, end: {
        'ticker': code, 'publishDateBegin': start, 'publishDateEnd': end,
    },
    'getFdmtEe': lambda code, start, end: {
        'ticker': code, 'publishDateBegin': start, 'publishDateEnd': end,
    },
    'getEquChangePlan': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
        'publishDateBegin': start, 'publishDateEnd': end,
    },
    'getEquShareBuyback': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
        'beginPrePubDate': start, 'endPrePubDate': end,
    },
    'getEquMjrCntrPIT': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquRegulatory': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquCompIllegal': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquLawSuits': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquBoardPIT': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquBoardPubPIT': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
    },
    'getEquDivPIT': lambda code, start, end: {
        'ticker': code, 'beginDate': start, 'endDate': end,
        'beginPublishDate': start, 'endPublishDate': end,
    },
}


def fetch_announcements(
    symbol: str,
    start_date: str,
    end_date: str,
    category: str = 'all',
    max_count: int = 30,
) -> List[EvidenceCard]:
    """巨潮公告列表与 Datayes 结构化公司事件合并。"""
    from ..providers import get_provider_router
    from ..providers.datayes.mappers import (
        map_company_events,
        map_investor_research,
        mark_public_fallback,
        merge_announcement_cards,
    )

    router = get_provider_router()
    if not router.enabled:
        return _fetch_announcements_public(symbol, start_date, end_date, category, max_count)

    public_error = None
    try:
        public_cards = _fetch_announcements_public(symbol, start_date, end_date, 'all', max_count)
    except Exception as exc:
        public_error = exc
        public_cards = []

    code = normalize_symbol(symbol)
    datayes_cards: List[EvidenceCard] = []
    degradation_reasons: List[str] = []
    for api, build_params in DATAYES_EVENT_APIS.items():
        result = router.fetch(
            api,
            build_params(code, start_date.replace('-', ''), end_date.replace('-', '')),
            end_date=end_date,
            latest=True,
            limit=max(100, max_count * 5),
        )
        datayes_cards.extend(map_company_events(result, symbol))
        degradation_reasons.extend(result.degradation_reasons)

    investor_cards: List[EvidenceCard] = []
    if category in ('all', '机构调研', 'investor_research'):
        activity = router.fetch(
            'getEquIsActivity',
            {
                'ticker': code,
                'publishBeginDate': start_date.replace('-', ''),
                'publishEndDate': end_date.replace('-', ''),
            },
            end_date=end_date,
            latest=True,
            limit=max(20, max_count),
        )
        details = {}
        for row in activity.rows[:10]:
            event_id = row.get('event_id')
            if not event_id:
                continue
            detail = router.fetch(
                'getEquIsParticipantQa',
                {'eventID': event_id, 'ticker': code},
                latest=True,
                limit=500,
            )
            details[str(event_id)] = detail
            degradation_reasons.extend(detail.degradation_reasons)
        investor_cards = map_investor_research(activity, details, symbol)
        degradation_reasons.extend(activity.degradation_reasons)

    if degradation_reasons and public_cards:
        mark_public_fallback(public_cards, degradation_reasons)
    cards = merge_announcement_cards(public_cards, datayes_cards)
    cards.extend(investor_cards)
    cards.sort(key=lambda card: str(card.publish_time or ''), reverse=True)
    if public_error and cards:
        for card in cards:
            card.structured.setdefault('collection_integrity', {})['cninfo'] = {
                'ok': False, 'error_type': type(public_error).__name__,
            }
    if category and category != 'all':
        kws = CATEGORY_KEYWORDS.get(category, [category])
        cards = [card for card in cards if any(keyword in card.title for keyword in kws)]
    if not cards and public_error:
        raise public_error
    return cards[:max_count]
