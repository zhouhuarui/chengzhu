"""行情与估值快照（04§3.9）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from ._helpers import retry_call, run_with_timeout, safe_float, to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard
from .symbol import normalize_symbol, to_market_symbol


def _fetch_stock_quote_public(symbol: str, days: int = 90) -> List[EvidenceCard]:
    import akshare as ak

    code = normalize_symbol(symbol)
    market = to_market_symbol(symbol)
    degraded = False
    info = {}
    hist = None
    source_name = '东方财富'

    try:
        limiter.wait('eastmoney')

        def _info():
            return ak.stock_individual_info_em(symbol=code)

        info_df = retry_call(lambda: run_with_timeout(_info, 20), retries=2)
        if info_df is not None and not getattr(info_df, 'empty', True):
            cols = list(info_df.columns)
            if len(cols) >= 2:
                for _, row in info_df.iterrows():
                    info[str(row[cols[0]])] = row[cols[1]]

        end = datetime.now()
        start = end - timedelta(days=days)
        limiter.wait('eastmoney')

        def _hist():
            return ak.stock_zh_a_hist(
                symbol=code,
                period='daily',
                start_date=start.strftime('%Y%m%d'),
                end_date=end.strftime('%Y%m%d'),
                adjust='qfq',
            )

        hist = retry_call(lambda: run_with_timeout(_hist, 30), retries=2)
    except Exception:
        degraded = True
        source_name = '新浪财经'
        limiter.wait('sina')
        prefix = 'sh' if market.endswith('.SH') else ('bj' if market.endswith('.BJ') else 'sz')

        def _sina():
            return ak.stock_zh_a_daily(symbol=f'{prefix}{code}', adjust='qfq')

        hist = retry_call(lambda: run_with_timeout(_sina, 30), retries=2)

    latest_close = None
    range_pct = None
    series = []
    if hist is not None and not getattr(hist, 'empty', True):
        # 只取最近 days
        if len(hist) > days:
            hist = hist.tail(days)
        close_col = next((c for c in hist.columns if '收盘' in str(c) or str(c).lower() == 'close'), None)
        date_col = next((c for c in hist.columns if '日期' in str(c) or str(c).lower() == 'date'), None)
        if close_col is None and 'close' in [str(c).lower() for c in hist.columns]:
            close_col = [c for c in hist.columns if str(c).lower() == 'close'][0]
        if close_col:
            closes = [safe_float(v) for v in hist[close_col].tolist()]
            closes = [c for c in closes if c is not None]
            if closes:
                latest_close = closes[-1]
                if closes[0]:
                    range_pct = round((closes[-1] / closes[0] - 1) * 100, 2)
            step = max(1, len(hist) // 12)
            for i in range(0, len(hist), step):
                row = hist.iloc[i]
                series.append({
                    'date': str(row[date_col]) if date_col else '',
                    'close': safe_float(row[close_col]),
                })

    structured = {
        'latest_close': latest_close,
        'range_pct': range_pct,
        'market_cap': info.get('总市值') or info.get('市值'),
        'industry': info.get('行业'),
        'series': series,
        'degraded': degraded,
    }
    excerpt = (
        f'{market} 近{days}日：最新收盘 {latest_close}，区间涨跌幅 {range_pct}%；'
        f"行业 {structured.get('industry')}；总市值 {structured.get('market_cap')}"
    )
    return [EvidenceCard(
        source_type='industry_data',
        title=f'{market} 行情快照',
        url=f'https://quote.eastmoney.com/{market.replace(".", "").lower()}.html',
        publish_time=to_iso(datetime.now()),
        source_name=source_name,
        symbol=market,
        excerpt=truncate(excerpt, 800),
        structured=structured,
        reliability=4,
        fetch_tool='fetch_stock_quote',
    )]


def fetch_stock_quote(symbol: str, days: int = 90) -> List[EvidenceCard]:
    """Datayes 未复权行情 + 后复权收益 + 独立估值，公开源作降级。"""
    from ..providers import get_provider_router
    from ..providers.datayes.mappers import map_stock_quote, mark_public_fallback

    router = get_provider_router()
    if not router.enabled:
        return _fetch_stock_quote_public(symbol, days)

    code = normalize_symbol(symbol)
    end = datetime.now().date()
    # days 是交易日展示上限，查询窗按约 1.7 倍自然日覆盖节假日。
    begin = end - timedelta(days=max(30, int(days * 1.7)))
    params = {
        'ticker': code,
        'beginDate': begin.strftime('%Y%m%d'),
        'endDate': end.strftime('%Y%m%d'),
    }
    quote_result = router.fetch(
        'getMktEqud', params, end_date=end.isoformat(), latest=True, limit=max(200, days * 3)
    )
    factor_result = router.fetch(
        'getMktAdjfAf',
        {'ticker': code, 'beginDate': '19900101', 'endDate': end.strftime('%Y%m%d')},
        end_date=end.isoformat(), latest=True, limit=1000
    )
    valuation_result = router.fetch(
        'getMktEqudEvalNew', params, end_date=end.isoformat(), latest=True, limit=max(200, days * 3)
    )
    cards = map_stock_quote(symbol, days, quote_result, factor_result, valuation_result)
    if cards:
        return cards
    fallback = _fetch_stock_quote_public(symbol, days)
    reasons = (
        quote_result.degradation_reasons
        + factor_result.degradation_reasons
        + valuation_result.degradation_reasons
    )
    return mark_public_fallback(fallback, reasons or ['datayes_no_rows'])
