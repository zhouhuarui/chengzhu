"""将 Datayes 统一行映射为 Chengzhu EvidenceCard。"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...tools._helpers import safe_float, truncate, yi_yuan
from ...tools.schema import EvidenceCard
from ...tools.symbol import normalize_symbol, to_market_symbol
from ..router import ProviderResult
from .manifest import get_endpoint
from .normalization import business_key, record_key, row_fingerprint


UPSTREAM = {
    'getMktEqud': '上海证券交易所/深圳证券交易所/北京证券交易所',
    'getMktEqudEvalNew': '通联数据计算',
    'getMktAdjfAf': '交易所及上市公司披露',
    'getFdmtIS': '上市公司定期报告',
    'getFdmtBS': '上市公司定期报告',
    'getFdmtCF': '上市公司定期报告',
    'getFdmtMainDataPIT': '通联数据根据上市公司披露计算',
    'getFdmtMainDataQPIT': '通联数据根据上市公司披露计算',
}


def _make_card(*, provenance: Optional[Dict[str, Any]] = None, **kwargs: Any) -> EvidenceCard:
    # 在 EvidenceCard schema 升级合并前后均可运行；升级后 provenance 是正式顶层字段。
    if 'provenance' in getattr(EvidenceCard, '__dataclass_fields__', {}):
        kwargs['provenance'] = provenance
    elif provenance:
        kwargs.setdefault('structured', {})['provenance'] = provenance
    return EvidenceCard(**kwargs)


def _date(value: Any) -> str:
    return str(value or '')[:10]


def _current_rows(result: ProviderResult, *, merged_only: bool = False) -> List[Dict[str, Any]]:
    spec = get_endpoint(result.api)
    version_fields = spec.version_order_fields or (
        'update_time', 'act_pubtime', 'new_pub_date', 'publish_date'
    )

    def version_rank(row: Mapping[str, Any]) -> Tuple[str, ...]:
        return tuple(str(row.get(field) or '') for field in version_fields)

    # 先在同一业务键内按契约选择当前版本，再按业务日期排列输出；业务日期
    # 不能凌驾于 update_time 等版本字段之上。
    selected: Dict[str, Dict[str, Any]] = {}
    for row in result.rows:
        if merged_only and row.get('merged_flag') not in (None, '', '1', 1):
            continue
        key = business_key(spec, row)
        previous = selected.get(key)
        if previous is None or version_rank(row) > version_rank(previous):
            selected[key] = row
    return sorted(
        selected.values(),
        key=lambda row: (
            _date(
                row.get('end_date') or row.get('trade_date')
                or row.get('publish_date') or row.get('new_pub_date')
            ),
            version_rank(row),
        ),
        reverse=True,
    )


STATEMENT_ALIASES = {
    'income': {
        'REPORT_DATE': 'end_date', 'TOTAL_OPERATE_INCOME': 't_revenue',
        'OPERATE_INCOME': 'revenue', 'OPERATE_PROFIT': 'operate_profit',
        'TOTAL_PROFIT': 't_profit', 'NETPROFIT': 'n_income',
        'PARENT_NETPROFIT': 'n_income_attr_p', 'BASIC_EPS': 'basic_eps',
    },
    'balance': {
        'REPORT_DATE': 'end_date', 'TOTAL_ASSETS': 't_assets',
        'TOTAL_LIABILITIES': 't_liab', 'TOTAL_EQUITY': 't_sh_equity',
        'PARENT_EQUITY': 't_equity_attr_p', 'MONETARYFUNDS': 'cash_c_equiv',
        'INVENTORY': 'inventories', 'ACCOUNTS_RECE': 'ar',
    },
    'cashflow': {
        'REPORT_DATE': 'end_date', 'CASH_IN_OPERATE': 'c_inf_fr_operate_a',
        'CASH_OUT_OPERATE': 'c_outf_operate_a', 'NETCASH_OPERATE': 'ncf_operate_a',
        'NETCASH_INVEST': 'ncf_fr_invest_a', 'NETCASH_FINANCE': 'ncf_fr_finan_a',
        'NET_CHANGE_CASH': 'n_change_in_cash', 'END_CASH': 'nce_end_bal',
    },
}

AMOUNT_ALIASES = {
    'TOTAL_OPERATE_INCOME', 'OPERATE_INCOME', 'OPERATE_PROFIT', 'TOTAL_PROFIT',
    'NETPROFIT', 'PARENT_NETPROFIT', 'TOTAL_ASSETS', 'TOTAL_LIABILITIES',
    'TOTAL_EQUITY', 'PARENT_EQUITY', 'MONETARYFUNDS', 'INVENTORY',
    'ACCOUNTS_RECE', 'CASH_IN_OPERATE', 'CASH_OUT_OPERATE', 'NETCASH_OPERATE',
    'NETCASH_INVEST', 'NETCASH_FINANCE', 'NET_CHANGE_CASH', 'END_CASH',
}


def map_financial_statements(
    result: ProviderResult,
    symbol: str,
    statement: str,
    period_count: int,
) -> List[EvidenceCard]:
    labels = {'income': '利润表', 'balance': '资产负债表', 'cashflow': '现金流量表'}
    aliases = STATEMENT_ALIASES[statement]
    cards = []
    all_current = _current_rows(result, merged_only=False)
    parent_rows = {
        (row.get('end_date'), row.get('report_type')): row
        for row in all_current
        if row.get('merged_flag') not in (None, '', '1', 1)
    }
    for row in _current_rows(result, merged_only=True)[:max(1, int(period_count))]:
        structured: Dict[str, Any] = {}
        for alias, field in aliases.items():
            structured[alias] = row.get(field)
            if alias in AMOUNT_ALIASES:
                value = yi_yuan(row.get(field))
                if value is not None:
                    structured[f'{alias}_yi'] = value
        structured.update({
            'statement': statement,
            'report_period': row.get('end_date'),
            'report_type': row.get('report_type'),
            'merged_flag': row.get('merged_flag'),
            'publish_date': row.get('publish_date'),
            'update_time': row.get('update_time'),
            'currency_unit': row.get('currency_cd') or 'CNY',
            'amount_unit': row.get('currency_cd') or 'CNY',
            'display_amount_unit': f'{row.get("currency_cd") or "CNY"}_100m',
            'degraded': result.degraded,
            'degradation_reasons': result.degradation_reasons,
        })
        parent = parent_rows.get((row.get('end_date'), row.get('report_type')))
        if parent:
            structured['parent_company_statement'] = {
                alias: parent.get(field) for alias, field in aliases.items()
            }
            structured['parent_company_statement'].update({
                'report_period': parent.get('end_date'),
                'report_type': parent.get('report_type'),
                'merged_flag': parent.get('merged_flag'),
                'publish_date': parent.get('publish_date'),
                'update_time': parent.get('update_time'),
            })
            structured['parent_company_provenance'] = result.provenance_for(
                parent, upstream_source=UPSTREAM.get(result.api)
            )
        period = _date(row.get('end_date'))
        if statement == 'income':
            excerpt = (
                f"{period or '未知报告期'} 利润表：营业总收入 "
                f"{structured.get('TOTAL_OPERATE_INCOME_yi')} 亿元，归母净利润 "
                f"{structured.get('PARENT_NETPROFIT_yi')} 亿元，基本每股收益 "
                f"{structured.get('BASIC_EPS')}"
            )
        else:
            excerpt = f"{period or '未知报告期'} {labels[statement]}要点：{ {k: structured.get(k) for k in aliases} }"
        provenance = result.provenance_for(row, upstream_source=UPSTREAM.get(result.api))
        cards.append(_make_card(
            source_type='financial_report',
            title=f'{normalize_symbol(symbol)} {labels[statement]} {period}',
            url=None,
            publish_time=str(row.get('act_pubtime') or row.get('publish_date') or ''),
            source_name='通联数据 DataAPI（上市公司披露）',
            symbol=normalize_symbol(symbol),
            excerpt=truncate(excerpt, 800),
            structured=structured,
            reliability=5,
            fetch_tool='fetch_financial_statements',
            provenance=provenance,
        ))
    return cards


INDICATOR_FIELDS = {
    '日期': 'end_date',
    '加权净资产收益率': 'roe_w',
    '净资产收益率': 'roe',
    '销售毛利率': 'gross_margin',
    '销售净利率': 'np_margin',
    '资产负债率': 'asset_liab_ratio',
    '流动比率': 'current_ratio',
    '速动比率': 'quick_ratio',
    '每股收益': 'basic_eps',
    '存货周转率': 'inven_turnover',
    '应收账款周转率': 'ar_turnover',
    '营业收入同比': 't_revenue_yoy',
    '归母净利润同比': 'ni_attr_p_yoy',
    '经营现金流同比': 'n_cf_opa_yoy',
    '营业收入环比': 't_revenue_qoq',
    '归母净利润环比': 'ni_attr_p_qoq',
}


def map_financial_indicators(
    result: ProviderResult,
    symbol: str,
    *,
    max_count: int = 20,
) -> List[EvidenceCard]:
    cards = []
    for row in _current_rows(result, merged_only=True)[:max_count]:
        structured = {label: row.get(field) for label, field in INDICATOR_FIELDS.items() if field in row}
        structured.update({
            'report_period': row.get('end_date'),
            'report_type': row.get('report_type'),
            'merged_flag': row.get('merged_flag'),
            'publish_date': row.get('publish_date'),
            'indicator_basis': 'datayes_derived',
            'degraded': result.degraded,
            'degradation_reasons': result.degradation_reasons,
        })
        period = _date(row.get('end_date'))
        cards.append(_make_card(
            source_type='financial_report',
            title=f'{to_market_symbol(symbol)} 财务指标 {period}',
            url=None,
            publish_time=str(row.get('act_pubtime') or row.get('publish_date') or ''),
            source_name='通联数据 DataAPI（衍生财务指标）',
            symbol=to_market_symbol(symbol),
            excerpt=truncate(str(structured), 800),
            structured=structured,
            reliability=4,
            fetch_tool='fetch_financial_indicators',
            provenance=result.provenance_for(row, upstream_source=UPSTREAM.get(result.api)),
        ))
    return cards


def _adjusted_series(
    quote_rows: Sequence[Mapping[str, Any]],
    factor_rows: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], str]:
    factors = sorted(
        [
            (_date(r.get('ex_div_date')), safe_float(r.get('accum_adj_factor')))
            for r in factor_rows
            if _date(r.get('ex_div_date')) and safe_float(r.get('accum_adj_factor')) is not None
        ],
        key=lambda item: item[0],
    )
    factor_dates = [item[0] for item in factors]
    rows = sorted(quote_rows, key=lambda row: _date(row.get('trade_date')))
    series = []
    for row in rows:
        trade_date = _date(row.get('trade_date'))
        close = safe_float(row.get('close_price'))
        factor = None
        if factor_dates and trade_date:
            idx = bisect_right(factor_dates, trade_date) - 1
            if idx >= 0:
                factor = factors[idx][1]
        # 复权因子缺失时不以原始收盘价冒充后复权序列。
        adjusted = close * factor if close is not None and factor is not None else None
        series.append({'date': trade_date, 'close': close, 'hfq_close': adjusted, 'hfq_factor': factor})
    # 任一有价格的交易日缺少因子，区间起点就可能被悄悄后移；此时整个
    # 区间收益均不可用，而不是丢弃前段后继续计算。
    complete = bool(series) and any(item['close'] is not None for item in series) and all(
        item['close'] is None or item['hfq_factor'] is not None for item in series
    )
    if not complete:
        for item in series:
            item['hfq_close'] = None
    return series, ('hfq' if complete else 'hfq_unavailable')


def map_stock_quote(
    symbol: str,
    days: int,
    quote_result: ProviderResult,
    factor_result: ProviderResult,
    valuation_result: ProviderResult,
) -> List[EvidenceCard]:
    if not quote_result.rows:
        return []
    quote_rows = sorted(quote_result.rows, key=lambda r: _date(r.get('trade_date')))[-max(1, int(days)):]
    series, adjustment_basis = _adjusted_series(quote_rows, factor_result.rows)
    latest = quote_rows[-1]
    latest_eval = max(valuation_result.rows, key=lambda r: _date(r.get('trade_date')), default={})
    closes = [safe_float(item.get('hfq_close')) for item in series]
    closes = [value for value in closes if value is not None]
    range_pct = (
        round((closes[-1] / closes[0] - 1) * 100, 2)
        if adjustment_basis == 'hfq' and len(closes) >= 2 and closes[0]
        else None
    )
    degraded = quote_result.degraded or factor_result.degraded or valuation_result.degraded
    reasons = list(dict.fromkeys(
        quote_result.degradation_reasons
        + factor_result.degradation_reasons
        + valuation_result.degradation_reasons
    ))
    if adjustment_basis != 'hfq':
        reasons.append('hfq_factor_unavailable')
        degraded = True
    component_provenance = []
    for result, row in (
        (quote_result, latest),
        (factor_result, factor_result.rows[-1] if factor_result.rows else None),
        (valuation_result, latest_eval or None),
    ):
        if row:
            component_provenance.append(result.provenance_for(row, upstream_source=UPSTREAM.get(result.api)))
    structured = {
        'latest_close': safe_float(latest.get('close_price')),
        'range_pct': range_pct,
        'market_cap': safe_float(latest_eval.get('market_value')),
        'neg_market_value': safe_float(latest_eval.get('neg_market_value')),
        'pe_ttm': safe_float(latest_eval.get('pecttm')),
        'pb': safe_float(latest_eval.get('pblyr') or latest_eval.get('pbdgw')),
        'ps': safe_float(latest_eval.get('pslyr')),
        'pcf_operating': safe_float(latest_eval.get('pcfolyr')),
        'series': series,
        'price_basis': 'unadjusted_raw',
        'return_basis': adjustment_basis,
        'component_provenance': component_provenance,
        'degraded': degraded,
        'degradation_reasons': reasons,
    }
    market = to_market_symbol(symbol)
    return_text = (
        f'后复权区间涨跌幅 {range_pct}%'
        if adjustment_basis == 'hfq'
        else '后复权区间涨跌幅不可用（复权因子缺失）'
    )
    excerpt = (
        f'{market} 近{days}日：最新未复权收盘 {structured["latest_close"]}，'
        f'{return_text}；PE(TTM) {structured["pe_ttm"]}，PB {structured["pb"]}'
    )
    return [_make_card(
        source_type='industry_data',
        title=f'{market} 行情与估值快照',
        url=None,
        publish_time=_date(latest.get('trade_date')),
        source_name='通联数据 DataAPI',
        symbol=market,
        excerpt=truncate(excerpt, 800),
        structured=structured,
        reliability=4,
        fetch_tool='fetch_stock_quote',
        provenance=quote_result.provenance_for(latest, upstream_source=UPSTREAM['getMktEqud']),
    )]


def map_industry_card(
    *,
    symbol: Optional[str],
    requested_industry: Optional[str],
    membership: ProviderResult,
    quote: ProviderResult,
    valuation: ProviderResult,
    flow: ProviderResult,
) -> List[EvidenceCard]:
    membership_row = max(
        membership.rows,
        key=lambda r: (int(r.get('is_new') or 0), _date(r.get('into_date'))),
        default={},
    )
    quote_rows = sorted(quote.rows, key=lambda r: _date(r.get('trade_date')))[-30:]
    eval_row = max(valuation.rows, key=lambda r: _date(r.get('trade_date')), default={})
    flow_row = max(flow.rows, key=lambda r: _date(r.get('trade_date')), default={})
    name = (
        membership_row.get('industry_name_3') or membership_row.get('industry_name_2')
        or membership_row.get('industry') or membership_row.get('industry_name') or eval_row.get('industry_name')
        or flow_row.get('industry_name') or requested_industry
    )
    if not name and not quote_rows and not eval_row and not flow_row:
        return []
    series = [
        {
            'date': _date(row.get('trade_date')),
            'close': safe_float(row.get('close_price')),
            'chg_pct': safe_float(row.get('chg_pct')),
        }
        for row in quote_rows
    ]
    results = [membership, quote, valuation, flow]
    reasons = list(dict.fromkeys(reason for result in results for reason in result.degradation_reasons))
    structured = {
        'industry': name,
        'industry_id': membership_row.get('industry_id') or eval_row.get('industry_id') or flow_row.get('industry_id'),
        'industry_version': membership_row.get('industry_version_cd'),
        'series': series,
        'valuation': {
            'trade_date': eval_row.get('trade_date'),
            'pe': safe_float(eval_row.get('pe_value')),
            'pb': safe_float(eval_row.get('pb')),
            'market_value': safe_float(eval_row.get('industry_market_value')),
        },
        'flow': {
            'trade_date': flow_row.get('trade_date'),
            'net_money_inflow': safe_float(flow_row.get('net_money_inflow')),
            'net_inflow_rate': safe_float(flow_row.get('net_inflow_rate')),
        },
        'degraded': any(result.degraded for result in results),
        'degradation_reasons': reasons,
    }
    primary_result, primary_row = next(
        ((r, row) for r, row in ((membership, membership_row), (quote, quote_rows[-1] if quote_rows else {}), (valuation, eval_row), (flow, flow_row)) if row),
        (membership, {}),
    )
    return [_make_card(
        source_type='industry_data',
        title=f'行业结构化数据：{name or "未知行业"}',
        url=None,
        publish_time=_date((quote_rows[-1] if quote_rows else eval_row or flow_row).get('trade_date')),
        source_name='通联数据 DataAPI（行业衍生数据）',
        symbol=to_market_symbol(symbol) if symbol else None,
        excerpt=truncate(f'{name} 近30日行情：{series[-5:]}；估值：{structured["valuation"]}；资金流：{structured["flow"]}', 800),
        structured=structured,
        reliability=4,
        fetch_tool='fetch_industry_data',
        provenance=primary_result.provenance_for(primary_row, upstream_source='交易所数据/通联数据计算') if primary_row else None,
    )]


EVENT_META = {
    'getFdmtEfNew': ('earnings_forecast', '业绩预告'),
    'getFdmtEe': ('earnings_flash', '业绩快报'),
    'getEquChangePlan': ('shareholder_change', '股东增减持计划'),
    'getEquShareBuyback': ('buyback', '股份回购'),
    'getEquMjrCntrPIT': ('major_contract', '重大合同'),
    'getEquRegulatory': ('regulatory', '监管信息'),
    'getEquCompIllegal': ('violation', '违规处罚'),
    'getEquLawSuits': ('lawsuit', '诉讼仲裁'),
    'getEquBoardPIT': ('board_resolution', '董事会公告'),
    'getEquBoardPubPIT': ('board_resolution', '董事会议案'),
    'getEquDivPIT': ('dividend', '分红方案'),
}

EVENT_FACT_FIELDS = {
    'getFdmtEfNew': (
        'end_date', 'report_type', 'forecast_type', 'exp_rev_ll', 'exp_rev_upl',
        'expn_inc_apll', 'expn_inc_apupl', 'n_inc_ap_chgr_ll', 'n_inc_ap_chgr_upl',
        'forecast_cont',
    ),
    'getFdmtEe': (
        'end_date', 'report_type', 'revenue', 'operate_profit', 'n_income_attr_p',
        'basic_eps', 'roe', 'revenue_yoy', 'n_income_attr_pyoy',
    ),
    'getEquChangePlan': (
        'first_publish_date', 'begin_date', 'end_date', 'sh_name', 'chg_dir',
        'sh_chg_up_l', 'sh_chg_ll', 'ratio_up_l', 'ratio_ll', 'chg_reason',
    ),
    'getEquShareBuyback': (
        'pre_pub_date', 'end_date', 'buy_back_process', 'buy_back_type',
        'buy_back_value', 'buy_back_vol', 'price_upl', 'price_ll', 'chg_detl',
    ),
    'getEquMjrCntrPIT': (
        'progress', 'project_name', 'cntr_name', 'cntr_total_upl', 'cntr_total_lol', 'currency',
    ),
    'getEquRegulatory': (
        'regulatory_date', 'regulatory_type', 'regulatory_reason',
        'regulatory_object', 'inquiry_response', 'info_source',
    ),
    'getEquCompIllegal': (
        'lllegal_type', 'adminobject', 'event', 'punishment_type',
        'punishment_measure', 'punishment_unit', 'punishment_amount',
    ),
    'getEquLawSuits': (
        'lawsuit_type', 'sue_date', 'abstract', 'case_introduction', 'event_prce',
        'suit_amount', 'currency_code', 'accuser', 'accused',
    ),
    'getEquBoardPIT': (
        'meeting_date', 'meeting_num', 'meeting_location', 'meeting_mode',
        'plan_drec', 'actr_drec',
    ),
    'getEquBoardPubPIT': ('prop_type', 'prop_name', 'is_pass'),
    'getEquDivPIT': (
        'end_date', 'is_div', 'event_process_cd', 'per_cash_div',
        'per_share_div_ratio', 'per_share_trans_ratio', 'record_date',
        'ex_div_date', 'pay_cash_date',
    ),
}


def _first(row: Mapping[str, Any], fields: Iterable[str]) -> Any:
    for field in fields:
        if row.get(field) not in (None, ''):
            return row[field]
    return None


def map_company_events(result: ProviderResult, symbol: str) -> List[EvidenceCard]:
    event_type, label = EVENT_META[result.api]
    cards = []
    spec = get_endpoint(result.api)
    versions: Dict[str, List[Mapping[str, Any]]] = {}
    for candidate in result.rows:
        versions.setdefault(business_key(spec, candidate), []).append(candidate)
    for row in _current_rows(result, merged_only=False):
        detail = _first(row, (
            'forecast_cont', 'prop_name', 'cntr_name', 'project_name', 'regulatory_reason',
            'event', 'abstract', 'chg_detl', 'chg_reason', 'sh_name',
        ))
        publish_time = _first(row, (
            'publish_date', 'new_pub_date', 'regulatory_date', 'pre_pub_date', 'meeting_date',
        ))
        title = f'{row.get("sec_short_name") or normalize_symbol(symbol)} {label}'
        if detail:
            title += f'：{truncate(str(detail), 80)}'
        announcement_id = row.get('anno_id')
        event_id = row.get('event_id')
        record_id_value = row.get('record_id') or row.get('event_num')
        structured = {
            'event_type': event_type,
            'canonical_event_type': event_type,
            'announcement_id': announcement_id,
            'event_id': event_id,
            'record_id': record_id_value,
            'publish_date': publish_time,
            'degraded': result.degraded,
            'degradation_reasons': result.degradation_reasons,
        }
        for field in EVENT_FACT_FIELDS[result.api]:
            if row.get(field) not in (None, ''):
                structured[field] = row[field]
        history = [
            candidate for candidate in versions.get(business_key(spec, row), [])
            if row_fingerprint(candidate) != row_fingerprint(row)
        ]
        if history:
            structured['revision_count'] = len(history)
            structured['superseded_provenance'] = [
                result.provenance_for(
                    candidate, upstream_source='上市公司公告/交易所/监管机构'
                )
                for candidate in history
            ]
        cards.append(_make_card(
            source_type='announcement',
            title=title,
            url=row.get('url') or None,
            publish_time=str(publish_time or ''),
            source_name='通联数据 DataAPI（结构化公司事件）',
            symbol=to_market_symbol(symbol),
            excerpt=truncate(str(detail or label), 800),
            structured=structured,
            reliability=5,
            fetch_tool='fetch_announcements',
            provenance=result.provenance_for(row, upstream_source='上市公司公告/交易所/监管机构'),
        ))
    return cards


def map_investor_research(
    activity_result: ProviderResult,
    detail_results: Mapping[str, ProviderResult],
    symbol: str,
) -> List[EvidenceCard]:
    """机构调研是独立证据类型，绝不映射成券商研报。"""
    cards: List[EvidenceCard] = []
    for row in activity_result.rows:
        event_id = str(row.get('event_id') or '')
        detail_result = detail_results.get(event_id)
        details = (detail_result.rows if detail_result else [])
        institutions = sorted({str(item.get('party_name')) for item in details if item.get('party_name')})
        activity_types = sorted({str(item.get('activity_type')) for item in details if item.get('activity_type')})
        locations = sorted({str(item.get('location')) for item in details if item.get('location')})
        # 合同边界未确认时不下发机构问答正文；只保留完成报告所需的
        # 参与方、活动类型等最小结构化事实。
        discussion = next((str(item.get('centent')) for item in details if item.get('centent')), '')
        include_discussion = activity_result.license_scope != 'private_derived_only'
        reasons = list(activity_result.degradation_reasons)
        if detail_result:
            reasons.extend(detail_result.degradation_reasons)
        structured = {
            'event_id': event_id,
            'survey_date': row.get('survey_date'),
            'publish_date': row.get('publish_date'),
            'institution_count': row.get('party_num'),
            'institutions': institutions[:20],
            'activity_types': activity_types,
            'locations': locations[:5],
            'discussion_available': bool(discussion),
            'degraded': activity_result.degraded or bool(detail_result and detail_result.degraded),
            'degradation_reasons': list(dict.fromkeys(reasons)),
        }
        if include_discussion and discussion:
            structured['discussion_excerpt'] = truncate(discussion, 500)
        provenance = activity_result.provenance_for(
            row, upstream_source='上市公司机构调研活动披露'
        )
        if detail_result and details:
            structured['detail_provenance'] = detail_result.provenance_for(
                details[0], upstream_source='上市公司机构调研活动披露'
            )
        cards.append(_make_card(
            source_type='investor_research',
            title=f'{row.get("sec_short_name") or normalize_symbol(symbol)} 机构调研 {row.get("survey_date") or ""}',
            url=None,
            publish_time=str(row.get('publish_date') or ''),
            source_name='通联数据 DataAPI（上市公司机构调研披露）',
            symbol=to_market_symbol(symbol),
            excerpt=truncate(
                f'机构数量 {row.get("party_num")}；参与机构 {institutions[:8]}；'
                f'活动类型 {activity_types[:5]}'
                + (f'；{discussion}' if include_discussion and discussion else ''),
                800,
            ),
            structured=structured,
            reliability=5,
            fetch_tool='fetch_announcements',
            provenance=provenance,
        ))
    return cards


def _normalize_title(title: str) -> str:
    value = re.sub(r'<[^>]+>', '', title or '')
    value = re.sub(r'\s+|[：:，,。；;（）()【】\[\]_-]', '', value)
    return value.lower()


def announcement_dedup_key(card: EvidenceCard) -> str:
    structured = card.structured or {}
    announcement_id = structured.get('announcement_id') or structured.get('anno_id')
    event_id = structured.get('event_id')
    if announcement_id:
        return f'announcement:{announcement_id}'
    if event_id:
        return f'event:{event_id}'
    record_id_value = structured.get('record_id')
    provenance = getattr(card, 'provenance', None) or structured.get('provenance') or {}
    if record_id_value:
        return f'record:{provenance.get("api") or "unknown"}:{record_id_value}'
    event_type = (
        structured.get('canonical_event_type')
        or structured.get('event_type')
        or structured.get('category')
        or 'announcement'
    )
    fallback = (
        f'fallback:{normalize_symbol(card.symbol or "")}:{_date(card.publish_time)}:'
        f'{event_type}:{_normalize_title(card.title)}'
    )
    return f'fallback:{hashlib.sha256(fallback.encode("utf-8")).hexdigest()}'


def _event_item(card: EvidenceCard) -> Dict[str, Any]:
    structured = dict(card.structured or {})
    for key in ('degraded', 'degradation_reasons', 'public_source', 'items'):
        structured.pop(key, None)
    return {
        'title': card.title,
        'event_type': structured.get('canonical_event_type') or structured.get('event_type'),
        'record_id': structured.get('record_id'),
        'facts': structured,
        'provenance': getattr(card, 'provenance', None) or {},
    }


def merge_announcement_cards(
    public_cards: Sequence[EvidenceCard],
    datayes_cards: Sequence[EvidenceCard],
) -> List[EvidenceCard]:
    merged: Dict[str, EvidenceCard] = {}
    for card in public_cards:
        merged[announcement_dedup_key(card)] = card

    # 同一 annoID 的 Datayes 主表/明细先聚合，避免 BoardPub 多议案 last-wins。
    datayes_grouped: Dict[str, EvidenceCard] = {}
    for card in datayes_cards:
        key = announcement_dedup_key(card)
        previous = datayes_grouped.get(key)
        if previous:
            previous.structured.setdefault('items', [_event_item(previous)])
            previous.structured['items'].append(_event_item(card))
            event_types = previous.structured.setdefault(
                'event_types', [previous.structured.get('event_type')]
            )
            if card.structured.get('event_type') not in event_types:
                event_types.append(card.structured.get('event_type'))
            related_titles = previous.structured.setdefault('related_titles', [previous.title])
            if card.title not in related_titles:
                related_titles.append(card.title)
            previous.structured['degraded'] = bool(
                previous.structured.get('degraded') or card.structured.get('degraded')
            )
            continue
        datayes_grouped[key] = card

    for key, card in datayes_grouped.items():
        previous = merged.get(key)
        if previous:
            if not card.url and previous.url:
                card.url = previous.url
            card.source_name = '通联数据 DataAPI / 巨潮资讯网'
            card.structured['public_source'] = {
                'source_name': previous.source_name,
                'title': previous.title,
                'url': previous.url,
                'publish_time': previous.publish_time,
            }
            card.reliability = max(card.reliability, previous.reliability)
        merged[key] = card
    return sorted(merged.values(), key=lambda c: str(c.publish_time or ''), reverse=True)


def mark_public_fallback(cards: Sequence[EvidenceCard], reasons: Sequence[str]) -> List[EvidenceCard]:
    clean_reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))
    for card in cards:
        card.structured = dict(card.structured or {})
        card.structured['degraded'] = True
        card.structured['datayes_degradation_reasons'] = clean_reasons
        if 'provenance' in getattr(EvidenceCard, '__dataclass_fields__', {}):
            card.provenance = {
                'provider': 'public_fallback',
                'api': None,
                'record_key': None,
                'business_key': None,
                'as_of': datetime.now().astimezone().isoformat(timespec='seconds'),
                'update_time': None,
                'warehouse_watermark': None,
                'row_fingerprint': row_fingerprint(card.to_dict()),
                'upstream_source': card.source_name,
                'license_scope': 'public',
            }
    return list(cards)
