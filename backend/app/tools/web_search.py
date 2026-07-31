"""博查联网搜索（04§3.8）— 谷歌搜索的大陆替代。"""

from __future__ import annotations

import threading
from typing import List, Optional

import httpx

from ..config import Config
from ._helpers import to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard

_task_counters = threading.local()


def _get_task_count() -> int:
    return getattr(_task_counters, 'count', 0)


def _inc_task_count() -> int:
    _task_counters.count = _get_task_count() + 1
    return _task_counters.count


def reset_web_search_budget() -> None:
    _task_counters.count = 0


def web_search(
    query: str,
    freshness: str = 'oneMonth',
    count: int = 8,
    site: Optional[str] = None,
) -> List[EvidenceCard]:
    if _get_task_count() >= 10:
        return [EvidenceCard(
            source_type='web_search',
            title='web_search 次数已达上限',
            url='',
            publish_time='',
            source_name='博查搜索',
            excerpt='单次任务 web_search 调用上限 10 次，请改用免费数据源。',
            structured={'search_unavailable': True, 'reason': 'budget_exceeded'},
            reliability=1,
            fetch_tool='web_search',
        )]

    if not Config.BOCHA_API_KEY:
        return [EvidenceCard(
            source_type='web_search',
            title='BOCHA_API_KEY 未配置',
            url='',
            publish_time='',
            source_name='博查搜索',
            excerpt='请在 .env 配置 BOCHA_API_KEY；本次跳过联网搜索。',
            structured={'search_unavailable': True, 'reason': 'no_api_key'},
            reliability=1,
            fetch_tool='web_search',
        )]

    q = f'site:{site} {query}' if site else query
    limiter.wait('bocha')
    _inc_task_count()

    try:
        resp = httpx.post(
            'https://api.bochaai.com/v1/web-search',
            headers={
                'Authorization': f'Bearer {Config.BOCHA_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={'query': q, 'freshness': freshness, 'summary': True, 'count': count},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return [EvidenceCard(
            source_type='web_search',
            title='博查搜索不可用',
            url='',
            publish_time='',
            source_name='博查搜索',
            excerpt=str(e),
            structured={'search_unavailable': True, 'error': str(e)},
            reliability=1,
            fetch_tool='web_search',
        )]

    pages = (
        payload.get('data', {}).get('webPages', {}).get('value')
        or payload.get('webPages', {}).get('value')
        or []
    )
    cards: List[EvidenceCard] = []
    for p in pages:
        cards.append(EvidenceCard(
            source_type='web_search',
            title=p.get('name') or p.get('title') or '',
            url=p.get('url') or '',
            publish_time=to_iso(p.get('datePublished') or p.get('dateLastCrawled') or ''),
            source_name=p.get('siteName') or '博查搜索',
            excerpt=truncate(p.get('summary') or p.get('snippet') or '', 800),
            structured={'degraded': False},
            reliability=3,
            fetch_tool='web_search',
        ))
    return cards
