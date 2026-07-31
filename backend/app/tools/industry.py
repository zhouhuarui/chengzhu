"""行业与宏观数据（04§3.7）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from ._helpers import retry_call, run_with_timeout, to_iso, truncate
from .rate_limiter import limiter
from .schema import EvidenceCard
from .symbol import normalize_symbol

MACRO_MAP = {
    'cpi': 'macro_china_cpi_yearly',
    'ppi': 'macro_china_ppi_yearly',
    'pmi': 'macro_china_pmi_yearly',
    'gdp': 'macro_china_gdp_yearly',
    'lpr': 'macro_china_lpr',
    '社融': 'macro_china_shrzgm',
}


def _fetch_industry_data_public(
    industry: Optional[str] = None,
    macro_indicators: Optional[List[str]] = None,
    symbol: Optional[str] = None,
) -> List[EvidenceCard]:
    import akshare as ak

    cards: List[EvidenceCard] = []
    limiter.wait('eastmoney')

    # 若给了 symbol，先取所属行业
    if symbol and not industry:
        code = normalize_symbol(symbol)

        def _info():
            return ak.stock_individual_info_em(symbol=code)

        try:
            info_df = retry_call(lambda: run_with_timeout(_info, 20))
            cols = list(info_df.columns)
            for _, row in info_df.iterrows():
                if str(row[cols[0]]) == '行业':
                    industry = str(row[cols[1]])
                    break
        except Exception:
            pass

    if industry:
        limiter.wait('eastmoney')

        def _hist():
            return ak.stock_board_industry_hist_em(
                symbol=industry, period='日k', start_date='20240101',
                end_date='', adjust='',
            )

        try:
            df = retry_call(lambda: run_with_timeout(_hist, 30))
            series = []
            if df is not None and not getattr(df, 'empty', True):
                date_col = next((c for c in df.columns if '日期' in str(c)), None)
                close_col = next((c for c in df.columns if '收盘' in str(c)), None)
                for _, row in df.tail(30).iterrows():
                    series.append({
                        'date': str(row[date_col]) if date_col else '',
                        'close': row[close_col] if close_col else None,
                    })
            cards.append(EvidenceCard(
                source_type='industry_data',
                title=f'行业板块行情：{industry}',
                url='https://data.eastmoney.com/bkzj/hy.html',
                publish_time=to_iso(series[-1]['date']) if series else '',
                source_name='东方财富',
                symbol=None,
                excerpt=truncate(f'{industry} 近30日收盘序列：{series[-5:]}', 800),
                structured={'industry': industry, 'series': series, 'degraded': False},
                reliability=4,
                fetch_tool='fetch_industry_data',
            ))
        except Exception as e:
            cards.append(EvidenceCard(
                source_type='industry_data',
                title=f'行业数据获取失败：{industry}',
                url='https://data.eastmoney.com/bkzj/hy.html',
                publish_time='',
                source_name='东方财富',
                excerpt=str(e),
                structured={'industry': industry, 'degraded': True, 'error': str(e)},
                reliability=3,
                fetch_tool='fetch_industry_data',
            ))

    for key in (macro_indicators or []):
        fn_name = MACRO_MAP.get(key)
        if not fn_name:
            continue
        limiter.wait('eastmoney')

        def _macro(name=fn_name):
            return getattr(ak, name)()

        try:
            df = retry_call(lambda: run_with_timeout(_macro, 30))
            series = []
            if df is not None and not getattr(df, 'empty', True):
                for _, row in df.tail(12).iterrows():
                    series.append({k: (None if v != v else v) for k, v in row.to_dict().items()})
            cards.append(EvidenceCard(
                source_type='industry_data',
                title=f'宏观指标：{key}',
                url='https://data.eastmoney.com/cjsj/',
                publish_time='',
                source_name='东方财富/国家统计局公开数据',
                excerpt=truncate(str(series[-3:]), 800),
                structured={'indicator': key, 'series': series, 'degraded': False},
                reliability=5,
                fetch_tool='fetch_industry_data',
            ))
        except Exception as e:
            cards.append(EvidenceCard(
                source_type='industry_data',
                title=f'宏观指标失败：{key}',
                url='https://data.eastmoney.com/cjsj/',
                publish_time='',
                source_name='东方财富',
                excerpt=str(e),
                structured={'indicator': key, 'degraded': True, 'error': str(e)},
                reliability=3,
                fetch_tool='fetch_industry_data',
            ))

    return cards


def fetch_industry_data(
    industry: Optional[str] = None,
    macro_indicators: Optional[List[str]] = None,
    symbol: Optional[str] = None,
) -> List[EvidenceCard]:
    """Datayes 提供行业结构化数据，宏观指标继续使用原公开数据源。"""
    from ..providers import ProviderResult, get_provider_router
    from ..providers.datayes.mappers import map_industry_card, mark_public_fallback

    router = get_provider_router()
    if not router.enabled or (not symbol and not industry):
        return _fetch_industry_data_public(industry, macro_indicators, symbol)

    if symbol:
        membership = router.fetch(
            'getEquIndustry',
            {
                'ticker': normalize_symbol(symbol),
                'industryVersionCD': '010303',
            },
            latest=True,
            limit=100,
        )
    else:
        membership = router.fetch(
            'getIndustry',
            {
                'industryVersionCD': '010303',
                'industryName': industry,
                'isNew': 1,
            },
            latest=True,
            limit=100,
        )

    membership_row = max(
        membership.rows,
        key=lambda row: (int(row.get('is_new') or 0), str(row.get('into_date') or row.get('begin_date') or '')),
        default={},
    )
    industry_id = (
        membership_row.get('industry_id3') or membership_row.get('industry_id2')
        or membership_row.get('industry_id')
    )
    end = date.today()
    begin = end - timedelta(days=120)

    def _empty(api: str, reason: str) -> ProviderResult:
        return ProviderResult(
            rows=[], provider='public_fallback', api=api, degraded=True,
            degradation_reasons=[reason], license_scope=router.license_mode,
        )

    if industry_id:
        common = {
            'industryID': industry_id,
            'beginDate': begin.strftime('%Y%m%d'),
            'endDate': end.strftime('%Y%m%d'),
        }
        quote = router.fetch('getMktInstEqudV1', common, latest=True, limit=200)
        valuation = router.fetch('getMktIndustryEvalV1', common, latest=True, limit=200)
        flow = router.fetch('getMktIndustryFlowV1', common, latest=True, limit=200)
    else:
        quote = _empty('getMktInstEqudV1', 'industry_id_unresolved')
        valuation = _empty('getMktIndustryEvalV1', 'industry_id_unresolved')
        flow = _empty('getMktIndustryFlowV1', 'industry_id_unresolved')

    cards = map_industry_card(
        symbol=symbol,
        requested_industry=industry,
        membership=membership,
        quote=quote,
        valuation=valuation,
        flow=flow,
    )
    # 宏观数据明确不迁移到 Datayes。
    macro_cards = _fetch_industry_data_public(None, macro_indicators, None)
    if cards:
        return cards + macro_cards
    fallback = _fetch_industry_data_public(industry, macro_indicators, symbol)
    reasons = (
        membership.degradation_reasons + quote.degradation_reasons
        + valuation.degradation_reasons + flow.degradation_reasons
    )
    return mark_public_fallback(fallback, reasons or ['datayes_no_rows'])
