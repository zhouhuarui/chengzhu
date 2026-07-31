"""Deterministic financial fact normalisation.

No network or LLM call is made here.  The normaliser consumes the immutable
evidence snapshot for one run and emits exact ``FinancialFact`` records.  A
fact with an explicitly present but null source value is retained and marked
``missing_value`` so downstream code cannot silently substitute a prior run's
number.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..models.debate import FinancialFact


_NULL_TEXT = {'', '--', 'n/a', 'na', 'none', 'null', 'nan'}

# These fields denote an upstream publication/announcement event.  In
# particular, ``provenance.as_of`` and update/fetch timestamps are deliberately
# absent: they describe when Chengzhu observed a row, not when the issuer made
# the financial information public.
_DISCLOSURE_FIELDS = (
    'publish_date', 'publish_time',
    'NOTICE_DATE', 'PUBLISH_DATE', 'ANNOUNCEMENT_DATE', 'ANN_DATE',
    'DISCLOSURE_DATE', 'ACT_PUBTIME',
    'notice_date', 'announcement_date', 'ann_date', 'disclosure_date',
    'act_pubtime', '公告日期', '披露日期', '发布日期',
)

# Canonical metric, preferred 100m-CNY field, raw field.  The normaliser uses
# the explicit display-unit copy where available and never emits both copies.
_AMOUNT_FIELDS: Dict[str, Tuple[str, str]] = {
    '营业总收入': ('TOTAL_OPERATE_INCOME_yi', 'TOTAL_OPERATE_INCOME'),
    '营业收入': ('OPERATE_INCOME_yi', 'OPERATE_INCOME'),
    '营业利润': ('OPERATE_PROFIT_yi', 'OPERATE_PROFIT'),
    '利润总额': ('TOTAL_PROFIT_yi', 'TOTAL_PROFIT'),
    '净利润': ('NETPROFIT_yi', 'NETPROFIT'),
    '归母净利润': ('PARENT_NETPROFIT_yi', 'PARENT_NETPROFIT'),
    '总资产': ('TOTAL_ASSETS_yi', 'TOTAL_ASSETS'),
    '总负债': ('TOTAL_LIABILITIES_yi', 'TOTAL_LIABILITIES'),
    '所有者权益': ('TOTAL_EQUITY_yi', 'TOTAL_EQUITY'),
    '归母权益': ('PARENT_EQUITY_yi', 'PARENT_EQUITY'),
    '货币资金': ('MONETARYFUNDS_yi', 'MONETARYFUNDS'),
    '存货': ('INVENTORY_yi', 'INVENTORY'),
    '应收账款': ('ACCOUNTS_RECE_yi', 'ACCOUNTS_RECE'),
    '经营活动现金流入': ('CASH_IN_OPERATE_yi', 'CASH_IN_OPERATE'),
    '经营活动现金流出': ('CASH_OUT_OPERATE_yi', 'CASH_OUT_OPERATE'),
    '经营活动现金流净额': ('NETCASH_OPERATE_yi', 'NETCASH_OPERATE'),
    '投资活动现金流净额': ('NETCASH_INVEST_yi', 'NETCASH_INVEST'),
    '筹资活动现金流净额': ('NETCASH_FINANCE_yi', 'NETCASH_FINANCE'),
    '现金净增加额': ('NET_CHANGE_CASH_yi', 'NET_CHANGE_CASH'),
    '期末现金余额': ('END_CASH_yi', 'END_CASH'),
}

_OTHER_FIELDS: Dict[str, Tuple[str, str, str]] = {
    # source field: (canonical metric, unit, currency)
    'BASIC_EPS': ('基本每股收益', '元/股', 'CNY'),
    '每股收益': ('基本每股收益', '元/股', 'CNY'),
    '每股净资产': ('每股净资产', '元/股', 'CNY'),
    '加权净资产收益率': ('加权净资产收益率', '%', ''),
    '净资产收益率': ('净资产收益率', '%', ''),
    '销售毛利率': ('销售毛利率', '%', ''),
    '销售净利率': ('销售净利率', '%', ''),
    '资产负债率': ('资产负债率', '%', ''),
    '营业收入同比': ('营业收入同比', '%', ''),
    '归母净利润同比': ('归母净利润同比', '%', ''),
    '经营现金流同比': ('经营现金流同比', '%', ''),
    '营业收入环比': ('营业收入环比', '%', ''),
    '归母净利润环比': ('归母净利润环比', '%', ''),
    '流动比率': ('流动比率', '倍', ''),
    '速动比率': ('速动比率', '倍', ''),
    '存货周转率': ('存货周转率', '次', ''),
    '应收账款周转率': ('应收账款周转率', '次', ''),
}


def _as_mapping(card: Any) -> Dict[str, Any]:
    if isinstance(card, Mapping):
        raw = dict(card)
    elif hasattr(card, 'to_dict'):
        raw = dict(card.to_dict())
    else:
        raw = dict(vars(card))

    # Canonical evidence_index entries wrap the EvidenceCard in ``card``.
    nested = raw.get('card')
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key in ('evidence_uid', 'display_id'):
            if raw.get(key) not in (None, ''):
                merged[key] = raw[key]
        return merged
    return raw


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def stable_evidence_uid(card: Any) -> str:
    """Return an immutable identifier independent of a run's E1/E2 ordering."""

    data = _as_mapping(card)
    existing = data.get('evidence_uid')
    if existing:
        return str(existing)
    provenance = data.get('provenance') if isinstance(data.get('provenance'), Mapping) else {}
    durable_key = provenance.get('record_key') or provenance.get('row_fingerprint')
    identity = durable_key or {
        'source_type': data.get('source_type'),
        'source_name': data.get('source_name'),
        'symbol': data.get('symbol'),
        'publish_time': data.get('publish_time'),
        'title': data.get('title'),
        'structured': data.get('structured') or {},
    }
    digest = hashlib.sha256(_canonical_json(identity).encode('utf-8')).hexdigest()
    return f'ev_{digest[:24]}'


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(',', '')
        if text.lower() in _NULL_TEXT:
            return None
        # Keep a leading sign and decimal/exponent, but do not guess units from
        # prose such as "约 10 亿元".
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', text):
            return None
        value = text
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _date_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ''
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and 1900 <= int(digits[:4]) <= 2100:
        return f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}'
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if match:
        return f'{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'
    return text[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', text) else ''


def _disclosure_time(
    data: Mapping[str, Any],
    structured: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> str:
    """Return an actual public-disclosure day, never an observation time."""

    for source in (structured, data, provenance):
        for field in _DISCLOSURE_FIELDS:
            value = _date_text(source.get(field))
            if value:
                return value
    return ''


def _period_type(period: str, explicit: Any = None) -> str:
    text = str(explicit or '').strip().upper().replace('Q2', 'H1')
    aliases = {
        '1': 'Q1', 'Q1': 'Q1', '一季报': 'Q1',
        '2': 'H1', 'H1': 'H1', '半年报': 'H1', '中报': 'H1',
        '3': 'Q3', 'Q3': 'Q3', '三季报': 'Q3',
        '4': 'FY', 'FY': 'FY', 'ANNUAL': 'FY', '年报': 'FY', 'A': 'FY',
    }
    if text in aliases:
        return aliases[text]
    suffix = period[5:10] if len(period) >= 10 else ''
    return {'03-31': 'Q1', '06-30': 'H1', '09-30': 'Q3', '12-31': 'FY'}.get(suffix, 'unknown')


def _accumulation_basis(structured: Mapping[str, Any], period_type: str, provenance: Mapping[str, Any]) -> str:
    explicit = structured.get('accumulation_basis') or structured.get('period_basis')
    if explicit:
        text = str(explicit).strip().lower()
        if text in {'cumulative', 'ytd', '累计'}:
            return 'cumulative'
        if text in {'single_quarter', 'quarter', '单季'}:
            return 'single_quarter'
        if text in {'point_in_time', 'snapshot', '时点'}:
            return 'point_in_time'
    if structured.get('is_cumulative') is True:
        return 'cumulative'
    if structured.get('is_cumulative') is False:
        return 'single_quarter'
    statement = str(structured.get('statement') or '').lower()
    api = str(provenance.get('api') or '')
    if statement == 'balance':
        return 'point_in_time'
    if api == 'getFdmtMainDataQPIT':
        return 'single_quarter'
    if api == 'getFdmtMainDataPIT':
        return 'cumulative'
    if statement in {'income', 'cashflow'} and period_type != 'unknown':
        return 'cumulative'
    # Ratios without an explicit PIT/QPIT source describe the stated period but
    # cannot safely be treated as either cumulative or single-quarter.
    return 'unknown'


def _consolidation_scope(structured: Mapping[str, Any]) -> str:
    explicit = structured.get('consolidation_scope') or structured.get('statement_scope')
    if explicit:
        text = str(explicit).strip().lower()
        if text in {'consolidated', 'merged', '合并', '1', 'true'}:
            return 'consolidated'
        if text in {'parent_company', 'parent', '母公司', '0', 'false'}:
            return 'parent_company'
    flag = structured.get('merged_flag')
    if flag in (1, '1', True, 'Y', 'y'):
        return 'consolidated'
    if flag in (0, '0', False, 'N', 'n'):
        return 'parent_company'
    return 'unknown'


def _within_window(value: str, time_window: Optional[Mapping[str, str]]) -> bool:
    if not time_window or not value:
        return True
    day = value[:10]
    start = _date_text(time_window.get('start'))
    end = _date_text(time_window.get('end'))
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _fact_uid(parts: Sequence[Any]) -> str:
    digest = hashlib.sha256('|'.join(str(part or '') for part in parts).encode('utf-8')).hexdigest()
    return f'fact_{digest[:24]}'


def _quality_flags(
    *,
    value: Optional[Decimal],
    unit: str,
    currency: str,
    period: str,
    period_type: str,
    accumulation_basis: str,
    consolidation_scope: str,
    disclosure_time: str,
    time_window: Optional[Mapping[str, str]],
    is_monetary: bool,
    degraded: bool,
) -> List[str]:
    flags: List[str] = []
    if value is None:
        flags.append('missing_value')
    if not unit:
        flags.append('missing_unit')
    if is_monetary and not currency:
        flags.append('missing_currency')
    if not period or period_type == 'unknown':
        flags.append('unknown_period')
    if accumulation_basis == 'unknown':
        flags.append('unknown_accumulation_basis')
    if consolidation_scope == 'unknown':
        flags.append('unknown_consolidation_scope')
    if not disclosure_time:
        flags.append('missing_disclosure_time')
    elif period and disclosure_time < period:
        # A financial statement cannot have been publicly disclosed before its
        # reporting period ended.  Treat such upstream metadata as invalid,
        # rather than silently accepting a collection/update timestamp.
        flags.append('invalid_disclosure_time')
    # The report period is the primary boundary.  Disclosure time is checked as
    # well to reject information unavailable at the run's stated end date.
    if time_window and (
        (period and not _within_window(period, time_window))
        or (disclosure_time and not _within_window(disclosure_time, time_window))
    ):
        flags.append('outside_time_window')
    if degraded:
        flags.append('degraded_source')
    return flags


class FinancialNormalizer:
    """Convert frozen EvidenceCard records to stable ``FinancialFact`` rows."""

    def __init__(self, time_window: Optional[Mapping[str, str]] = None):
        self.time_window = dict(time_window or {})

    def normalize_card(self, card: Any) -> List[FinancialFact]:
        data = _as_mapping(card)
        if str(data.get('source_type') or '') != 'financial_report':
            return []
        structured = data.get('structured')
        if not isinstance(structured, Mapping):
            return []

        evidence_uid = stable_evidence_uid(data)
        subject = str(data.get('symbol') or structured.get('symbol') or '')
        period = _date_text(
            structured.get('report_period')
            or structured.get('REPORT_DATE')
            or structured.get('日期')
            or structured.get('end_date')
        )
        ptype = _period_type(period, structured.get('period_type') or structured.get('report_type'))
        provenance = data.get('provenance') if isinstance(data.get('provenance'), Mapping) else {}
        basis = _accumulation_basis(structured, ptype, provenance)
        scope = _consolidation_scope(structured)
        disclosure = _disclosure_time(data, structured, provenance)
        degraded = bool(structured.get('degraded'))
        currency_hint = str(
            structured.get('currency_unit')
            or structured.get('currency')
            or ''
        ).upper()

        facts: List[FinancialFact] = []
        for metric, (yi_field, raw_field) in _AMOUNT_FIELDS.items():
            if yi_field not in structured and raw_field not in structured:
                continue
            if yi_field in structured and _to_decimal(structured.get(yi_field)) is not None:
                source_field = yi_field
                value = _to_decimal(structured.get(yi_field))
                unit = '亿元'
                currency = currency_hint or 'CNY'
            elif raw_field in structured:
                source_field = raw_field
                value = _to_decimal(structured.get(raw_field))
                # Datayes statement values are currency units.  Unknown public
                # inputs remain untyped rather than being assumed comparable.
                amount_unit = str(structured.get('amount_unit') or '').upper()
                currency = currency_hint or (amount_unit if amount_unit in {'CNY', 'RMB'} else '')
                currency = 'CNY' if currency == 'RMB' else currency
                unit = '元' if currency else ''
            else:
                source_field = yi_field
                value = None
                unit = '亿元'
                currency = currency_hint or 'CNY'

            flags = _quality_flags(
                value=value, unit=unit, currency=currency, period=period,
                period_type=ptype, accumulation_basis=basis,
                consolidation_scope=scope, disclosure_time=disclosure,
                time_window=self.time_window, is_monetary=True, degraded=degraded,
            )
            facts.append(FinancialFact(
                fact_uid=_fact_uid((evidence_uid, subject, metric, source_field, period, basis, scope)),
                evidence_uid=evidence_uid,
                subject=subject,
                metric=metric,
                value=value,
                unit=unit,
                currency=currency,
                period=period,
                period_type=ptype,
                accumulation_basis=basis,
                consolidation_scope=scope,
                disclosure_time=disclosure,
                quality_flags=flags,
            ))

        for source_field, (metric, unit, currency) in _OTHER_FIELDS.items():
            if source_field not in structured:
                continue
            value = _to_decimal(structured.get(source_field))
            flags = _quality_flags(
                value=value, unit=unit, currency=currency, period=period,
                period_type=ptype, accumulation_basis=basis,
                consolidation_scope=scope, disclosure_time=disclosure,
                time_window=self.time_window, is_monetary=bool(currency), degraded=degraded,
            )
            facts.append(FinancialFact(
                fact_uid=_fact_uid((evidence_uid, subject, metric, source_field, period, basis, scope)),
                evidence_uid=evidence_uid,
                subject=subject,
                metric=metric,
                value=value,
                unit=unit,
                currency=currency,
                period=period,
                period_type=ptype,
                accumulation_basis=basis,
                consolidation_scope=scope,
                disclosure_time=disclosure,
                quality_flags=flags,
            ))
        return facts

    def normalize(
        self,
        cards: Iterable[Any],
        output_path: Optional[os.PathLike] = None,
    ) -> List[FinancialFact]:
        facts: List[FinancialFact] = []
        seen = set()
        for card in cards:
            for fact in self.normalize_card(card):
                if fact.fact_uid in seen:
                    continue
                seen.add(fact.fact_uid)
                facts.append(fact)
        facts.sort(key=lambda item: (item.subject, item.metric, item.period, item.fact_uid))
        if output_path is not None:
            write_facts_jsonl(output_path, facts)
        return facts

    def normalize_to_run(self, cards: Iterable[Any], run_dir: os.PathLike) -> List[FinancialFact]:
        return self.normalize(cards, Path(run_dir) / 'normalized_facts.jsonl')


def write_facts_jsonl(path: os.PathLike, facts: Iterable[FinancialFact]) -> None:
    """Atomically replace a run's normalised facts artefact."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            for fact in facts:
                handle.write(json.dumps(fact.to_dict(), ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_facts_jsonl(path: os.PathLike) -> List[FinancialFact]:
    target = Path(path)
    if not target.is_file():
        return []
    facts: List[FinancialFact] = []
    with target.open('r', encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                facts.append(FinancialFact.from_dict(json.loads(line)))
    return facts


def normalize_financial_facts(
    cards: Iterable[Any],
    *,
    time_window: Optional[Mapping[str, str]] = None,
    output_path: Optional[os.PathLike] = None,
) -> List[FinancialFact]:
    """Functional convenience wrapper used by the pipeline."""

    return FinancialNormalizer(time_window=time_window).normalize(cards, output_path)


__all__ = [
    'FinancialNormalizer', 'load_facts_jsonl', 'normalize_financial_facts',
    'stable_evidence_uid', 'write_facts_jsonl',
]
