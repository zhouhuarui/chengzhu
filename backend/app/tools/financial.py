"""财报三大表 + 财务指标（04§3.2-3.3）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ._helpers import retry_call, run_with_timeout, safe_float, to_iso, truncate, yi_yuan
from .rate_limiter import limiter
from .schema import EvidenceCard
from .symbol import normalize_symbol, to_em_symbol, to_market_symbol

INCOME_KEYS = [
    'REPORT_DATE', 'TOTAL_OPERATE_INCOME', 'OPERATE_PROFIT', 'TOTAL_PROFIT',
    'NETPROFIT', 'PARENT_NETPROFIT', 'BASIC_EPS',
]
BALANCE_KEYS = [
    'REPORT_DATE', 'TOTAL_ASSETS', 'TOTAL_LIABILITIES', 'TOTAL_EQUITY',
    'MONETARYFUNDS', 'INVENTORY', 'ACCOUNTS_RECE',
]
CASHFLOW_KEYS = [
    'REPORT_DATE', 'NETCASH_OPERATE', 'NETCASH_INVEST', 'NETCASH_FINANCE', 'END_CASH',
]

# ``REPORT_DATE`` is the accounting period end, not the date on which the
# information became public.  Only fields that explicitly describe an
# announcement/publication time may be used as EvidenceCard.publish_time.
REPORT_DATE_KEYS = ('REPORT_DATE', '报告日', '报告期', '日期')
DISCLOSURE_DATE_KEYS = (
    'NOTICE_DATE', 'PUBLISH_DATE', 'ANNOUNCEMENT_DATE', 'ANN_DATE',
    'DISCLOSURE_DATE', 'ACT_PUBTIME', '公告日期', '披露日期', '发布日期',
)
CURRENCY_KEYS = ('CURRENCY', 'CURRENCY_UNIT', 'RCURRENCY', '币种')
REPORT_TYPE_KEYS = ('REPORT_TYPE', 'RTYPE', '类型')
STATEMENT_META_KEYS = list(dict.fromkeys(
    REPORT_DATE_KEYS + DISCLOSURE_DATE_KEYS + CURRENCY_KEYS + REPORT_TYPE_KEYS
))

INDICATOR_WHITELIST = [
    '日期', '加权净资产收益率', '销售毛利率', '销售净利率', '资产负债率',
    '每股收益', '每股净资产', '存货周转率', '应收账款周转率',
]


def _pick(row: Any, keys: List[str]) -> Dict[str, Any]:
    out = {}
    cols = {str(c).upper(): c for c in getattr(row, 'index', [])}
    for k in keys:
        col = cols.get(k.upper())
        if col is not None:
            out[k] = row[col]
        else:
            # 模糊匹配
            for c in getattr(row, 'index', []):
                if k.lower() in str(c).lower() or str(c).upper() == k.upper():
                    out[k] = row[c]
                    break
    return out


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() in {'nan', 'nat', '<na>', 'none', 'null', '--'}:
        return None
    return value


def _first_present(data: Dict[str, Any], keys: Any) -> Any:
    upper = {str(key).upper(): value for key, value in data.items()}
    for key in keys:
        value = _clean_scalar(upper.get(str(key).upper()))
        if value is not None:
            return value
    return None


def _period_type(report_date: Any) -> str:
    day = to_iso(report_date)[:10]
    return {
        '03-31': 'Q1',
        '06-30': 'H1',
        '09-30': 'Q3',
        '12-31': 'FY',
    }.get(day[5:] if len(day) == 10 else '', 'unknown')


def _period_basis(statement: str) -> str:
    return 'point_in_time' if statement == 'balance' else 'cumulative'


def _normalise_currency(value: Any) -> str:
    text = str(_clean_scalar(value) or '').strip().upper()
    aliases = {
        '人民币': 'CNY', '人民币元': 'CNY', '元': 'CNY', 'RMB': 'CNY', 'CNY': 'CNY',
        '美元': 'USD', 'USD': 'USD',
        '港币': 'HKD', '港元': 'HKD', 'HKD': 'HKD',
    }
    if text in aliases:
        return aliases[text]
    if '人民币' in text or 'RMB' in text or 'CNY' in text:
        return 'CNY'
    if '美元' in text or 'USD' in text:
        return 'USD'
    if '港币' in text or '港元' in text or 'HKD' in text:
        return 'HKD'
    return text


def _scope_from_report_type(value: Any) -> str:
    text = str(_clean_scalar(value) or '').strip().lower()
    if not text:
        return 'unknown'
    if '合并' in text or 'consolidated' in text or text == 'merged':
        return 'consolidated'
    if '母公司' in text or text in {'parent', 'parent_company'}:
        return 'parent_company'
    return 'unknown'


def _income_excerpt(d: Dict[str, Any]) -> str:
    date = to_iso(d.get('REPORT_DATE'))[:10] or '未知报告期'
    rev = yi_yuan(d.get('TOTAL_OPERATE_INCOME'))
    np_ = yi_yuan(d.get('PARENT_NETPROFIT') or d.get('NETPROFIT'))
    eps = safe_float(d.get('BASIC_EPS'))
    parts = [f'{date} 利润表：']
    if rev is not None:
        parts.append(f'营业总收入 {rev} 亿元')
    if np_ is not None:
        parts.append(f'归母净利润 {np_} 亿元')
    if eps is not None:
        parts.append(f'基本每股收益 {eps}')
    return truncate('，'.join(parts) if len(parts) > 1 else parts[0], 800)


def _fetch_financial_statements_public(
    symbol: str,
    statement: str = 'income',
    period_count: int = 8,
) -> List[EvidenceCard]:
    import akshare as ak

    em = to_em_symbol(symbol)
    limiter.wait('eastmoney')

    fn_map = {
        'income': ('stock_profit_sheet_by_report_em', INCOME_KEYS, '利润表'),
        'balance': ('stock_balance_sheet_by_report_em', BALANCE_KEYS, '资产负债表'),
        'cashflow': ('stock_cash_flow_sheet_by_report_em', CASHFLOW_KEYS, '现金流量表'),
    }
    if statement not in fn_map:
        raise ValueError(f'unknown statement: {statement}')
    fn_name, keys, label = fn_map[statement]
    degraded = False

    def _em():
        fn = getattr(ak, fn_name)
        return fn(symbol=em)

    try:
        df = retry_call(lambda: run_with_timeout(_em, 45))
    except Exception:
        # 新浪降级
        degraded = True
        limiter.wait('sina')
        sina_map = {'income': '利润表', 'balance': '资产负债表', 'cashflow': '现金流量表'}
        code = normalize_symbol(symbol)
        prefix = 'sh' if em.startswith('SH') else ('bj' if em.startswith('BJ') else 'sz')

        def _sina():
            return ak.stock_financial_report_sina(stock=f'{prefix}{code}', symbol=sina_map[statement])

        df = retry_call(lambda: run_with_timeout(_sina, 45))

    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    rows = list(df.iterrows())[:period_count]
    for _, row in rows:
        data = _pick(row, keys)
        if not data and hasattr(row, 'to_dict'):
            # 降级：取前若干非空字段
            raw = {
                str(k): v for k, v in row.to_dict().items()
                if _clean_scalar(v) is not None
            }
            data = dict(list(raw.items())[:12])
        # Metadata is collected separately so a Sina row containing only
        # Chinese financial item names still retains its source fields.
        data.update(_pick(row, STATEMENT_META_KEYS))
        report_date = _first_present(data, REPORT_DATE_KEYS)
        if report_date is not None:
            data['REPORT_DATE'] = report_date
        publish_date = _first_present(data, DISCLOSURE_DATE_KEYS)
        pub = to_iso(publish_date)
        report_period = to_iso(report_date)
        structured = {k: (None if (isinstance(v, float) and v != v) else v) for k, v in data.items()}
        structured['statement'] = statement
        structured['degraded'] = degraded
        structured['report_period'] = report_period
        structured['publish_date'] = pub
        structured['period_type'] = _period_type(report_date)
        structured['accumulation_basis'] = _period_basis(statement)

        if not degraded:
            # AkShare's Eastmoney "by report" endpoints request reportType=1,
            # which is the consolidated statement.  Make that otherwise
            # implicit API contract auditable by downstream normalisation.
            structured.update({
                'report_type': 1,
                'source_report_type': 1,
                'consolidation_scope': 'consolidated',
                'statement_scope': 'consolidated',
                'merged_flag': 1,
                'currency': 'CNY',
                'currency_unit': 'CNY',
                'amount_unit': 'CNY',
            })
        else:
            source_currency = _first_present(data, CURRENCY_KEYS)
            source_report_type = _first_present(data, REPORT_TYPE_KEYS)
            currency = _normalise_currency(source_currency)
            scope = _scope_from_report_type(source_report_type)
            structured.update({
                'report_type': source_report_type or 'unknown',
                'source_report_type': source_report_type or 'unknown',
                'consolidation_scope': scope,
                'statement_scope': scope,
                'merged_flag': 1 if scope == 'consolidated' else (
                    0 if scope == 'parent_company' else None
                ),
                'currency': currency,
                'currency_unit': currency,
            })
            if currency:
                structured['amount_unit'] = currency
        # 金额字段转亿元副本
        for k in list(structured.keys()):
            if k.endswith('INCOME') or k.endswith('PROFIT') or k in (
                'TOTAL_ASSETS', 'TOTAL_LIABILITIES', 'TOTAL_EQUITY',
                'MONETARYFUNDS', 'INVENTORY', 'ACCOUNTS_RECE',
                'NETCASH_OPERATE', 'NETCASH_INVEST', 'NETCASH_FINANCE', 'END_CASH',
            ):
                y = yi_yuan(structured.get(k))
                if y is not None:
                    structured[f'{k}_yi'] = y

        excerpt = _income_excerpt(data) if statement == 'income' else truncate(
            f"{report_period[:10] or '报告期'} {label}要点：{structured}", 800
        )
        cards.append(EvidenceCard(
            source_type='financial_report',
            title=f'{normalize_symbol(symbol)} {label} {report_period[:10]}',
            url=f'https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code={em}&color=b#/cwfx',
            publish_time=pub,
            source_name='东方财富' if not degraded else '新浪财经',
            symbol=normalize_symbol(symbol),
            excerpt=excerpt,
            structured=structured,
            reliability=5,
            fetch_tool='fetch_financial_statements',
        ))
    return cards


def _fetch_financial_indicators_public(symbol: str, start_year: Optional[str] = None) -> List[EvidenceCard]:
    import akshare as ak

    code = normalize_symbol(symbol)
    market = to_market_symbol(symbol)
    limiter.wait('sina')

    def _call():
        kwargs = {'symbol': code}
        if start_year:
            kwargs['start_year'] = start_year
        return ak.stock_financial_analysis_indicator(**kwargs)

    df = retry_call(lambda: run_with_timeout(_call, 45))
    cards: List[EvidenceCard] = []
    if df is None or getattr(df, 'empty', True):
        return cards

    for _, row in df.iterrows():
        structured = {}
        for col in df.columns:
            name = str(col)
            if any(w in name for w in INDICATOR_WHITELIST) or name in INDICATOR_WHITELIST:
                structured[name] = row[col]
        if not structured:
            structured = {str(k): row[k] for k in list(df.columns)[:10]}
        raw = row.to_dict() if hasattr(row, 'to_dict') else {}
        report_period = to_iso(
            structured.get('日期') or structured.get('报告期') or row.iloc[0]
        )
        # The public Sina indicator table normally exposes only an accounting
        # period.  Do not manufacture a disclosure timestamp from it.
        pub = to_iso(_first_present(raw, DISCLOSURE_DATE_KEYS))
        structured.update({
            'report_period': report_period,
            'publish_date': pub,
            'consolidation_scope': 'unknown',
            'accumulation_basis': 'unknown',
        })
        cards.append(EvidenceCard(
            source_type='financial_report',
            title=f'{market} 财务指标 {report_period[:10]}',
            url=f'https://finance.sina.com.cn/realstock/company/{to_em_symbol(code).lower()}/nc.shtml',
            publish_time=pub,
            source_name='新浪财经',
            symbol=market,
            excerpt=truncate(str(structured), 800),
            structured={**structured, 'degraded': False},
            reliability=5,
            fetch_tool='fetch_financial_indicators',
        ))
    return cards[:20]


def fetch_financial_statements(
    symbol: str,
    statement: str = 'income',
    period_count: int = 8,
) -> List[EvidenceCard]:
    """优先 Datayes PIT 仓库/API，失败时保持原公开数据源行为。"""
    if statement not in ('income', 'balance', 'cashflow'):
        raise ValueError(f'unknown statement: {statement}')
    from ..providers import get_provider_router
    from ..providers.datayes.mappers import map_financial_statements, mark_public_fallback

    router = get_provider_router()
    if not router.enabled:
        return _fetch_financial_statements_public(symbol, statement, period_count)

    api = {'income': 'getFdmtIS', 'balance': 'getFdmtBS', 'cashflow': 'getFdmtCF'}[statement]
    end = date.today()
    begin = end - timedelta(days=max(730, int(period_count) * 150))
    result = router.fetch(
        api,
        {
            'ticker': normalize_symbol(symbol),
            'beginDate': begin.strftime('%Y%m%d'),
            'endDate': end.strftime('%Y%m%d'),
        },
        end_date=end.isoformat(),
        latest=True,
        limit=max(100, int(period_count) * 20),
    )
    cards = map_financial_statements(result, symbol, statement, period_count)
    if cards:
        return cards
    fallback = _fetch_financial_statements_public(symbol, statement, period_count)
    return mark_public_fallback(fallback, result.degradation_reasons or ['datayes_no_rows'])


def fetch_financial_indicators(symbol: str, start_year: Optional[str] = None) -> List[EvidenceCard]:
    """Datayes PIT 累计/单季指标合并；公开新浪接口仅作降级。"""
    from ..providers import get_provider_router
    from ..providers.datayes.mappers import map_financial_indicators, mark_public_fallback

    router = get_provider_router()
    if not router.enabled:
        return _fetch_financial_indicators_public(symbol, start_year)

    today = date.today()
    try:
        begin_year = int(start_year) if start_year else today.year - 5
    except (TypeError, ValueError):
        begin_year = today.year - 5
    params = {
        'ticker': normalize_symbol(symbol),
        'beginDate': f'{begin_year}0101',
        'endDate': today.strftime('%Y%m%d'),
        'beginYear': str(begin_year),
        'endYear': str(today.year),
    }
    pit = router.fetch('getFdmtMainDataPIT', params, latest=True, limit=100)
    qpit = router.fetch('getFdmtMainDataQPIT', params, latest=True, limit=100)
    cards = map_financial_indicators(pit, symbol, max_count=12)
    cards.extend(map_financial_indicators(qpit, symbol, max_count=8))
    if cards:
        return cards[:20]
    fallback = _fetch_financial_indicators_public(symbol, start_year)
    reasons = pit.degradation_reasons + qpit.degradation_reasons
    return mark_public_fallback(fallback, reasons or ['datayes_no_rows'])
