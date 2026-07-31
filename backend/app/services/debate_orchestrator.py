"""Two-round, evidence-only fundamental debate orchestration.

The orchestrator has no retrieval tools and never performs network collection.
It receives one frozen evidence index, runs four role calls and one judge call,
then persists an auditable transcript below ``run_dir/debate``.  The
deterministic :class:`EvidenceAuditor` is authoritative: a judge cannot accept
a claim which failed any hard check.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..config import Config
from ..models.debate import (
    Challenge,
    ChallengeStatus,
    ClaimCard,
    ClaimStatus,
    DebateVerdict,
    EvidenceRequest,
    FactAssertion,
    FinancialFact,
    JudgeScore,
    VerdictStatus,
)
from .compliance_checker import check_compliance
from .financial_normalizer import (
    FinancialNormalizer,
    load_facts_jsonl,
    stable_evidence_uid,
)


DEFAULT_DIMENSIONS = ('盈利质量', '现金流与偿债', '增长驱动', '经营变化')
QUALITY_ROLE = '稳健与质量视角'
GROWTH_ROLE = '成长与变化视角'
# All report paths and debate paths share the same deterministic blacklist.
# Keep the alias for compatibility with tests/importers that referenced the
# earlier debate-local name.
from .compliance_checker import COMPLIANCE_BLACKLIST as FORBIDDEN_RECOMMENDATION_RE
NUMBER_RE = re.compile(r'(?<![A-Za-z\d])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?![A-Za-z\d])')
DATE_RE = re.compile(
    r'(?<!\d)(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?(?!\d)'
)
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
NON_COMPARABLE_CHALLENGE_RE = re.compile(
    r'不可比|不能(?:直接)?比较|口径不一致|期间不一致|拒绝比较'
)
WORD_RE = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}')
SEMANTIC_STOP = {
    '公司', '披露', '报告', '数据', '证据', '显示', '认为', '可能', '当前',
    '以及', '通过', '其中', '基于', '信息', '事实', '进一步',
}

# ``assumptions`` is a presentation field, not an escape hatch around the
# deterministic auditor.  A narrowly framed counterfactual/analysis condition
# may remain as an assumption, while source-like factual statements and
# forecasts still need exact frozen-evidence support.
ASSUMPTION_FORECAST_RE = re.compile(
    r'预计|预测|预期|有望|或将|将会|未来|明年|来年|下一(?:季度|年度|期)|'
    r'后续(?:将|会)|翻倍|倍增|(?:收入|利润|现金流|销量|产能|份额)'
    r'[^，。；;]{0,16}(?:增长|下降|提升|降低|改善|恶化|达到|超过)'
)
ASSUMPTION_FACTUAL_RE = re.compile(
    r'已经|已(?:经)?(?:实现|完成|获得|发生|成为|达到)|目前|当前|现为|'
    r'拥有|发生|实现|完成|获得|位居|排名|同比|环比|占比|市场份额|'
    r'(?:公司|企业|业务|产品|技术|市场|行业)[^，。；;]{0,20}'
    r'(?:是|为|领先|龙头|第一|最大|唯一)'
)
ASSUMPTION_CONDITIONAL_RE = re.compile(
    r'^(?:仅)?(?:假设|假定|若|如果|倘若|在.+?(?:前提|情形|情况下)|'
    r'不考虑|暂不考虑)|保持不变|维持不变|不发生(?:重大)?变化|'
    r'口径(?:保持)?一致|作为分析前提|不代表预测|取决于|视.+而定|敏感于|'
    r'可能|不确定|尚(?:未|待).{0,16}验证|待(?:后续)?.{0,16}验证|'
    r'需(?:要)?后续.{0,16}(?:披露|验证)'
)
ASSUMPTION_STABLE_CONDITION_RE = re.compile(
    r'保持|维持|不变|不超过|不低于|不发生|不考虑|口径(?:保持)?一致'
)

CONTEXT_TERM_ALIASES = {
    '盈利': ('营业收入', '营业总收入', '净利润', '毛利', '费用', '现金流'),
    '质量': ('现金流', '应收', '存货', '减值', '审计', '利润'),
    '现金流': ('经营活动现金流', '货币资金', '现金余额'),
    '偿债': ('负债', '资产负债率', '流动比率', '货币资金'),
    '增长': ('同比', '环比', '营业收入', '净利润', '业务变化'),
    '变化': ('同比', '环比', '新增', '减少', '调整', '变更'),
    '经营': ('收入', '利润', '现金流', '存货', '应收', '业务'),
}

SAFE_STRUCTURED_CONTEXT_FIELDS = {
    'statement', 'REPORT_DATE', 'report_period', 'period_type',
    'accumulation_basis', 'consolidation_scope', 'merged_flag', 'currency',
    'publish_date', 'visual_status', 'visual_parse_incomplete',
    'candidate_pages', 'page_count', 'file_name', 'file_sha256',
}


class DebateOrchestrationError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(f'{stage}: {message}')
        self.stage = stage
        self.detail = message


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, 'to_dict'):
        return dict(value.to_dict())
    return dict(vars(value))


def _load_json_source(source: Any) -> Any:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding='utf-8'))
    return source


def _evidence_items(source: Any) -> List[Dict[str, Any]]:
    """Accept canonical, flat and legacy evidence-index representations."""

    raw = _load_json_source(source)
    if isinstance(raw, Mapping):
        candidates = raw.get('items') or raw.get('cards') or raw.get('evidence') or []
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        candidates = raw
    else:
        candidates = []

    items: List[Dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, Mapping) and not hasattr(candidate, 'to_dict'):
            continue
        outer = _as_dict(candidate)
        nested = outer.get('card')
        card = dict(nested) if isinstance(nested, Mapping) else dict(outer)
        uid = str(outer.get('evidence_uid') or card.get('evidence_uid') or stable_evidence_uid(card))
        display_id = str(outer.get('display_id') or card.get('display_id') or f'E{index}')
        card['evidence_uid'] = uid
        items.append({'evidence_uid': uid, 'display_id': display_id, 'card': card})
    return items


def _normalise_dimensions(dimensions: Optional[Sequence[str]]) -> List[str]:
    values: List[str] = []
    for value in dimensions or DEFAULT_DIMENSIONS:
        text = str(value or '').strip()
        if text and text not in values:
            values.append(text)
        if len(values) == 4:
            break
    return values or list(DEFAULT_DIMENSIONS)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            for row in rows:
                value = row.to_dict() if hasattr(row, 'to_dict') else row
                handle.write(json.dumps(value, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _parse_day(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    text = str(value or '').strip()
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if match:
        return f'{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and 1900 <= int(digits[:4]) <= 2100:
        return f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}'
    return ''


def _in_window(value: str, time_window: Mapping[str, str]) -> bool:
    if not time_window or not value:
        return True
    day = _parse_day(value)
    if not day:
        return False
    start = _parse_day(time_window.get('start'))
    end = _parse_day(time_window.get('end'))
    return (not start or day >= start) and (not end or day <= end)


def _decimal(value: str) -> Optional[Decimal]:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


class EvidenceAuditor:
    """Authoritative, deterministic hard checks for claims."""

    def __init__(
        self,
        evidence_index: Any,
        facts: Iterable[Any],
        *,
        time_window: Optional[Mapping[str, str]] = None,
        dimensions: Optional[Sequence[str]] = None,
    ):
        self.evidence_items = _evidence_items(evidence_index)
        self.evidence_by_uid = {item['evidence_uid']: item for item in self.evidence_items}
        self.evidence_aliases: Dict[str, str] = {}
        for item in self.evidence_items:
            self.evidence_aliases[item['evidence_uid']] = item['evidence_uid']
            self.evidence_aliases[item['display_id']] = item['evidence_uid']
            self.evidence_aliases[item['display_id'].lstrip('[').rstrip(']')] = item['evidence_uid']
        self.facts: List[FinancialFact] = [
            fact if isinstance(fact, FinancialFact) else FinancialFact.from_dict(_as_dict(fact))
            for fact in facts
        ]
        self.fact_by_uid = {fact.fact_uid: fact for fact in self.facts}
        self.time_window = dict(time_window or {})
        self.dimensions = set(_normalise_dimensions(dimensions))
        self.challenge_audits: List[Dict[str, Any]] = []

    def canonical_evidence_uids(self, values: Iterable[str]) -> List[str]:
        result: List[str] = []
        for value in values:
            text = str(value or '').strip().strip('[]')
            canonical = self.evidence_aliases.get(text, text)
            if canonical and canonical not in result:
                result.append(canonical)
        return result

    def canonical_supporting_quotes(
        self,
        values: Mapping[str, Sequence[str]],
    ) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        for raw_uid, quotes in (values or {}).items():
            canonical = self.canonical_evidence_uids([raw_uid])
            if not canonical:
                continue
            uid = canonical[0]
            clean = [str(item).strip() for item in quotes if str(item).strip()]
            if clean:
                result.setdefault(uid, []).extend(clean)
        return {
            key: list(dict.fromkeys(items)) for key, items in result.items()
        }

    @staticmethod
    def _semantic_terms(text: str) -> set[str]:
        terms: set[str] = set()
        for token in WORD_RE.findall(str(text or '').lower()):
            if re.fullmatch(r'[\u4e00-\u9fff]{2,}', token):
                for index in range(len(token) - 1):
                    part = token[index:index + 2]
                    if part not in SEMANTIC_STOP:
                        terms.add(part)
            elif token not in SEMANTIC_STOP:
                terms.add(token)
        return terms

    @classmethod
    def _semantically_related(cls, statement: str, quotes: Sequence[str]) -> bool:
        def compact(value: str) -> str:
            return re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', value or '').lower()

        statement_value = compact(statement)
        if not statement_value:
            return False
        # A deterministic auditor cannot prove free-form entailment. A
        # factless Claim is therefore publishable only as the normalized source
        # sentence itself; broader interpretation belongs in ``assumptions``
        # and cannot be smuggled into the accepted assertion.
        for quote in quotes:
            quote_value = compact(quote)
            if len(quote_value) >= 4 and quote_value == statement_value:
                return True
        return False

    @staticmethod
    def _contains_supporting_quote(statement: str, quotes: Sequence[str]) -> bool:
        def compact(value: str) -> str:
            return re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', value or '').lower()

        value = compact(statement)
        return any(
            len(compact(quote)) >= 4 and compact(quote) in value
            for quote in quotes
        )

    @staticmethod
    def _evidence_blob(item: Mapping[str, Any]) -> str:
        card = item.get('card') or {}
        return '\n'.join((
            str(card.get('title') or ''),
            str(card.get('excerpt') or ''),
            json.dumps(card.get('structured') or {}, ensure_ascii=False, default=str),
        ))

    @staticmethod
    def _claim_numbers(text: str) -> List[Decimal]:
        # Dates and evidence IDs are provenance, not claimed financial values.
        cleaned = DATE_RE.sub(' ', text or '')
        cleaned = re.sub(r'\[?E\d+\]?', ' ', cleaned, flags=re.IGNORECASE)
        numbers: List[Decimal] = []
        for match in NUMBER_RE.findall(cleaned):
            value = _decimal(match)
            if value is None:
                continue
            if value == value.to_integral_value() and Decimal(1900) <= value <= Decimal(2100):
                continue
            numbers.append(value)
        return numbers

    @staticmethod
    def _number_supported(number: Decimal, facts: Sequence[FinancialFact]) -> bool:
        for fact in facts:
            if fact.value is None:
                continue
            if number == fact.value:
                return True
            # Permit harmless display rounding only; never permit a unit scale
            # conversion which is absent from the normalised fact.
            if abs(number - fact.value) <= Decimal('0.005'):
                return True
        return False

    @classmethod
    def _challenge_argument_supported(
        cls,
        challenge: Challenge,
        facts: Sequence[FinancialFact],
        quotes: Sequence[str],
    ) -> bool:
        """Permit exact counterevidence or a narrow deterministic critique."""

        if cls._semantically_related(challenge.argument, quotes):
            return True
        compact = re.sub(
            r'[^A-Za-z0-9\u4e00-\u9fff]+', '', challenge.argument or ''
        ).lower()
        if not compact or not facts:
            return False
        comparable, _ = cls._comparability_check(facts)
        if not comparable and any(
            token in compact
            for token in ('期间', '报告期', '口径', '不可比', '不能比较', 'h1', 'q1', 'q3', 'fy')
        ):
            return True
        metric_aliases: List[str] = []
        for fact in facts:
            metric = str(fact.metric or '')
            metric_aliases.append(metric)
            if '收入' in metric:
                metric_aliases.append('收入')
            if '利润' in metric:
                metric_aliases.extend(('净利润', '利润'))
            if '现金流' in metric:
                metric_aliases.append('现金流')
            if '负债' in metric:
                metric_aliases.append('负债')
            if '资产' in metric:
                metric_aliases.append('资产')
        has_metric = any(
            alias and re.sub(r'\s+', '', alias).lower() in compact
            for alias in metric_aliases
        )
        has_critique = any(
            token in compact
            for token in (
                '不足', '不能', '缺少', '无法', '尚未', '仍需',
                '外推', '单期', '持续', '不一致', '反证', '可比',
            )
        )
        return has_metric and has_critique

    def _citation_check(self, claim: ClaimCard, facts: Sequence[FinancialFact]) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        claim.evidence_uids = self.canonical_evidence_uids(claim.evidence_uids)
        claim.supporting_quotes = self.canonical_supporting_quotes(claim.supporting_quotes)
        if not claim.evidence_uids:
            issues.append('citation:no_evidence_uid')
        invalid = [uid for uid in claim.evidence_uids if uid not in self.evidence_by_uid]
        if invalid:
            issues.append(f'citation:unknown_evidence:{",".join(invalid)}')
        for fact in facts:
            if fact.evidence_uid not in claim.evidence_uids:
                issues.append(f'citation:fact_evidence_not_cited:{fact.fact_uid}')
        valid_quotes: List[str] = []
        for uid in claim.evidence_uids:
            quotes = claim.supporting_quotes.get(uid) or []
            if not quotes:
                issues.append(f'citation:missing_supporting_quote:{uid}')
                continue
            item = self.evidence_by_uid.get(uid)
            if not item:
                continue
            blob = self._evidence_blob(item)
            for quote in quotes:
                if len(quote.strip()) < 2 or quote.strip() not in blob:
                    issues.append(f'citation:quote_not_found:{uid}')
                else:
                    valid_quotes.append(quote.strip())
        if (
            valid_quotes
            and not facts
            and not self._semantically_related(claim.assertion, valid_quotes)
        ):
            issues.append('citation:semantic_mismatch')
        return not issues, issues

    @staticmethod
    def _period_supported_in_text(fact: FinancialFact, text: str) -> bool:
        compact = re.sub(r'\s+', '', text or '').lower()
        year = str(fact.period or '')[:4]
        if year and year not in compact:
            return False
        aliases = {
            'FY': ('fy', '年度', '全年', '年报'),
            'H1': ('h1', '半年', '上半年', '中报'),
            'Q1': ('q1', '一季度', '一季报', '首季'),
            'Q3': ('q3', '三季度', '三季报', '前三季度'),
        }
        values = aliases.get(str(fact.period_type or '').upper())
        return not values or any(value in compact for value in values)

    @staticmethod
    def canonical_fact_statement(facts: Sequence[FinancialFact]) -> str:
        parts: List[str] = []
        for fact in facts:
            value = format(fact.value, 'f') if fact.value is not None else '缺失'
            parts.append(
                f'{fact.subject}在{fact.period}（{fact.period_type}，'
                f'{fact.currency}/{fact.unit}，{fact.accumulation_basis}，'
                f'{fact.consolidation_scope}）的{fact.metric}为{value}{fact.unit}'
            )
        return '；'.join(parts) + ('。' if parts else '')

    def _fact_assertion_check(
        self,
        claim: ClaimCard,
        facts: Sequence[FinancialFact],
    ) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        by_uid = {item.fact_uid: item for item in claim.fact_assertions}
        duplicates = [
            uid for uid in by_uid
            if sum(item.fact_uid == uid for item in claim.fact_assertions) > 1
        ]
        for uid in duplicates:
            issues.append(f'numeric:duplicate_fact_assertion:{uid}')
        for uid in claim.fact_uids:
            if uid not in by_uid:
                issues.append(f'numeric:missing_fact_assertion:{uid}')
        for uid in by_uid:
            if uid not in claim.fact_uids:
                issues.append(f'numeric:unreferenced_fact_assertion:{uid}')

        fields = (
            'subject', 'metric', 'unit', 'currency', 'period', 'period_type',
            'accumulation_basis', 'consolidation_scope',
        )
        text = claim.assertion or ''
        for fact in facts:
            assertion = by_uid.get(fact.fact_uid)
            if assertion is None:
                continue
            for field_name in fields:
                if str(getattr(assertion, field_name) or '') != str(getattr(fact, field_name) or ''):
                    issues.append(f'numeric:fact_assertion_mismatch:{fact.fact_uid}:{field_name}')
            if assertion.value != fact.value:
                issues.append(f'numeric:fact_assertion_mismatch:{fact.fact_uid}:value')
            # Bind the natural-language sentence to the exact structured fact;
            # otherwise a correct sidecar could accompany unrelated prose.
            if fact.subject and fact.subject not in text:
                issues.append(f'numeric:statement_missing_subject:{fact.fact_uid}')
            if fact.metric and fact.metric not in text:
                issues.append(f'numeric:statement_missing_metric:{fact.fact_uid}')
            if fact.unit and fact.unit not in text:
                issues.append(f'numeric:statement_missing_unit:{fact.fact_uid}')
            if not self._period_supported_in_text(fact, text):
                issues.append(f'numeric:statement_period_mismatch:{fact.fact_uid}')
            currency = str(fact.currency or '').upper()
            if currency == 'CNY' and not any(token in text for token in ('CNY', '人民币', '元')):
                issues.append(f'numeric:statement_currency_mismatch:{fact.fact_uid}')
            elif currency and currency != 'CNY' and currency not in text.upper():
                issues.append(f'numeric:statement_currency_mismatch:{fact.fact_uid}')
        if facts:
            def compact(value: str) -> str:
                return re.sub(r'\s+', '', value or '').rstrip('。')

            expected = self.canonical_fact_statement(facts)
            if compact(text) != compact(expected):
                issues.append('numeric:noncanonical_fact_statement')
        return not issues, issues

    def _numeric_check(self, claim: ClaimCard, facts: Sequence[FinancialFact]) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        missing_fact_ids = [uid for uid in claim.fact_uids if uid not in self.fact_by_uid]
        if missing_fact_ids:
            issues.append(f'numeric:unknown_fact:{",".join(missing_fact_ids)}')
        for fact in facts:
            if fact.value is None or 'missing_value' in fact.quality_flags:
                issues.append(f'numeric:missing_value:{fact.fact_uid}')
        numeric_text = claim.assertion
        for fact in facts:
            if fact.subject:
                numeric_text = numeric_text.replace(str(fact.subject), ' ')
        numbers = self._claim_numbers(numeric_text)
        if numbers and not facts:
            issues.append('numeric:numbers_without_facts')
        for number in numbers:
            if facts and not self._number_supported(number, facts):
                issues.append(f'numeric:unsupported_value:{format(number, "f")}')
        _, assertion_issues = self._fact_assertion_check(claim, facts)
        issues.extend(assertion_issues)
        return not issues, issues

    @staticmethod
    def _comparability_check(facts: Sequence[FinancialFact]) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        disqualifying_flags = {
            'missing_unit', 'missing_currency', 'unknown_period',
            'unknown_accumulation_basis', 'unknown_consolidation_scope',
        }
        for fact in facts:
            bad = disqualifying_flags.intersection(fact.quality_flags)
            if bad:
                issues.append(f'comparability:{fact.fact_uid}:{",".join(sorted(bad))}')

        by_metric: Dict[str, List[FinancialFact]] = defaultdict(list)
        for fact in facts:
            by_metric[fact.metric].append(fact)
        for metric, group in by_metric.items():
            if len(group) < 2:
                continue
            reference = group[0]
            for candidate in group[1:]:
                if not reference.is_comparable_with(candidate):
                    issues.append(
                        f'comparability:mismatch:{metric}:{reference.fact_uid}:{candidate.fact_uid}'
                    )
        return not issues, issues

    def _currentness_check(self, claim: ClaimCard, facts: Sequence[FinancialFact]) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        for fact in facts:
            if 'outside_time_window' in fact.quality_flags:
                issues.append(f'currentness:outside_time_window:{fact.fact_uid}')
            if 'invalid_disclosure_time' in fact.quality_flags:
                issues.append(f'currentness:invalid_disclosure_time:{fact.fact_uid}')
            if 'missing_disclosure_time' in fact.quality_flags or not fact.disclosure_time:
                issues.append(f'currentness:missing_disclosure_time:{fact.fact_uid}')
            elif fact.period and fact.disclosure_time < fact.period:
                issues.append(f'currentness:disclosure_before_period_end:{fact.fact_uid}')
            elif self.time_window and not _in_window(fact.disclosure_time, self.time_window):
                issues.append(f'currentness:disclosure_outside_window:{fact.fact_uid}')
        for uid in claim.evidence_uids:
            item = self.evidence_by_uid.get(uid)
            if not item:
                continue
            published = _parse_day(item['card'].get('publish_time'))
            if self.time_window and (not published or not _in_window(published, self.time_window)):
                issues.append(f'currentness:evidence_outside_or_unknown:{uid}')
        return not issues, issues

    def _assumption_check(
        self,
        claim: ClaimCard,
    ) -> Tuple[List[str], List[str]]:
        """Audit assumption prose without treating a labelled condition as fact.

        Assumptions may describe a neutral counterfactual or analytical
        condition.  They may not carry an unquoted source-like fact, numeric
        assertion, or forecast into a verdict.  Exact frozen-evidence quotes
        remain admissible because ``_citation_check`` separately verifies that
        every quoted fragment really exists in the cited evidence card.
        """

        citation_issues: List[str] = []
        numeric_issues: List[str] = []
        supporting_quotes = [
            quote
            for uid in claim.evidence_uids
            for quote in (claim.supporting_quotes.get(uid) or [])
            if str(quote).strip()
        ]
        for index, raw_assumption in enumerate(claim.assumptions, start=1):
            assumption = str(raw_assumption or '').strip()
            if not assumption:
                continue
            if self._semantically_related(assumption, supporting_quotes):
                continue

            forecast_text = re.sub(
                r'不(?:构成|代表)(?:任何)?(?:业绩)?预测',
                '',
                assumption,
            )
            forecast = bool(ASSUMPTION_FORECAST_RE.search(forecast_text))
            factual = bool(ASSUMPTION_FACTUAL_RE.search(assumption))
            conditional = bool(ASSUMPTION_CONDITIONAL_RE.search(assumption))
            if (
                forecast
                and conditional
                and ASSUMPTION_STABLE_CONDITION_RE.search(assumption)
            ):
                forecast = False
            numbers = self._claim_numbers(assumption)

            # A forecast remains an evidentiary assertion even when it is put
            # in the assumptions array.  A stable counterfactual such as
            # "假设汇率保持不变" is intentionally allowed.
            if forecast:
                citation_issues.append(
                    f'citation:unsupported_assumption_forecast:{index}'
                )
            elif factual or not conditional:
                citation_issues.append(
                    f'citation:unsupported_assumption_fact:{index}'
                )

            # Numeric scenario thresholds are allowed only when clearly
            # conditional and non-forecasting.  Otherwise there is no typed
            # FactAssertion binding the value to metric/period/unit/scope.
            if numbers and not (conditional and not forecast and not factual):
                numeric_issues.append(
                    f'numeric:unsupported_assumption_value:{index}'
                )

        return list(dict.fromkeys(citation_issues)), list(dict.fromkeys(numeric_issues))

    def _compliance_check(self, claim: ClaimCard) -> Tuple[bool, List[str]]:
        issues = [f'compliance:{item.get("quote")}' for item in check_compliance(claim.assertion)]
        for match in FORBIDDEN_RECOMMENDATION_RE.finditer(claim.assertion or ''):
            issues.append(f'compliance:{match.group(0)}')
        for index, assumption in enumerate(claim.assumptions, start=1):
            for item in check_compliance(assumption):
                issues.append(
                    f'compliance:assumption:{index}:{item.get("quote")}'
                )
        return not issues, list(dict.fromkeys(issues))

    def audit_claim(self, claim: ClaimCard) -> JudgeScore:
        facts = [self.fact_by_uid[uid] for uid in claim.fact_uids if uid in self.fact_by_uid]
        citation_pass, citation_issues = self._citation_check(claim, facts)
        numeric_pass, numeric_issues = self._numeric_check(claim, facts)
        assumption_citation_issues, assumption_numeric_issues = self._assumption_check(claim)
        citation_issues.extend(assumption_citation_issues)
        numeric_issues.extend(assumption_numeric_issues)
        citation_pass = citation_pass and not assumption_citation_issues
        numeric_pass = numeric_pass and not assumption_numeric_issues
        comparability_pass, comparison_issues = self._comparability_check(facts)
        currentness_pass, currentness_issues = self._currentness_check(claim, facts)
        compliance_pass, compliance_issues = self._compliance_check(claim)
        valid_citations = sum(uid in self.evidence_by_uid for uid in claim.evidence_uids)
        coverage = min(1.0, valid_citations / max(1, len(claim.evidence_uids)))
        relevance = 1.0 if claim.dimension in self.dimensions and claim.assertion.strip() else 0.0
        return JudgeScore(
            claim_id=claim.claim_id,
            citation_pass=citation_pass,
            numeric_pass=numeric_pass,
            comparability_pass=comparability_pass,
            currentness_pass=currentness_pass,
            compliance_pass=compliance_pass,
            evidence_coverage=coverage,
            counterevidence_resilience=0.0,
            relevance=relevance,
            issues=(citation_issues + numeric_issues + comparison_issues + currentness_issues + compliance_issues),
        )

    def audit_challenge(
        self,
        challenge: Challenge,
        claim_ids: set[str],
        *,
        claim_by_id: Optional[Mapping[str, ClaimCard]] = None,
        score_by_id: Optional[Mapping[str, JudgeScore]] = None,
    ) -> Dict[str, Any]:
        """Deterministically validate counterevidence before it affects a claim."""

        issues: List[str] = []
        if challenge.target_claim_id not in claim_ids:
            issues.append('challenge:unknown_target')
        challenge.evidence_uids = self.canonical_evidence_uids(challenge.evidence_uids)
        challenge.supporting_quotes = self.canonical_supporting_quotes(
            challenge.supporting_quotes
        )
        if not challenge.evidence_uids:
            issues.append('challenge:no_evidence_uid')
        invalid = [uid for uid in challenge.evidence_uids if uid not in self.evidence_by_uid]
        if invalid:
            issues.append(f'challenge:unknown_evidence:{",".join(invalid)}')
        valid_quotes: List[str] = []
        for uid in challenge.evidence_uids:
            quotes = challenge.supporting_quotes.get(uid) or []
            if not quotes:
                issues.append(f'challenge:missing_supporting_quote:{uid}')
                continue
            item = self.evidence_by_uid.get(uid)
            if not item:
                continue
            blob = self._evidence_blob(item)
            for quote in quotes:
                if len(quote.strip()) < 2 or quote.strip() not in blob:
                    issues.append(f'challenge:quote_not_found:{uid}')
                else:
                    valid_quotes.append(quote.strip())
        bound_facts = [
            self.fact_by_uid[uid]
            for uid in challenge.fact_uids
            if uid in self.fact_by_uid
        ]
        missing_fact_ids = [
            uid for uid in challenge.fact_uids if uid not in self.fact_by_uid
        ]
        if missing_fact_ids:
            issues.append(f'challenge:unknown_fact:{",".join(missing_fact_ids)}')
        for fact in bound_facts:
            if fact.evidence_uid not in challenge.evidence_uids:
                issues.append(f'challenge:fact_evidence_not_cited:{fact.fact_uid}')

        assertion_by_uid = {item.fact_uid: item for item in challenge.fact_assertions}
        for uid in challenge.fact_uids:
            if uid not in assertion_by_uid:
                issues.append(f'challenge:missing_fact_assertion:{uid}')
        for uid in assertion_by_uid:
            if uid not in challenge.fact_uids:
                issues.append(f'challenge:unreferenced_fact_assertion:{uid}')
        exact_fields = (
            'subject', 'metric', 'unit', 'currency', 'period', 'period_type',
            'accumulation_basis', 'consolidation_scope',
        )
        # The challenge is immutable counterevidence owned by the challenger.
        # A poor response from the target must never poison that challenge and
        # make it disappear from the authoritative set.
        challenge_text = challenge.argument
        for fact in bound_facts:
            assertion = assertion_by_uid.get(fact.fact_uid)
            if assertion is None:
                continue
            for field_name in exact_fields:
                if str(getattr(assertion, field_name) or '') != str(getattr(fact, field_name) or ''):
                    issues.append(
                        f'challenge:fact_assertion_mismatch:{fact.fact_uid}:{field_name}'
                    )
            if assertion.value != fact.value:
                issues.append(f'challenge:fact_assertion_mismatch:{fact.fact_uid}:value')

        if bound_facts:
            expected_basis = self.canonical_fact_statement(bound_facts)
            if re.sub(r'\s+', '', challenge.fact_basis_statement or '').rstrip('。') != re.sub(
                r'\s+', '', expected_basis
            ).rstrip('。'):
                issues.append('challenge:noncanonical_fact_basis')
            if self._claim_numbers(challenge_text):
                issues.append('challenge:numbers_outside_fact_basis')
            if valid_quotes and not self._challenge_argument_supported(
                challenge,
                bound_facts,
                valid_quotes,
            ):
                issues.append('challenge:semantic_mismatch')
        elif challenge.fact_basis_statement.strip():
            issues.append('challenge:fact_basis_without_facts')
        elif self._claim_numbers(challenge_text):
            issues.append('challenge:numbers_without_facts')

        comparable, comparison_issues = self._comparability_check(bound_facts)
        declares_mismatch = (
            any(token in challenge.challenge_type.lower() for token in ('mismatch', 'comparability'))
            or NON_COMPARABLE_CHALLENGE_RE.search(challenge.argument or '') is not None
        )
        if not comparable and not declares_mismatch:
            issues.extend(
                f'challenge:{item}' for item in comparison_issues
            )
        if not bound_facts:
            if valid_quotes and not self._semantically_related(
                challenge.argument, valid_quotes
            ):
                issues.append('challenge:semantic_mismatch')

        for uid in challenge.evidence_uids:
            item = self.evidence_by_uid.get(uid)
            if not item:
                continue
            published = _parse_day(item['card'].get('publish_time'))
            if self.time_window and (not published or not _in_window(published, self.time_window)):
                issues.append(f'challenge:evidence_outside_or_unknown:{uid}')

        if check_compliance(challenge.argument) or FORBIDDEN_RECOMMENDATION_RE.search(
            challenge.argument or ''
        ):
            issues.append('challenge:compliance')
        response_compliant = not (
            check_compliance(challenge.response)
            or FORBIDDEN_RECOMMENDATION_RE.search(challenge.response or '')
        )
        resolution_warnings: List[str] = []
        if challenge.resolution_status in {ChallengeStatus.RESOLVED, ChallengeStatus.DISMISSED}:
            response_claim = (claim_by_id or {}).get(str(challenge.response_claim_id or ''))
            response_score = (score_by_id or {}).get(str(challenge.response_claim_id or ''))
            target_claim = (claim_by_id or {}).get(challenge.target_claim_id)
            resolution_valid = all((
                challenge.resolution_role == challenge.role,
                bool(challenge.response.strip()),
                bool(target_claim and challenge.response_role == target_claim.role),
                bool(response_claim and target_claim and response_claim.role == target_claim.role),
                bool(response_score and response_score.hard_pass),
                response_compliant,
                bool(
                    response_claim
                    and self._semantically_related(
                        challenge.response,
                        [response_claim.assertion],
                    )
                ),
            ))
            if not resolution_valid:
                # An unaudited response cannot erase valid counterevidence.
                # Keep the challenge open without discarding the challenge
                # itself from the authoritative set.
                challenge.resolution_status = ChallengeStatus.OPEN
                resolution_warnings.append('challenge:resolution_kept_open')
        return {
            'audit_type': 'challenge',
            'challenge_id': challenge.challenge_id,
            'target_claim_id': challenge.target_claim_id,
            'hard_pass': not issues,
            'issues': list(dict.fromkeys(issues)),
            'resolution_warnings': resolution_warnings,
        }

    def audit_all(self, claims: Sequence[ClaimCard], challenges: Sequence[Challenge]) -> List[JudgeScore]:
        scores = [self.audit_claim(claim) for claim in claims]
        claim_by_id = {claim.claim_id: claim for claim in claims}
        claim_ids = set(claim_by_id)
        score_by_id = {score.claim_id: score for score in scores}
        self.challenge_audits = [
            self.audit_challenge(
                challenge,
                claim_ids,
                claim_by_id=claim_by_id,
                score_by_id=score_by_id,
            ) for challenge in challenges
        ]
        valid_ids = {
            audit['challenge_id'] for audit in self.challenge_audits if audit['hard_pass']
        }
        valid_challenges = [
            challenge for challenge in challenges if challenge.challenge_id in valid_ids
        ]
        unresolved = {ChallengeStatus.OPEN, ChallengeStatus.UPHELD}
        by_target: Dict[str, List[Challenge]] = defaultdict(list)
        for challenge in valid_challenges:
            by_target[challenge.target_claim_id].append(challenge)
        for claim in claims:
            score = score_by_id[claim.claim_id]
            targeted = by_target.get(claim.claim_id, [])
            if not targeted:
                score.counterevidence_resilience = 1.0
            elif any(item.resolution_status in unresolved for item in targeted):
                score.counterevidence_resilience = 0.0
            else:
                score.counterevidence_resilience = 0.7

            if claim.status == ClaimStatus.WITHDRAWN:
                continue
            if not score.hard_pass:
                claim.status = ClaimStatus.REJECTED
            elif any(item.resolution_status in unresolved for item in targeted):
                claim.status = ClaimStatus.DISPUTED
            elif claim.status not in {ClaimStatus.REVISED, ClaimStatus.WITHDRAWN}:
                claim.status = ClaimStatus.PROPOSED
        return scores


class JudgeSynthesizer:
    """Sanitise a judge decision or produce a deterministic fallback."""

    @staticmethod
    def _eligible(
        claims: Sequence[ClaimCard],
        scores: Sequence[JudgeScore],
        challenges: Sequence[Challenge],
    ) -> Dict[str, ClaimCard]:
        hard = {score.claim_id for score in scores if score.hard_pass}
        unresolved_targets = {
            challenge.target_claim_id
            for challenge in challenges
            if challenge.resolution_status in {ChallengeStatus.OPEN, ChallengeStatus.UPHELD}
        }
        return {
            claim.claim_id: claim
            for claim in claims
            if claim.claim_id in hard
            and claim.claim_id not in unresolved_targets
            and claim.status not in {ClaimStatus.WITHDRAWN, ClaimStatus.REJECTED, ClaimStatus.REVISED}
        }

    @staticmethod
    def _common_fields(
        claims: Sequence[ClaimCard],
        scores: Sequence[JudgeScore],
        challenges: Sequence[Challenge],
    ) -> Dict[str, Any]:
        score_by_id = {score.claim_id: score for score in scores}
        unresolved: List[str] = []
        requests: List[EvidenceRequest] = []
        for challenge in challenges:
            if challenge.resolution_status in {ChallengeStatus.OPEN, ChallengeStatus.UPHELD}:
                unresolved.append(challenge.argument)
                target = next(
                    (claim for claim in claims if claim.claim_id == challenge.target_claim_id),
                    None,
                )
                requests.append(EvidenceRequest(
                    dimension=target.dimension if target else '全局',
                    description=(
                        f'{target.dimension if target else "全局"}：未决反证需要后续公开证据'
                    ),
                    reason=f'unresolved_challenge:{challenge.challenge_id}',
                    requested_fields=['public_disclosure', 'supporting_quote'],
                ))
        for claim in claims:
            score = score_by_id.get(claim.claim_id)
            if claim.status == ClaimStatus.DISPUTED or (score and not score.hard_pass):
                unresolved.append(claim.assertion)
        claim_by_id = {claim.claim_id: claim for claim in claims}
        field_map = {
            'citation': ['evidence_uid', 'supporting_quote', 'source'],
            'numeric': ['fact_uid', 'subject', 'metric', 'value', 'unit', 'currency', 'period'],
            'comparability': [
                'period', 'period_type', 'unit', 'currency',
                'accumulation_basis', 'consolidation_scope',
            ],
            'currentness': ['disclosure_time'],
        }
        label_map = {
            'citation': '缺少可核对的原文引用',
            'numeric': '缺少完整且可核对的财务事实字段',
            'comparability': '缺少同期间、同单位、同币种与同口径数据',
            'currentness': '缺少时间窗内的披露时点',
        }
        for score in scores:
            for issue in score.issues:
                category = issue.split(':', 1)[0]
                if category not in field_map:
                    continue
                claim = claim_by_id.get(score.claim_id)
                requests.append(EvidenceRequest(
                    dimension=claim.dimension if claim else '全局',
                    description=(
                        f'{claim.dimension if claim else "全局"}：{label_map[category]}'
                    ),
                    reason=issue,
                    requested_fields=field_map[category],
                ))
        deduped_requests: List[EvidenceRequest] = []
        seen_requests = set()
        for item in requests:
            key = (item.dimension, item.reason, tuple(item.requested_fields))
            if key not in seen_requests:
                seen_requests.add(key)
                deduped_requests.append(item)
        return {
            'unresolved_disputes': list(dict.fromkeys(filter(None, unresolved))),
            'withdrawn_claims': list(dict.fromkeys(
                claim.assertion for claim in claims if claim.status == ClaimStatus.WITHDRAWN
            )),
            'evidence_requests': deduped_requests,
        }

    def from_judge_payload(
        self,
        payload: Mapping[str, Any],
        claims: Sequence[ClaimCard],
        scores: Sequence[JudgeScore],
        challenges: Sequence[Challenge],
    ) -> DebateVerdict:
        eligible = self._eligible(claims, scores, challenges)
        requested = payload.get('accepted_claim_ids') or []
        if isinstance(requested, str):
            requested = [requested]
        accepted_ids = [str(uid) for uid in requested if str(uid) in eligible]
        accepted = [eligible[uid] for uid in accepted_ids]
        for claim in claims:
            if claim.claim_id in accepted_ids:
                claim.status = ClaimStatus.ACCEPTED
            elif claim.claim_id in eligible and claim.status == ClaimStatus.PROPOSED:
                claim.status = ClaimStatus.DISPUTED

        common = self._common_fields(claims, scores, challenges)
        # Narrative facts are always regenerated from accepted ClaimCards.  A
        # model cannot smuggle an unaudited sentence into the verdict body.
        consensus = [claim.assertion for claim in accepted if claim.fact_uids and not claim.assumptions]
        interpretations = [claim.assertion for claim in accepted if claim.assumptions or not claim.fact_uids]
        evidence_requests = list(common['evidence_requests'])
        allowed_dimensions = {claim.dimension for claim in claims}
        allowed_fields = {
            'evidence_uid', 'supporting_quote', 'source', 'fact_uid', 'subject',
            'metric', 'value', 'unit', 'currency', 'period', 'period_type',
            'accumulation_basis', 'consolidation_scope', 'disclosure_time',
            'public_disclosure', 'follow_up_public_item',
        }
        raw_requests = payload.get('evidence_requests') or []
        if isinstance(raw_requests, Mapping):
            raw_requests = [raw_requests]
        if isinstance(raw_requests, list):
            for raw in raw_requests[:12]:
                if not isinstance(raw, Mapping):
                    continue
                dimension = str(raw.get('dimension') or '全局')
                description = str(raw.get('description') or '').strip()[:240]
                reason = str(raw.get('reason') or 'judge_identified_gap').strip()[:120]
                fields = [
                    str(item) for item in (raw.get('requested_fields') or [])
                    if str(item) in allowed_fields
                ]
                if (
                    description
                    and dimension in allowed_dimensions.union({'全局'})
                    and not check_compliance(description)
                ):
                    evidence_requests.append(EvidenceRequest(
                        dimension=dimension,
                        description=description,
                        reason=reason or 'judge_identified_gap',
                        requested_fields=fields or ['public_disclosure'],
                    ))
        # Backward-compatible Judge payloads are converted to the same typed
        # request contract rather than copied into factual verdict prose.
        legacy_gaps = payload.get('evidence_gaps') or []
        if isinstance(legacy_gaps, str):
            legacy_gaps = [legacy_gaps]
        for item in legacy_gaps:
            description = str(item or '').strip()[:240]
            if description and not check_compliance(description):
                evidence_requests.append(EvidenceRequest(
                    dimension='全局',
                    description=description,
                    reason='judge_identified_gap',
                    requested_fields=['public_disclosure'],
                ))
        legacy_followups = payload.get('follow_up_public_items') or []
        if isinstance(legacy_followups, str):
            legacy_followups = [legacy_followups]
        for item in legacy_followups:
            description = str(item or '').strip()[:240]
            if description and not check_compliance(description):
                evidence_requests.append(EvidenceRequest(
                    dimension='全局',
                    description=f'后续公开事项：{description}',
                    reason='judge_follow_up_public_item',
                    requested_fields=['follow_up_public_item'],
                ))
        deduped_requests: List[EvidenceRequest] = []
        seen_requests = set()
        for item in evidence_requests:
            key = (item.dimension, item.description, item.reason)
            if key not in seen_requests:
                seen_requests.add(key)
                deduped_requests.append(item)
        evidence_requests = deduped_requests
        return DebateVerdict(
            accepted_claim_ids=accepted_ids,
            consensus_facts=consensus,
            supported_interpretations=interpretations,
            unresolved_disputes=common['unresolved_disputes'],
            withdrawn_claims=common['withdrawn_claims'],
            evidence_requests=evidence_requests,
            assumptions=[assumption for claim in accepted for assumption in claim.assumptions],
            follow_up_public_items=[
                f'后续公开披露待补充：{item.description}'
                for item in evidence_requests
            ],
            status=VerdictStatus.COMPLETE,
            generated_by='judge',
        )

    def fallback(
        self,
        claims: Sequence[ClaimCard],
        scores: Sequence[JudgeScore],
        challenges: Sequence[Challenge],
        *,
        reason: str,
    ) -> DebateVerdict:
        eligible = self._eligible(claims, scores, challenges)
        accepted = list(eligible.values())
        for claim in accepted:
            claim.status = ClaimStatus.ACCEPTED
        common = self._common_fields(claims, scores, challenges)
        return DebateVerdict(
            accepted_claim_ids=[claim.claim_id for claim in accepted],
            consensus_facts=[
                claim.assertion for claim in accepted if claim.fact_uids and not claim.assumptions
            ],
            supported_interpretations=[
                claim.assertion for claim in accepted if claim.assumptions or not claim.fact_uids
            ],
            unresolved_disputes=common['unresolved_disputes'],
            withdrawn_claims=common['withdrawn_claims'],
            evidence_requests=common['evidence_requests'],
            assumptions=[assumption for claim in accepted for assumption in claim.assumptions],
            follow_up_public_items=[],
            status=VerdictStatus.COMPLETE if accepted or common['unresolved_disputes'] else VerdictStatus.DEGRADED,
            generated_by='deterministic_fallback',
            degradation_reason=reason,
        )


class DebateOrchestrator:
    """Run the fixed four-agent-round plus judge protocol.

    Args:
        run_dir: Immutable run directory. Artefacts are written under
            ``run_dir/debate``.
        evidence_index: Canonical evidence-index dict/path/list.
        facts: Optional facts or JSONL path.  When omitted, facts are derived
            from the supplied frozen evidence and written to the run root.
        llm: Injectable client.  It may expose ``chat_json_result`` (preferred)
            or the legacy ``chat_json`` method.
        degrade_on_failure: Return and persist a degraded verdict instead of
            raising when an agent round cannot complete.

    ``run()`` returns a :class:`DebateVerdict`; full state remains available as
    ``claims``, ``challenges`` and ``audit_scores`` and through run artefacts.
    """

    def __init__(
        self,
        run_dir: os.PathLike,
        evidence_index: Any,
        facts: Optional[Any] = None,
        *,
        dimensions: Optional[Sequence[str]] = None,
        time_window: Optional[Mapping[str, str]] = None,
        llm: Any = None,
        llm_factory: Optional[Callable[[], Any]] = None,
        auto_create_llm: bool = True,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        max_corrections: int = 2,
        degrade_on_failure: bool = True,
        run_id: Optional[str] = None,
        deadline_epoch: Optional[float] = None,
        unavailable_reason: Optional[str] = None,
    ):
        self.run_dir = Path(run_dir)
        self.run_id = str(run_id or self.run_dir.name)
        self.deadline_epoch = float(deadline_epoch) if deadline_epoch is not None else None
        self.unavailable_reason = unavailable_reason
        self.debate_dir = self.run_dir / 'debate'
        self.evidence_items = _evidence_items(evidence_index)
        self.evidence_index = {'items': self.evidence_items}
        self.dimensions = _normalise_dimensions(dimensions)
        self.time_window = dict(time_window or {})
        self.progress_callback = progress_callback
        self.max_corrections = max(0, min(2, int(max_corrections)))
        self.degrade_on_failure = degrade_on_failure
        self._corrections_used = 0
        self._id_counters: Dict[str, int] = defaultdict(int)
        self.claims: List[ClaimCard] = []
        self.challenges: List[Challenge] = []
        self.audit_scores: List[JudgeScore] = []
        self.challenge_audits: List[Dict[str, Any]] = []
        self.valid_challenges: List[Challenge] = []
        self._evidence_context_cache: Optional[List[Dict[str, Any]]] = None

        if facts is None:
            cards = [item for item in self.evidence_items]
            self.facts = FinancialNormalizer(self.time_window).normalize_to_run(cards, self.run_dir)
        elif isinstance(facts, (str, os.PathLike)):
            self.facts = load_facts_jsonl(facts)
        else:
            self.facts = [
                item if isinstance(item, FinancialFact) else FinancialFact.from_dict(_as_dict(item))
                for item in facts
            ]

        self.auditor = EvidenceAuditor(
            self.evidence_index,
            self.facts,
            time_window=self.time_window,
            dimensions=self.dimensions,
        )
        self.llm = llm
        if self.llm is None and llm_factory is not None:
            self.llm = llm_factory()
        if self.llm is None and auto_create_llm:
            self.llm = self._default_llm()

    def _default_llm(self) -> Any:
        key = getattr(Config, 'TEXT_LLM_API_KEY', None) or getattr(Config, 'LLM_API_KEY', None)
        if not key:
            return None
        from ..utils.llm_client import LLMClient
        return LLMClient(
            api_key=key,
            base_url=(
                getattr(Config, 'TEXT_LLM_BASE_URL', None)
                or getattr(Config, 'LLM_BASE_URL', None)
            ),
            model=(
                getattr(Config, 'TEXT_LLM_REASONING_MODEL', None)
                or getattr(Config, 'LLM_MODEL_NAME_ANALYSIS', None)
            ),
            provider=getattr(Config, 'TEXT_LLM_PROVIDER', 'deepseek'),
            budget_run_id=self.run_id,
        )

    def _notify(self, stage: str, **detail: Any) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(stage, detail)
        except Exception:
            # UI progress must never invalidate research artefacts.
            pass

    def _next_id(self, kind: str, stage: str) -> str:
        self._id_counters[kind] += 1
        prefix = 'clm' if kind == 'claim' else 'chg'
        return f'{prefix}_{stage}_{self._id_counters[kind]:03d}'

    def _safe_id(self, requested: Any, kind: str, stage: str, used: set) -> str:
        text = str(requested or '')
        if text and ID_RE.fullmatch(text) and text not in used:
            return text
        while True:
            generated = self._next_id(kind, stage)
            if generated not in used:
                return generated

    def _context_terms(self) -> List[str]:
        terms: List[str] = []
        for dimension in self.dimensions:
            compact = re.sub(r'\s+', '', str(dimension or '')).lower()
            if len(compact) >= 2:
                terms.append(compact)
                terms.extend(
                    compact[index:index + 2]
                    for index in range(len(compact) - 1)
                )
            for key, aliases in CONTEXT_TERM_ALIASES.items():
                if key in compact:
                    terms.extend(aliases)
        return list(dict.fromkeys(term for term in terms if len(term) >= 2))

    @staticmethod
    def _safe_structured_context(value: Any) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: Dict[str, Any] = {}
        for key in SAFE_STRUCTURED_CONTEXT_FIELDS:
            item = value.get(key)
            if isinstance(item, (str, int, float, bool)) or item is None:
                if item not in (None, ''):
                    result[key] = str(item)[:240] if isinstance(item, str) else item
            elif isinstance(item, list):
                result[key] = [
                    part for part in item[:16]
                    if isinstance(part, (str, int, float, bool))
                ]
        return result

    @staticmethod
    def _relevant_excerpt(
        card: Mapping[str, Any],
        terms: Sequence[str],
        *,
        max_chars: int = 1200,
    ) -> tuple[str, int]:
        """Retrieve bounded snippets without copying a whole uploaded file.

        Uploaded ``traditional_text`` can approach the 50 MB request limit.
        It remains local evidence, but only the highest-scoring fixed windows
        are exposed to any debate prompt.  The scan keeps three windows in
        memory rather than materialising tens of thousands of chunks.
        """

        structured = card.get('structured')
        structured = structured if isinstance(structured, Mapping) else {}
        full_text = str(
            structured.get('traditional_text')
            or structured.get('structured_markdown')
            or card.get('excerpt')
            or ''
        )
        if len(full_text) <= max_chars:
            return full_text, sum(full_text.lower().count(term) for term in terms)

        window_size = max_chars
        stride = max(200, window_size - 160)
        candidates: List[tuple[int, int, str]] = []
        lowered = full_text.lower()
        for start in range(0, len(full_text), stride):
            window = full_text[start:start + window_size]
            if not window:
                break
            window_lower = lowered[start:start + window_size]
            score = sum(window_lower.count(term.lower()) for term in terms)
            candidate = (score, -start, window)
            if score > 0 or not candidates:
                candidates.append(candidate)
                candidates.sort(reverse=True)
                del candidates[3:]
            if start + window_size >= len(full_text):
                break
        selected = sorted(candidates, key=lambda item: -item[1])
        combined = '\n…\n'.join(item[2].strip() for item in selected if item[2].strip())
        # At most two short fragments are sent for one evidence item.
        return combined[: max_chars * 2], max((item[0] for item in candidates), default=0)

    def _evidence_context(self) -> List[Dict[str, Any]]:
        if self._evidence_context_cache is not None:
            return [dict(item) for item in self._evidence_context_cache]

        terms = self._context_terms()
        fact_evidence = {fact.evidence_uid for fact in self.facts}
        ranked: List[tuple[int, int, Dict[str, Any]]] = []
        source_priority = {
            'financial_report': 8,
            'announcement': 6,
            'uploaded_document': 6,
            'industry_data': 3,
            'research_report': 2,
            'news': 1,
        }
        for index, item in enumerate(self.evidence_items):
            card = item['card']
            excerpt, relevance = self._relevant_excerpt(card, terms)
            uid = str(item['evidence_uid'])
            score = source_priority.get(str(card.get('source_type') or ''), 0)
            score += min(12, relevance * 2)
            if uid in fact_evidence:
                score += 10
            ranked.append((score, -index, {
                'evidence_uid': uid,
                'display_id': item['display_id'],
                'source_type': card.get('source_type'),
                'title': str(card.get('title') or '')[:240],
                'symbol': card.get('symbol'),
                'publish_time': card.get('publish_time'),
                'excerpt': excerpt,
                'structured': self._safe_structured_context(card.get('structured')),
            }))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        self._evidence_context_cache = [item[2] for item in ranked[:32]]
        return [dict(item) for item in self._evidence_context_cache]

    def _facts_context(self) -> List[Dict[str, Any]]:
        selected_uids = {
            str(item.get('evidence_uid') or '') for item in self._evidence_context()
        }
        selected = [
            fact for fact in self.facts if fact.evidence_uid in selected_uids
        ]
        return [fact.to_dict() for fact in (selected or self.facts)[:96]]

    @staticmethod
    def _json_example(stage: str) -> str:
        if stage == 'judge':
            return json.dumps({
                'accepted_claim_ids': ['clm_r1_quality_001'],
                'evidence_requests': [{
                    'dimension': '盈利质量',
                    'description': '缺少某项同口径披露',
                    'reason': 'judge_identified_gap',
                    'requested_fields': ['period', 'public_disclosure'],
                }],
            }, ensure_ascii=False)
        return json.dumps({
            'claims': [{
                'claim_id': 'optional-id', 'dimension': '盈利质量',
                'assertion': '300000在2025-06-30（H1，CNY/亿元，cumulative，consolidated）的营业收入为100亿元。',
                'evidence_uids': ['ev_xxx'], 'fact_uids': ['fact_xxx'],
                'fact_assertions': [{
                    'fact_uid': 'fact_xxx', 'subject': '300000',
                    'metric': '营业收入', 'value': '100', 'unit': '亿元',
                    'currency': 'CNY', 'period': '2025-06-30',
                    'period_type': 'H1', 'accumulation_basis': 'cumulative',
                    'consolidation_scope': 'consolidated',
                }],
                'supporting_quotes': {'ev_xxx': ['营业收入 100 亿元']},
                'assumptions': [], 'status': 'proposed',
            }],
            'challenges': [{
                'target_claim_id': 'clm_xxx', 'challenge_type': 'counterevidence',
                'argument': '反证内容', 'evidence_uids': ['ev_xxx'],
                'fact_uids': ['fact_xxx'],
                'fact_assertions': [{
                    'fact_uid': 'fact_xxx', 'subject': '300000',
                    'metric': '营业收入', 'value': '100', 'unit': '亿元',
                    'currency': 'CNY', 'period': '2025-06-30',
                    'period_type': 'H1', 'accumulation_basis': 'cumulative',
                    'consolidation_scope': 'consolidated',
                }],
                'fact_basis_statement': '300000在2025-06-30（H1，CNY/亿元，cumulative，consolidated）的营业收入为100亿元。',
                'supporting_quotes': {'ev_xxx': ['支持反证的原文片段']},
            }],
            'challenge_responses': [{
                'challenge_id': 'chg_xxx', 'response': '回应内容',
                'resolution_status': 'resolved',
            }],
        }, ensure_ascii=False)

    def _reasoning_kwargs_for_method(
        self,
        method: Callable[..., Any],
        kwargs: Mapping[str, Any],
        *,
        provider: str,
        model: str,
    ) -> Dict[str, Any]:
        """Resolve legacy fake capability before making exactly one call.

        Runtime ``TypeError`` is never interpreted as a signature mismatch: it
        may have been raised after a provider request was already submitted.
        Real/configured clients must accept both reasoning controls.  Only an
        inspectable offline injected fake (no provider/model metadata) may use
        the pre-thinking legacy signature.
        """

        resolved = dict(kwargs)
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # Unknown callables receive the full secure contract; if they do
            # not support it, their pre-call TypeError propagates once.
            return resolved
        parameters = signature.parameters
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return resolved
        keyword_names = {
            name for name, parameter in parameters.items()
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        }
        missing = [
            name for name in ('thinking', 'reasoning_effort')
            if name not in keyword_names
        ]
        if missing and (provider or model):
            raise RuntimeError(
                'Configured debate LLM client must support thinking=True and '
                f'reasoning_effort=high (missing: {", ".join(missing)})'
            )
        for name in missing:
            resolved.pop(name, None)
        return resolved

    def _invoke_once(self, messages: List[Dict[str, str]], *, agent: str) -> Dict[str, Any]:
        if self.llm is None:
            raise RuntimeError('TEXT_LLM_API_KEY 未配置')
        kwargs = {
            'temperature': None,
            'max_tokens': 4096,
            'max_attempts': 1,
            'thinking': True,
            'reasoning_effort': 'high',
        }
        if self.deadline_epoch is not None:
            from ..utils.run_limits import ensure_time_remaining
            ensure_time_remaining(self.deadline_epoch, stage=f'debate:{agent}')
        provider = str(getattr(self.llm, 'provider', None) or '')
        model = str(getattr(self.llm, 'model', None) or '')
        method = getattr(self.llm, 'chat_json_result', None)
        result_method = callable(method)
        if not result_method:
            method = getattr(self.llm, 'chat_json', None)
        if not callable(method):
            raise RuntimeError('LLM client does not expose chat_json')
        call_kwargs = self._reasoning_kwargs_for_method(
            method,
            kwargs,
            provider=provider,
            model=model,
        )
        if provider or model:
            from ..utils.llm_audit import ensure_llm_call_budget
            ensure_llm_call_budget(
                self.run_id,
                provider=provider,
                model=model,
                messages=messages,
                max_tokens=kwargs['max_tokens'],
                attempts=1 + int(getattr(self.llm, 'max_retries', 0) or 0),
            )
        if result_method:
            try:
                result = method(messages, **call_kwargs)
            except Exception as error:
                if getattr(error, 'budget_reservation_id', None):
                    from ..utils.llm_audit import record_llm_client_error
                    record_llm_client_error(self.run_id, agent, self.llm, error)
                raise
            try:
                from ..utils.llm_audit import record_llm_result
                record_llm_result(self.run_id, agent, result)
            except Exception:
                # A real reservation must never be silently released or left
                # unreported.  Propagate settlement failure into the existing
                # debate correction/fallback path; injected clients without a
                # reservation retain their lightweight test compatibility.
                if getattr(result, 'budget_reservation_id', None):
                    raise
            payload = getattr(result, 'parsed_json', None)
            if payload is None:
                payload = getattr(result, 'data', None)
            if payload is None and isinstance(result, Mapping):
                payload = result
        else:
            payload = method(messages, **call_kwargs)
        if not isinstance(payload, Mapping):
            raise ValueError('LLM JSON response must be an object')
        return dict(payload)

    def _call_stage(self, stage: str, role: str, instruction: str, state: Dict[str, Any]) -> Dict[str, Any]:
        system = (
            f'你是成竹的「{role}」。只可使用本消息提供的冻结证据与 FinancialFact，'
            '不得联网、不得调用工具、不得补造事实，也不要输出思维过程。所有事实必须引用稳定 '
            'evidence_uid，并提供能在该证据原文中精确找到的 supporting_quotes；'
            '所有数字还必须引用 fact_uid，且在 fact_assertions 中逐字段复述 '
            'subject/metric/value/unit/currency/period/period_type/accumulation_basis/'
            'consolidation_scope。只要引用 FinancialFact，assertion 必须严格按以下模板逐事实输出，'
            '多个事实用分号连接，禁止添加推断：'
            '“subject在period（period_type，currency/unit，accumulation_basis，'
            'consolidation_scope）的metric为valueunit。”推断只能写入 assumptions '
            '或 Challenge，不得混入 assertion。assumptions 只允许清晰标注的非事实分析前提；'
            '不得在其中写投资建议、无证据事实或无证据预测。'
            '禁止投资建议、价格预测、看多看空或 Alpha 信号。'
            f'最多研究四个维度：{self.dimensions}。只输出单个 JSON 对象。'
            f'JSON 示例：{self._json_example(stage)}'
        )
        payload = {
            'instruction': instruction,
            'dimensions': self.dimensions,
            'evidence': self._evidence_context(),
            'financial_facts': self._facts_context(),
            'debate_state': state,
        }
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        try:
            return self._invoke_once(messages, agent=stage)
        except Exception as first_error:
            self._record_llm_error(stage, first_error)
            if self._corrections_used >= self.max_corrections:
                from ..utils.llm_audit import safe_error_summary
                raise DebateOrchestrationError(
                    stage, safe_error_summary(first_error)
                ) from first_error
            self._corrections_used += 1
            messages.append({
                'role': 'user',
                'content': (
                    '上次输出为空、截断或不符合 JSON 契约。请依据同一冻结证据重新生成一次；'
                    '不得增加新事实。只输出一个完整 JSON 对象。'
                ),
            })
            try:
                return self._invoke_once(messages, agent=stage)
            except Exception as second_error:
                self._record_llm_error(stage, second_error)
                from ..utils.llm_audit import safe_error_summary
                raise DebateOrchestrationError(
                    stage, safe_error_summary(second_error)
                ) from second_error

    def _record_llm_error(self, agent: str, error: Exception) -> None:
        try:
            from ..utils.llm_audit import record_llm_error
            record_llm_error(
                self.run_id,
                agent,
                provider=str(
                    getattr(self.llm, 'provider', None)
                    or getattr(Config, 'TEXT_LLM_PROVIDER', '')
                ),
                model=str(
                    getattr(self.llm, 'model', None)
                    or getattr(Config, 'TEXT_LLM_REASONING_MODEL', '')
                ),
                error=error,
            )
        except Exception:
            if getattr(error, 'budget_reservation_id', None):
                raise

    def _canonical_claim_refs(self, claim: ClaimCard) -> None:
        claim.evidence_uids = self.auditor.canonical_evidence_uids(claim.evidence_uids)
        claim.supporting_quotes = self.auditor.canonical_supporting_quotes(
            claim.supporting_quotes
        )

    def _ingest_claims(self, payload: Mapping[str, Any], *, role: str, round_number: int, stage: str) -> List[ClaimCard]:
        raw_claims = payload.get('claims') or payload.get('revisions') or []
        if isinstance(raw_claims, Mapping):
            raw_claims = [raw_claims]
        if not isinstance(raw_claims, list):
            raise DebateOrchestrationError(stage, 'claims must be a list')
        used = {claim.claim_id for claim in self.claims}
        existing = {claim.claim_id: claim for claim in self.claims}
        added: List[ClaimCard] = []
        for raw in raw_claims[: max(4, len(self.dimensions) * 2)]:
            if not isinstance(raw, Mapping):
                continue
            source = dict(raw)
            referenced_existing = str(source.get('parent_claim_id') or '')
            if not referenced_existing and str(source.get('claim_id') or '') in existing:
                referenced_existing = str(source.get('claim_id'))
            if referenced_existing:
                owner = existing.get(referenced_existing)
                # A role may challenge the other side, but it may only revise
                # or withdraw a ClaimCard that it originally authored.
                if owner is None or owner.role != role:
                    continue
            requested_id = source.get('claim_id') if not referenced_existing else source.get('new_claim_id')
            source['claim_id'] = self._safe_id(requested_id, 'claim', stage, used)
            source['parent_claim_id'] = referenced_existing or source.get('parent_claim_id')
            source['role'] = role
            source['round'] = round_number
            claim = ClaimCard.from_dict(source)
            if not claim.assertion.strip():
                # A status-only withdrawal is a valid second-round action and
                # must not be lost merely because it adds no new narrative.
                parent = existing.get(referenced_existing)
                if parent and claim.status == ClaimStatus.WITHDRAWN:
                    parent.status = ClaimStatus.WITHDRAWN
                continue
            if claim.dimension not in self.dimensions:
                continue
            if claim.status == ClaimStatus.ACCEPTED:
                claim.status = ClaimStatus.PROPOSED
            if referenced_existing:
                parent = existing.get(referenced_existing)
                if parent:
                    parent.status = (
                        ClaimStatus.WITHDRAWN
                        if claim.status == ClaimStatus.WITHDRAWN
                        else ClaimStatus.REVISED
                    )
                    if claim.status not in {ClaimStatus.WITHDRAWN, ClaimStatus.REJECTED}:
                        # ``REVISED`` marks the superseded parent.  The new
                        # version must remain judge-eligible after auditing.
                        claim.status = ClaimStatus.PROPOSED
            self._canonical_claim_refs(claim)
            used.add(claim.claim_id)
            self.claims.append(claim)
            added.append(claim)
        return added

    def _apply_claim_status_updates(self, payload: Mapping[str, Any], *, role: str) -> None:
        by_id = {claim.claim_id: claim for claim in self.claims}
        withdrawn = payload.get('withdrawn_claim_ids') or []
        if isinstance(withdrawn, str):
            withdrawn = [withdrawn]
        if isinstance(withdrawn, list):
            for claim_id in withdrawn:
                claim = by_id.get(str(claim_id))
                if claim and claim.role == role:
                    claim.status = ClaimStatus.WITHDRAWN

        updates = payload.get('claim_updates') or []
        if isinstance(updates, Mapping):
            updates = [updates]
        if not isinstance(updates, list):
            return
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            claim = by_id.get(str(update.get('claim_id') or ''))
            if not claim:
                continue
            if claim.role != role:
                continue
            try:
                status = ClaimStatus(str(update.get('status') or ''))
            except ValueError:
                continue
            # Agents may withdraw or dispute their own assertion.  Only the
            # judge may set ACCEPTED and only the auditor may set REJECTED.
            if status in {ClaimStatus.WITHDRAWN, ClaimStatus.DISPUTED}:
                claim.status = status

    def _ingest_challenges(self, payload: Mapping[str, Any], *, role: str, round_number: int, stage: str) -> List[Challenge]:
        raw_items = payload.get('challenges') or []
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            raise DebateOrchestrationError(stage, 'challenges must be a list')
        claim_ids = {claim.claim_id for claim in self.claims}
        used = {challenge.challenge_id for challenge in self.challenges}
        added: List[Challenge] = []
        for raw in raw_items[: max(8, len(self.dimensions) * 2)]:
            if not isinstance(raw, Mapping):
                continue
            source = dict(raw)
            target = str(source.get('target_claim_id') or '')
            if target not in claim_ids:
                continue
            source['challenge_id'] = self._safe_id(source.get('challenge_id'), 'challenge', stage, used)
            source['role'] = role
            source['round'] = round_number
            challenge = Challenge.from_dict(source)
            if not challenge.argument.strip():
                continue
            challenge.evidence_uids = self.auditor.canonical_evidence_uids(challenge.evidence_uids)
            target_claim = next(claim for claim in self.claims if claim.claim_id == target)
            if target_claim.status not in {ClaimStatus.WITHDRAWN, ClaimStatus.REJECTED}:
                target_claim.status = ClaimStatus.CHALLENGED
            used.add(challenge.challenge_id)
            self.challenges.append(challenge)
            added.append(challenge)
        return added

    def _apply_responses(self, payload: Mapping[str, Any], *, role: str) -> None:
        raw_items = payload.get('challenge_responses') or payload.get('responses') or []
        if isinstance(raw_items, Mapping):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            return
        by_id = {challenge.challenge_id: challenge for challenge in self.challenges}
        claims_by_id = {claim.claim_id: claim for claim in self.claims}
        by_target: Dict[str, List[Challenge]] = defaultdict(list)
        for challenge in self.challenges:
            by_target[challenge.target_claim_id].append(challenge)
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            challenge = by_id.get(str(raw.get('challenge_id') or ''))
            if challenge is None and raw.get('target_claim_id'):
                candidates = by_target.get(str(raw.get('target_claim_id')), [])
                challenge = candidates[-1] if candidates else None
            if challenge is None:
                continue
            target = claims_by_id.get(challenge.target_claim_id)
            if target and target.role == role:
                if raw.get('response'):
                    challenge.response = str(raw.get('response') or '')
                    challenge.response_role = role
                response_claim_id = raw.get('response_claim_id')
                response_claim = claims_by_id.get(str(response_claim_id or ''))
                if response_claim and response_claim.role == role:
                    challenge.response_claim_id = response_claim.claim_id
                # The target may answer, revise or withdraw, but cannot declare
                # the counterevidence dismissed.
                continue
            if challenge.role != role:
                continue
            status = raw.get('resolution_status') or raw.get('status')
            if status:
                try:
                    challenge.resolution_status = ChallengeStatus(str(status))
                    challenge.resolution_role = role
                except ValueError:
                    challenge.resolution_status = ChallengeStatus.OPEN

    def _state(self) -> Dict[str, Any]:
        return {
            'claims': [claim.to_dict() for claim in self.claims],
            'challenges': [challenge.to_dict() for challenge in self.challenges],
        }

    def _persist_transcript(self) -> None:
        _atomic_jsonl(self.debate_dir / 'claims.jsonl', self.claims)
        _atomic_jsonl(self.debate_dir / 'challenges.jsonl', self.challenges)

    def _persist_audit(self) -> None:
        _atomic_jsonl(self.debate_dir / 'audit.jsonl', self.audit_scores)
        _atomic_jsonl(self.debate_dir / 'challenge_audit.jsonl', self.challenge_audits)

    def _degraded(self, reason: str) -> DebateVerdict:
        if self.claims:
            self.audit_scores = self.auditor.audit_all(self.claims, self.challenges)
            self.challenge_audits = list(self.auditor.challenge_audits)
            valid_ids = {
                item['challenge_id'] for item in self.challenge_audits if item['hard_pass']
            }
            self.valid_challenges = [
                challenge for challenge in self.challenges
                if challenge.challenge_id in valid_ids
            ]
        verdict = DebateVerdict(
            status=VerdictStatus.DEGRADED,
            generated_by='orchestrator_degraded',
            degradation_reason=reason,
            unresolved_disputes=[claim.assertion for claim in self.claims if claim.status == ClaimStatus.DISPUTED],
            withdrawn_claims=[claim.assertion for claim in self.claims if claim.status == ClaimStatus.WITHDRAWN],
            evidence_requests=[EvidenceRequest(
                dimension='全局',
                description='辩论未完成，需要在同一冻结快照上重新裁决。',
                reason='debate_incomplete',
                requested_fields=['valid_debate_verdict'],
            )],
            follow_up_public_items=[],
        )
        self._persist_transcript()
        self._persist_audit()
        _atomic_json(self.debate_dir / 'verdict.json', verdict.to_dict())
        self._notify('degraded', reason=reason)
        return verdict

    def run(self) -> DebateVerdict:
        self.debate_dir.mkdir(parents=True, exist_ok=True)
        if self.llm is None:
            reason = self.unavailable_reason or 'TEXT_LLM_API_KEY 未配置，辩论未执行'
            if self.degrade_on_failure:
                return self._degraded(reason)
            raise DebateOrchestrationError('initialization', reason)
        if not self.evidence_items:
            reason = '冻结证据快照为空'
            if self.degrade_on_failure:
                return self._degraded(reason)
            raise DebateOrchestrationError('initialization', reason)

        try:
            self._notify('debating', round=1, role=QUALITY_ROLE)
            first = self._call_stage(
                'r1_quality', QUALITY_ROLE,
                '第一轮：从现金流、盈利质量、资产负债和经营稳健性提出初始 Claim。',
                self._state(),
            )
            if not self._ingest_claims(first, role=QUALITY_ROLE, round_number=1, stage='r1_quality'):
                raise DebateOrchestrationError('r1_quality', 'no valid claims')
            self._apply_claim_status_updates(first, role=QUALITY_ROLE)
            self._persist_transcript()

            self._notify('debating', round=1, role=GROWTH_ROLE)
            second = self._call_stage(
                'r1_growth', GROWTH_ROLE,
                '第一轮：提出增长驱动、业务变化和可持续性 Claim，并逐项挑战对方可疑 Claim。',
                self._state(),
            )
            if not self._ingest_claims(second, role=GROWTH_ROLE, round_number=1, stage='r1_growth'):
                raise DebateOrchestrationError('r1_growth', 'no valid claims')
            self._ingest_challenges(second, role=GROWTH_ROLE, round_number=1, stage='r1_growth')
            self._apply_claim_status_updates(second, role=GROWTH_ROLE)
            self._persist_transcript()

            self._notify('debating', round=2, role=QUALITY_ROLE)
            third = self._call_stage(
                'r2_quality', QUALITY_ROLE,
                '第二轮：回应挑战；有反证时必须修订或撤回原 Claim，可提交 parent_claim_id 指向旧版本。',
                self._state(),
            )
            self._ingest_claims(third, role=QUALITY_ROLE, round_number=2, stage='r2_quality')
            self._ingest_challenges(third, role=QUALITY_ROLE, round_number=2, stage='r2_quality')
            self._apply_responses(third, role=QUALITY_ROLE)
            self._apply_claim_status_updates(third, role=QUALITY_ROLE)
            self._persist_transcript()

            self._notify('debating', round=2, role=GROWTH_ROLE)
            fourth = self._call_stage(
                'r2_growth', GROWTH_ROLE,
                '第二轮最终回应：解决、维持或升级反证；必要时修订或撤回 Claim。',
                self._state(),
            )
            self._ingest_claims(fourth, role=GROWTH_ROLE, round_number=2, stage='r2_growth')
            self._ingest_challenges(fourth, role=GROWTH_ROLE, round_number=2, stage='r2_growth')
            self._apply_responses(fourth, role=GROWTH_ROLE)
            self._apply_claim_status_updates(fourth, role=GROWTH_ROLE)

            self._notify('adjudicating', round=2, role='EvidenceAuditor')
            self.audit_scores = self.auditor.audit_all(self.claims, self.challenges)
            self.challenge_audits = list(self.auditor.challenge_audits)
            valid_challenge_ids = {
                item['challenge_id'] for item in self.challenge_audits if item['hard_pass']
            }
            self.valid_challenges = [
                challenge for challenge in self.challenges
                if challenge.challenge_id in valid_challenge_ids
            ]
            self._persist_transcript()
            self._persist_audit()

            self._notify('adjudicating', round=2, role='Judge/Synthesizer')
            judge_state = {
                'claims': [claim.to_dict() for claim in self.claims],
                'challenges': [challenge.to_dict() for challenge in self.valid_challenges],
                'audit': [score.to_dict() for score in self.audit_scores],
                'challenge_audit': self.challenge_audits,
                'instruction_guard': '只能接受 hard_pass=true 且无未决反证的 claim_id。',
            }
            synthesizer = JudgeSynthesizer()
            try:
                judge_payload = self._call_stage(
                    'judge', 'Judge/Synthesizer',
                    '综合已通过硬校验的 Claim。输出 accepted_claim_ids 与结构化 evidence_requests；缺口不得写成事实。',
                    judge_state,
                )
                verdict = synthesizer.from_judge_payload(
                    judge_payload, self.claims, self.audit_scores, self.valid_challenges
                )
            except DebateOrchestrationError as judge_error:
                # The deterministic auditor remains authoritative, but it is
                # not a substitute for the configured Judge. Exhausting the
                # single correction allowance therefore triggers the pipeline
                # contract: same-snapshot direct fallback with disclosure.
                return self._degraded(str(judge_error))
            self._persist_transcript()
            _atomic_json(self.debate_dir / 'verdict.json', verdict.to_dict())
            self._notify(
                'completed',
                accepted=len(verdict.accepted_claim_ids),
                claims=len(self.claims),
                challenges=len(self.challenges),
                audit_failures=sum(not score.hard_pass for score in self.audit_scores),
            )
            return verdict
        except DebateOrchestrationError as error:
            if self.degrade_on_failure:
                return self._degraded(str(error))
            raise
        except Exception as error:
            from ..utils.llm_audit import safe_error_summary
            wrapped = DebateOrchestrationError(
                'unexpected', safe_error_summary(error)
            )
            if self.degrade_on_failure:
                return self._degraded(str(wrapped))
            raise wrapped from error

    @staticmethod
    def replay(run_dir: os.PathLike) -> DebateVerdict:
        """Replay a demo/offline run without creating an LLM client."""

        return replay_debate(run_dir)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_debate_artifacts(run_dir: os.PathLike) -> Dict[str, Any]:
    """Load persisted debate state for the API or demo_seed playback."""

    debate_dir = Path(run_dir) / 'debate'
    verdict_path = debate_dir / 'verdict.json'
    return {
        'claims': _read_jsonl(debate_dir / 'claims.jsonl'),
        'challenges': _read_jsonl(debate_dir / 'challenges.jsonl'),
        'audit': _read_jsonl(debate_dir / 'audit.jsonl'),
        'challenge_audit': _read_jsonl(debate_dir / 'challenge_audit.jsonl'),
        'verdict': (
            json.loads(verdict_path.read_text(encoding='utf-8'))
            if verdict_path.is_file() else None
        ),
    }


def replay_debate(run_dir: os.PathLike) -> DebateVerdict:
    """Return a previously persisted verdict, requiring no API key."""

    artifacts = load_debate_artifacts(run_dir)
    if not isinstance(artifacts.get('verdict'), Mapping):
        raise FileNotFoundError(f'缺少辩论裁决产物: {Path(run_dir) / "debate" / "verdict.json"}')
    return DebateVerdict.from_dict(artifacts['verdict'])


__all__ = [
    'DebateOrchestrationError', 'DebateOrchestrator', 'EvidenceAuditor',
    'JudgeSynthesizer', 'load_debate_artifacts', 'replay_debate',
]
