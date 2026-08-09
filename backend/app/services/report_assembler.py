"""报告装配：章节 + 自动生成「信息来源清单」「风险与关注点」+ Markdown。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set

from ..config import Config
from ..constants import DISCLAIMER
from ..models.research_task import task_artifact_folder
from ..utils.report_commit import (
    REPORT_COMMIT,
    REPORT_FILES,
    REPORT_PUBLISH_STARTED,
    build_report_commit,
    report_bundle_is_committed,
)
from .compliance_checker import check_chart_blocks, check_compliance, strip_advice_phrases
from .evidence_store import EvidenceStore

CITATION_RE = re.compile(r'\[E(\d+)\]')
CHART_RE = re.compile(r'```chart\s*(\{.*?\})\s*```', re.DOTALL)


def _atomic_write_text(path: str, content: str) -> None:
    """Write a latest/legacy artifact atomically without exposing partial bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp-{uuid.uuid4().hex}'
    try:
        with open(tmp, 'x', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_immutable_text(path: str, content: str) -> None:
    """Publish a run artifact exactly once; an existing run remains immutable."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f'.{os.path.basename(path)}-{uuid.uuid4().hex}.tmp')
    try:
        with open(tmp, 'x', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    except FileExistsError as exc:
        raise FileExistsError(f'run 产物已发布，禁止覆盖: {path}') from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _collect_cited_ids(sections: List[Dict[str, Any]]) -> Set[int]:
    ids: Set[int] = set()
    for s in sections:
        for m in CITATION_RE.findall(s.get('content') or ''):
            ids.add(int(m))
    return ids


def _as_list(value: Any) -> List[Any]:
    if value in (None, ''):
        return []
    return value if isinstance(value, list) else [value]


def _verdict_item_markdown(item: Any, store: EvidenceStore) -> str:
    if isinstance(item, dict):
        text = str(
            item.get('statement')
            or item.get('text')
            or item.get('description')
            or item.get('claim')
            or ''
        ).strip()
        refs = (
            item.get('evidence_uids')
            or item.get('evidence_refs')
            or item.get('evidence_ids')
            or []
        )
        rendered_refs: List[str] = []
        for ref in _as_list(refs):
            value = ref.get('evidence_uid') if isinstance(ref, dict) else ref
            card = store.get(str(value))
            if card:
                rendered_refs.append(f'[{store.display_id(card)}]')
            elif str(value).upper().startswith('E'):
                rendered_refs.append(f'[{str(value).upper()}]')
        return (text or '（未提供文本）') + (''.join(dict.fromkeys(rendered_refs)))
    return str(item)


def _ensure_debate_sections(
    sections: List[Dict[str, Any]],
    reviewed: Dict[str, Any],
    store: EvidenceStore,
) -> List[Dict[str, Any]]:
    if reviewed.get('analysis_mode') != 'evidence_debate':
        return sections
    if reviewed.get('claim_gate_enforced'):
        # AgentTeams report prose has already been rebuilt exclusively from
        # accepted, hard-pass ClaimCards. The full verdict may describe
        # rejected or unresolved material and must not be expanded into the
        # formal candidate behind the Writer gate.
        return sections
    if reviewed.get('debate_status') == 'fallback_direct':
        reason = reviewed.get('debate_fallback_reason') or '模型调用失败或未形成有效裁决'
        return [{
            'title': '辩论运行说明',
            'goal': '披露降级状态',
            'content': f'本次证据辩论未完成（{reason}），报告已基于同一冻结证据快照降级为直接分析。',
            'verdict': 'warning',
            'system': True,
        }, *sections]

    verdict = reviewed.get('debate_verdict') or {}
    existing = {str(section.get('title') or '') for section in sections}
    specs = [
        ('共识事实', verdict.get('consensus_facts')),
        ('主要反证', verdict.get('major_challenges') or verdict.get('key_challenges')),
        ('未决分歧', verdict.get('unresolved_disagreements') or verdict.get('unresolved_disputes') or verdict.get('disagreements')),
        ('证据不足', verdict.get('evidence_gaps')),
        ('后续公开事项', verdict.get('follow_up_public_events') or verdict.get('follow_up_public_items') or verdict.get('follow_ups')),
    ]
    required: List[Dict[str, Any]] = []
    for title, values in specs:
        if title in existing:
            continue
        items = _as_list(values)
        content = '\n'.join(f'- {_verdict_item_markdown(item, store)}' for item in items)
        required.append({
            'title': title,
            'goal': '来自证据辩论裁决',
            'content': content or '- 当前冻结证据中暂无可裁决内容。',
            'verdict': 'pass',
            'audited_debate': True,
        })
    return [*required, *sections]


def _integrity_note(reason: str) -> str:
    code = str(reason or '').lower()
    if 'warehouse_stale' in code:
        return 'Datayes 本地仓库水位已过期；相关结果来自实时补鲜或降级数据。'
    if 'warehouse_unavailable' in code:
        return 'Datayes 本地仓库不可用；相关能力未使用本地历史数据。'
    if 'token_missing' in code or 'no_token' in code:
        return 'Datayes Token 未配置，实时 DataAPI 不可用。'
    if any(key in code for key in (
        'permission', 'forbidden', 'unauthorized', '403', '权限', '鉴权',
    )):
        return 'Datayes DataAPI 权限校验失败，相关接口结果不完整。'
    if any(key in code for key in (
        'retcode=-16', 'retcode:-16', 'rate', '限流', '频率超限',
    )):
        return 'Datayes DataAPI 触发限流，相关结果已降级并等待重试。'
    if any(key in code for key in (
        'api_failed', 'timeout', 'network', 'retcode=-5', 'retcode:-5',
        'retcode=-7', 'retcode:-7', 'temporarily unavailable', '暂时不可用',
    )):
        return 'Datayes DataAPI 调用失败或超时，相关结果已降级。'
    if 'datayes_no_rows' in code:
        return 'Datayes 未返回可用记录，相关能力已尝试公共数据源。'
    if 'datayes_disabled' in code or 'api_disabled_by_mode' in code:
        return '当前配置未启用 Datayes 实时 API。'
    if 'public_fallback' in code:
        return '相关结构化能力使用了公共数据源降级结果。'
    return f'数据源记录了降级原因：{reason}'


def _collect_integrity_notes(store: EvidenceStore) -> List[str]:
    notes: List[str] = []
    for card in store.cards:
        public = card.to_dict()
        structured = public.get('structured') or {}
        reasons: List[Any] = []
        for key in ('degradation_reasons', 'datayes_degradation_reasons'):
            value = structured.get(key)
            if isinstance(value, (list, tuple, set)):
                reasons.extend(value)
            elif value:
                reasons.append(value)
        provenance = public.get('provenance') or {}
        if provenance.get('provider') == 'public_fallback':
            reasons.append('public_fallback')
        if structured.get('degraded') and not reasons:
            reasons.append('unspecified_degradation')
        if (
            structured.get('visual_parse_incomplete')
            or structured.get('vision_parse_complete') is False
            or structured.get('vision_status') in {'failed', 'partial'}
        ):
            note = '部分图片或扫描页未能完成视觉解析，报告仅保留传统文本解析结果。'
            if note not in notes:
                notes.append(note)
        for reason in reasons:
            note = _integrity_note(str(reason))
            if note not in notes:
                notes.append(note)
    return notes


def _final_report_gate(
    sections: List[Dict[str, Any]],
    store: EvidenceStore,
    artifact_folder: str,
) -> List[Dict[str, Any]]:
    """Sanitize prohibited language and reject invalid citations/charts."""

    valid_ids = {int(card.card_id) for card in store.cards if card.card_id is not None}
    evidence_blobs = {
        int(card.card_id): (
            f'{card.title}\n{card.excerpt}\n'
            f'{json.dumps(card.structured, ensure_ascii=False)}'
        )
        for card in store.cards if card.card_id is not None
    }
    for section in sections:
        for field in ('title', 'goal'):
            value = str(section.get(field) or '')
            if check_compliance(value):
                section[field] = strip_advice_phrases(value)
                section['verdict'] = 'revise'
        content = str(section.get('content') or '')
        if check_compliance(content):
            content = strip_advice_phrases(content)
            section['content'] = content
            section['verdict'] = 'revise'
        invalid_ids = {
            int(value) for value in CITATION_RE.findall(content)
            if int(value) not in valid_ids
        }
        if invalid_ids:
            joined = ', '.join(f'E{value}' for value in sorted(invalid_ids))
            raise ValueError(f'报告含无效证据引用：{joined}')
        chart_issues = check_chart_blocks(content, evidence_blobs)
        if chart_issues:
            raise ValueError(f'报告图表未通过确定性校验：{chart_issues[0].get("detail")}')
        financial_chart_issue = _financial_chart_issue(content, store, artifact_folder)
        if financial_chart_issue:
            raise ValueError(f'报告图表未通过同口径校验：{financial_chart_issue}')
        financial_prose_issue = _financial_prose_issue(
            section,
            store,
            artifact_folder,
        )
        if financial_prose_issue:
            raise ValueError(f'报告正文未通过同口径校验：{financial_prose_issue}')
        evidence_issue = _direct_evidence_issue(section, store)
        if evidence_issue:
            raise ValueError(f'报告正文未通过证据相关性校验：{evidence_issue}')
    return sections


GENERIC_DIRECT_LINE_PREFIXES = (
    '本章围绕「', '（风格偏好：', '已采集证据中', '当前冻结证据中',
    '当前证据中未见', '暂无同口径数据',
)


def _direct_evidence_issue(
    section: Dict[str, Any],
    store: EvidenceStore,
) -> Optional[str]:
    """Require direct-report factual lines to be exact frozen-source excerpts."""

    if any(section.get(key) for key in (
        'system', 'deterministic_financial', 'audited_debate',
    )):
        return None
    content = CHART_RE.sub('', str(section.get('content') or ''))
    by_display = {store.display_id(card): card for card in store.cards}

    def compact(value: str) -> str:
        value = CITATION_RE.sub('', value or '')
        value = re.sub(r'[`*_>#|\-]+', '', value)
        value = value.rstrip('…')
        return re.sub(r'\s+', '', value).strip()

    for line in content.splitlines():
        raw = line.strip()
        if not raw or set(raw.replace('|', '').replace('-', '').replace(':', '').strip()) == set():
            continue
        plain = raw.lstrip('- ').strip()
        if plain.startswith(GENERIC_DIRECT_LINE_PREFIXES):
            continue
        refs = [f'E{value}' for value in CITATION_RE.findall(raw)]
        if not refs:
            return f'事实行缺少证据角标：{plain[:40]}'
        statement = compact(plain)
        if not statement:
            continue
        supported = False
        for ref in refs:
            card = by_display.get(ref)
            if not card:
                continue
            blobs = (
                str(card.excerpt or ''),
                str(card.title or ''),
            )
            if any(statement in compact(blob) for blob in blobs):
                supported = True
                break
        if not supported:
            return f'引用与原文不匹配：{plain[:40]}'
    return None


def _decimal_value(value: Any) -> Optional[Decimal]:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _financial_chart_issue(
    text: str,
    store: EvidenceStore,
    artifact_folder: str,
) -> Optional[str]:
    """Require every financial chart value to resolve to one exact fact signature."""

    matches = list(CHART_RE.finditer(text or ''))
    if not matches:
        return None
    facts_path = os.path.join(artifact_folder, 'normalized_facts.jsonl')
    facts: List[Dict[str, Any]] = []
    if os.path.isfile(facts_path):
        with open(facts_path, 'r', encoding='utf-8') as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    facts.append(value)

    display_to_card = {store.display_id(card): card for card in store.cards}
    bad_flags = {
        'missing_value', 'missing_unit', 'missing_currency', 'unknown_period',
        'unknown_accumulation_basis', 'unknown_consolidation_scope',
        'outside_time_window', 'missing_disclosure_time',
        'invalid_disclosure_time',
    }
    for match in matches:
        try:
            chart = json.loads(match.group(1))
        except ValueError:
            continue  # The generic chart gate reports malformed JSON.
        refs = [str(ref).upper() for ref in (chart.get('source_refs') or [])]
        referenced_uids = {
            str(display_to_card[ref].evidence_uid)
            for ref in refs
            if ref in display_to_card
        }
        series_items = chart.get('series') or []
        fact_metrics = {str(fact.get('metric') or '') for fact in facts}
        basis = chart.get('comparison_basis')
        is_financial = (
            isinstance(basis, dict)
            or any(isinstance(series, dict) and series.get('fact_uids') for series in series_items)
            or any(
                isinstance(series, dict) and str(series.get('name') or '') in fact_metrics
                for series in series_items
            )
        )
        if not is_financial:
            continue
        if not referenced_uids:
            return '财务图表没有可映射的冻结证据'
        if not isinstance(basis, dict):
            return '财务图表缺少 comparison_basis'

        fact_by_uid = {
            str(fact.get('fact_uid') or ''): fact for fact in facts
            if fact.get('fact_uid')
        }
        x_values = [str(value) for value in (chart.get('x') or [])]
        for series in chart.get('series') or []:
            metric = str(series.get('name') or '')
            series_values = series.get('data') or []
            fact_uids = series.get('fact_uids')
            if not isinstance(fact_uids, list) or len(fact_uids) != len(series_values):
                return f'{metric or "未命名序列"} 缺少与数据点一一对应的 fact_uids'
            signatures = set()
            for index, raw_value in enumerate(series_values):
                if raw_value is None:
                    continue
                value = _decimal_value(raw_value)
                fact = fact_by_uid.get(str(fact_uids[index] or ''))
                if not fact:
                    return f'{metric or "未命名序列"} 引用了不存在的 FinancialFact'
                fact_value = _decimal_value(fact.get('value'))
                if (
                    value is None
                    or fact_value is None
                    or abs(value - fact_value) > Decimal('0.005')
                    or str(fact.get('evidence_uid') or '') not in referenced_uids
                    or str(fact.get('metric') or '') != metric
                    or index >= len(x_values)
                    or str(fact.get('subject') or '') != x_values[index]
                    or bad_flags.intersection(set(fact.get('quality_flags') or []))
                ):
                    return f'{metric or "未命名序列"} 第 {index + 1} 个数据点与主体/事实/证据不一致'
                signatures.add((
                    str(fact.get('period') or ''),
                    str(fact.get('period_type') or ''),
                    str(fact.get('unit') or ''),
                    str(fact.get('currency') or ''),
                    str(fact.get('accumulation_basis') or ''),
                    str(fact.get('consolidation_scope') or ''),
                ))
            if len(signatures) != 1:
                return f'{metric or "未命名序列"} 混用了期间、单位、币种、累计口径或合并范围'
            signature = next(iter(signatures))
            expected = (
                str(basis.get('period') or ''),
                str(basis.get('period_type') or ''),
                str(basis.get('unit') or ''),
                str(basis.get('currency') or ''),
                str(basis.get('cumulative') or basis.get('accumulation_basis') or ''),
                str(basis.get('consolidation_scope') or ''),
            )
            if signature != expected:
                return f'{metric or "未命名序列"} 的 comparison_basis 与 FinancialFact 不一致'
    return None


FINANCIAL_TERM_RE = re.compile(
    r'营业(?:总)?收入|净利润|现金流|毛利|负债|资产|净资产|应收|存货|费用'
)
COMPARISON_TERM_RE = re.compile(
    r'对比|比较|高于|低于|较.+(?:高|低|增|减)|差异|同比|环比|领先|落后|'
    r'\bvs\b|百分点|\d+(?:\.\d+)?%',
    re.IGNORECASE,
)
NON_COMPARABLE_WARNING_RE = re.compile(
    r'不可比|不能(?:直接)?比较|口径不一致|暂无同口径数据|拒绝比较'
)
FINANCIAL_NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9_.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
    r'\s*(亿元|万元|元/股|人民币|美元|元|%|％|百分点|倍|次)'
)
TABLE_NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9_.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
    r'(?![A-Za-z0-9_.-])'
)


def _normalised_financial_unit(unit: str) -> str:
    value = str(unit or '').strip()
    return {
        '％': '%', '百分点': '%', '人民币': 'CNY', '美元': 'USD',
    }.get(value, value)


def _financial_number_tokens(
    content: str,
    *,
    subjects: Set[str],
    financial_table: bool,
) -> List[tuple[Decimal, str]]:
    """Extract report numbers that must resolve to cited FinancialFacts.

    Dates, citation labels and six-digit security codes are context rather
    than financial values.  Unit-bearing prose values are always checked;
    table cells are additionally checked using the unit declared in a header.
    """

    scrubbed = CITATION_RE.sub('', content or '')
    scrubbed = re.sub(r'\b(?:19|20)\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b', '', scrubbed)
    scrubbed = re.sub(r'\b(?:19|20)\d{2}(?=\s*(?:Q[1-4]|H[12]|FY|年))', '', scrubbed, flags=re.I)
    values: List[tuple[Decimal, str]] = []
    for match in FINANCIAL_NUMBER_RE.finditer(scrubbed):
        number = _decimal_value(match.group(1).replace(',', ''))
        if number is None:
            continue
        values.append((number, _normalised_financial_unit(match.group(2))))

    if not financial_table:
        return values

    subject_tokens = {str(value).strip() for value in subjects if str(value).strip()}
    lines = [line for line in scrubbed.splitlines() if line.strip().startswith('|')]
    if len(lines) < 3:
        return values
    header_unit = ''
    header = lines[0]
    header_match = re.search(r'亿元|万元|元/股|人民币|美元|元|%|％|百分点|倍|次', header)
    if header_match:
        header_unit = _normalised_financial_unit(header_match.group(0))
    for line in lines[2:]:
        # Unit-bearing numbers were already collected above.  Remove them so
        # a value is never counted twice.
        residual = FINANCIAL_NUMBER_RE.sub('', line)
        for match in TABLE_NUMBER_RE.finditer(residual):
            token = match.group(1).replace(',', '')
            if token in subject_tokens or (
                len(token) == 6 and token.isdigit() and token in subject_tokens
            ):
                continue
            number = _decimal_value(token)
            if number is not None:
                values.append((number, header_unit))
    return values


def _number_is_supported(
    number: Decimal,
    unit: str,
    facts: List[Dict[str, Any]],
) -> bool:
    """Validate an exact fact value or a deterministic two-fact delta."""

    tolerance = Decimal('0.005')
    normalised_unit = _normalised_financial_unit(unit)
    for fact in facts:
        fact_value = _decimal_value(fact.get('value'))
        fact_unit = _normalised_financial_unit(str(fact.get('unit') or ''))
        if fact_value is None:
            continue
        unit_matches = (
            not normalised_unit
            or normalised_unit == fact_unit
            or (normalised_unit == 'CNY' and str(fact.get('currency') or '') == 'CNY')
            or (normalised_unit == 'USD' and str(fact.get('currency') or '') == 'USD')
        )
        if unit_matches and abs(number - fact_value) <= tolerance:
            return True

    # A comparison may state a computed percentage/percentage-point delta.
    # It is accepted only when the cited, otherwise comparable source values
    # deterministically reproduce it.
    if normalised_unit != '%':
        return False
    grouped: Dict[tuple[str, ...], List[Decimal]] = {}
    for fact in facts:
        value = _decimal_value(fact.get('value'))
        if value is None:
            continue
        signature = (
            str(fact.get('metric') or ''), str(fact.get('period') or ''),
            str(fact.get('period_type') or ''), str(fact.get('unit') or ''),
            str(fact.get('currency') or ''),
            str(fact.get('accumulation_basis') or ''),
            str(fact.get('consolidation_scope') or ''),
        )
        grouped.setdefault(signature, []).append(value)
    for signature, source_values in grouped.items():
        if len(source_values) < 2:
            continue
        for left in source_values:
            for right in source_values:
                if left == right:
                    continue
                candidates = [abs(left - right)] if signature[3] == '%' else []
                if right != 0:
                    candidates.append(abs((left - right) / right * Decimal('100')))
                if any(abs(abs(number) - value) <= Decimal('0.05') for value in candidates):
                    return True
    return False


def _financial_prose_issue(
    section: Dict[str, Any],
    store: EvidenceStore,
    artifact_folder: str,
) -> Optional[str]:
    """Reject free-form cross-period financial comparisons outside charts.

    Direct deterministic financial sections are assembled exclusively from
    exact FinancialFact groups and are kept immutable by Reviewer. Other LLM
    prose may discuss context, but a numeric financial comparison must resolve
    to one exact signature from the cited frozen evidence.
    """

    content = CHART_RE.sub('', str(section.get('content') or ''))
    if not FINANCIAL_TERM_RE.search(content):
        return None
    table_lines = [
        line for line in content.splitlines() if line.strip().startswith('|')
    ]
    financial_table = len(table_lines) >= 3 and any(
        FINANCIAL_TERM_RE.search(line) for line in table_lines
    )
    explicit_comparison = COMPARISON_TERM_RE.search(content) is not None

    refs = {f'E{value}' for value in CITATION_RE.findall(content)}
    evidence_uids = {
        str(card.evidence_uid)
        for card in store.cards
        if store.display_id(card) in refs
    }
    facts_path = os.path.join(artifact_folder, 'normalized_facts.jsonl')
    facts: List[Dict[str, Any]] = []
    if os.path.isfile(facts_path):
        with open(facts_path, 'r', encoding='utf-8') as handle:
            for line in handle:
                try:
                    fact = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(fact, dict) and str(fact.get('evidence_uid') or '') in evidence_uids:
                    facts.append(fact)
    if section.get('deterministic_financial'):
        disclosure_flags = {'missing_disclosure_time', 'invalid_disclosure_time'}
        if any(
            disclosure_flags.intersection(set(fact.get('quality_flags') or []))
            for fact in facts
        ):
            return '财务正文引用了披露时点缺失或早于报告期末的 FinancialFact'
        return None
    if not facts:
        if financial_table or explicit_comparison:
            return '财务比较没有可定位的 FinancialFact'
        return None

    bad_flags = {
        'missing_value', 'missing_unit', 'missing_currency', 'unknown_period',
        'unknown_accumulation_basis', 'unknown_consolidation_scope',
        'outside_time_window', 'missing_disclosure_time',
        'invalid_disclosure_time',
    }
    usable = [
        fact for fact in facts
        if not bad_flags.intersection(set(fact.get('quality_flags') or []))
    ]
    if not usable:
        return '财务比较只引用了缺字段或时间窗外事实'

    numeric_values = _financial_number_tokens(
        content,
        subjects={str(fact.get('subject') or '') for fact in usable},
        financial_table=financial_table,
    )
    matched = []
    for fact in usable:
        fact_value = _decimal_value(fact.get('value'))
        fact_unit = _normalised_financial_unit(str(fact.get('unit') or ''))
        if fact_value is None:
            continue
        if any(
            abs(number - fact_value) <= Decimal('0.005')
            and (not unit or unit == fact_unit)
            for number, unit in numeric_values
        ):
            matched.append(fact)
    candidates = matched or usable
    subjects = {str(fact.get('subject') or '') for fact in candidates}
    signatures = {
        (
            str(fact.get('metric') or ''),
            str(fact.get('period') or ''),
            str(fact.get('period_type') or ''),
            str(fact.get('unit') or ''),
            str(fact.get('currency') or ''),
            str(fact.get('accumulation_basis') or ''),
            str(fact.get('consolidation_scope') or ''),
        )
        for fact in candidates
    }
    implicit_multi_fact = (
        len(subjects) >= 2
        and len(evidence_uids) >= 2
        and (len(matched) >= 2 or len(refs) >= 2)
    )
    if not (financial_table or explicit_comparison or implicit_multi_fact):
        return None
    if NON_COMPARABLE_WARNING_RE.search(content) and not financial_table:
        return None
    unsupported = [
        (number, unit) for number, unit in numeric_values
        if not _number_is_supported(number, unit, usable)
    ]
    if unsupported:
        number, unit = unsupported[0]
        return f'财务数值 {number}{unit} 无法对应已引用 FinancialFact'
    if len(subjects) < 2:
        return '财务比较未定位到两个可比主体'
    if len(signatures) != 1:
        return '财务比较混用了指标、期间、单位、币种、累计口径或合并范围'
    return None


def assemble_report(
    task_id: str,
    reviewed: Dict[str, Any],
    run_id: Optional[str] = None,
    *,
    publish: bool = True,
) -> Dict[str, Any]:
    store = EvidenceStore(task_id, run_id=run_id)
    artifact_folder = task_artifact_folder(task_id, run_id)
    safe_title = strip_advice_phrases(str(reviewed.get('title') or ''))
    safe_summary = strip_advice_phrases(str(reviewed.get('summary') or ''))
    if CITATION_RE.search(safe_title):
        raise ValueError('报告标题禁止携带证据角标')
    # Treat generated metadata as report content: summary citations and charts
    # must pass the same frozen-snapshot gates as body sections.
    checked_meta = _final_report_gate(
        [{
            'title': '报告摘要', 'goal': '', 'content': safe_summary,
            # Summary is deterministic report metadata.  Citation, finance,
            # chart and compliance gates still run; only the direct-report
            # exact-excerpt rule is inapplicable to this generated sentence.
            'system': True,
        }],
        store,
        artifact_folder,
    )
    safe_summary = str(checked_meta[0].get('content') or '')
    sections = [dict(section) for section in (reviewed.get('sections') or [])]
    sections = _ensure_debate_sections(sections, reviewed, store)
    sections = _final_report_gate(sections, store, artifact_folder)
    cited = _collect_cited_ids(sections)
    cited.update(int(value) for value in CITATION_RE.findall(safe_summary))
    has_private_evidence = any(
        (card.provenance or {}).get('license_scope') == 'private_derived_only'
        for card in store.cards
    )
    evidence_scope = '已授权数据与公开信息' if has_private_evidence else '公开信息'
    report_disclaimer = (
        DISCLAIMER.replace('基于公开信息', '基于已授权数据与公开信息')
        if has_private_evidence else DISCLAIMER
    )

    # 信息来源清单（必选系统章）
    source_lines = []
    for eid in sorted(cited) or [c.card_id for c in store.cards[:30]]:
        c = store.get(eid)
        if not c:
            continue
        display_id = store.display_id(c)
        line = f'- [{display_id}] {c.source_type} | {c.source_name} | {c.publish_time} | {c.title}'
        if c.url:
            line += f' | {c.url}'
        else:
            provenance = c.to_dict().get('provenance') or {}
            trace = [
                provenance.get('provider'),
                provenance.get('api'),
                provenance.get('record_key'),
                provenance.get('as_of'),
            ]
            trace = [str(value) for value in trace if value not in (None, '')]
            if provenance.get('row_fingerprint'):
                trace.append(f"fingerprint:{provenance['row_fingerprint']}")
            if trace:
                line += ' | 结构化溯源：' + ' / '.join(trace)
            if provenance.get('license_scope'):
                line += f" | 授权范围：{provenance['license_scope']}"
        source_lines.append(line)
    sources_md = '以下列出报告正文引用的信息来源与结构化数据溯源（非穷尽全部采集结果）：\n\n' + (
        '\n'.join(source_lines) if source_lines else '（暂无引用）'
    )
    sections.append({
        'title': '信息来源清单',
        'goal': '系统自动生成',
        'content': sources_md,
        'verdict': 'pass',
        'system': True,
    })

    # 结构化 Provider 的失败/陈旧/公共源降级必须在报告中显式披露。
    integrity_notes = _collect_integrity_notes(store)
    integrity_md = '\n'.join(f'- {note}' for note in integrity_notes) if integrity_notes else (
        '- 本次证据未记录 Datayes 权限、仓库水位或公共数据源降级。'
    )
    sections.append({
        'title': '数据完整性说明',
        'goal': '系统自动汇总数据源降级状态',
        'content': integrity_md,
        'verdict': 'warning' if integrity_notes else 'pass',
        'system': True,
    })

    # 风险与关注点（必选系统章）
    risks = [
        f'本报告仅整理{evidence_scope}，不构成任何投资建议或收益承诺。',
        '财务比较仅纳入期间、累计口径、币种、单位与合并范围一致的数据；缺失项标为“暂无同口径数据”。',
        '新闻与研报观点不等于事实；文中已尽量标注机构归属，但仍可能存在转述偏差。',
        '采集工具依赖第三方公开接口，可能存在延迟、缺失或临时不可用。',
    ]
    if store.statistics().get('total_cards', 0) < 10:
        risks.append('本次可引用证据较少，部分章节信息可能不完整。')
    risk_md = '\n'.join(f'- {r}' for r in risks)
    sections.append({
        'title': '风险与关注点',
        'goal': '系统自动生成',
        'content': risk_md,
        'verdict': 'pass',
        'system': True,
    })

    # System-generated source metadata is untrusted input too (for example an
    # uploaded filename). Run the same gate after every system section exists.
    sections = _final_report_gate(sections, store, artifact_folder)

    # Markdown
    md_parts = [
        f'# {safe_title or "投研信息整理报告"}',
        '',
        f'> {safe_summary}',
        '',
        f'*生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · 产品：成竹 Foresketch*',
        '',
        f'> {report_disclaimer}',
        '',
    ]
    for s in sections:
        md_parts.append(f'## {s.get("title")}')
        md_parts.append('')
        md_parts.append(s.get('content') or '')
        md_parts.append('')

    markdown = '\n'.join(md_parts)
    report = {
        'task_id': task_id,
        'run_id': run_id,
        'title': safe_title,
        'summary': safe_summary,
        'sections': sections,
        'markdown': markdown,
        'cited_ids': sorted(cited),
        'statistics': store.statistics(),
        'disclaimer': report_disclaimer,
        'created_at': datetime.now().isoformat(),
        'mode': reviewed.get('mode'),
        'analysis_mode': reviewed.get('analysis_mode', 'direct'),
        'debate_status': reviewed.get('debate_status'),
        'debate_fallback_reason': reviewed.get('debate_fallback_reason'),
        'integrity_notes': integrity_notes,
    }

    if publish:
        publish_report(task_id, report, run_id=run_id)
    return report


def publish_report(
    task_id: str,
    report: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> None:
    """Atomically publish a fully serialized report before the run deadline."""

    if deadline_epoch is not None:
        from ..utils.run_limits import ensure_time_remaining
        ensure_time_remaining(
            deadline_epoch,
            reserve_seconds=1,
            stage='report_publish',
        )
    folder = os.path.join(Config.UPLOAD_FOLDER, 'tasks', task_id)
    os.makedirs(folder, exist_ok=True)
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    markdown = str(report.get('markdown') or '')
    contents = {
        'report.json': report_json,
        'report.md': markdown,
        'full_report.md': markdown,
    }
    transaction_id = f'report_{uuid.uuid4().hex}'
    started = json.dumps({
        'schema_version': 1,
        'task_id': task_id,
        'run_id': run_id,
        'transaction_id': transaction_id,
        'started_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }, ensure_ascii=False, indent=2)
    committed = json.dumps(build_report_commit(
        task_id=task_id,
        run_id=run_id,
        transaction_id=transaction_id,
        contents=contents,
    ), ensure_ascii=False, indent=2)

    # Explicit run artifacts are immutable. Only after all serializations have
    # succeeded do we update the task-root latest copies for old clients.
    if run_id and run_id != task_id:
        run_folder = task_artifact_folder(task_id, run_id)
        commit_path = os.path.join(run_folder, REPORT_COMMIT)
        # The presence of a commit marker is itself an immutable publication
        # boundary.  Even if later disk damage makes its hashes fail, never
        # reinterpret that run as an interrupted pre-commit attempt and erase
        # or rewrite it; a fresh execution must receive a fresh run_id.
        if os.path.lexists(commit_path):
            raise FileExistsError(f'run 产物已有提交标记，禁止覆盖: {run_folder}')
        if report_bundle_is_committed(
            run_folder,
            task_id=task_id,
            run_id=run_id,
        ):
            raise FileExistsError(f'run 产物已发布，禁止覆盖: {run_folder}')
        started_path = os.path.join(run_folder, REPORT_PUBLISH_STARTED)
        # A prior interrupted attempt is recoverable because its missing commit
        # marker means none of its report files were ever public. Only this
        # exact report bundle is cleaned; evidence and debate artifacts remain.
        if os.path.isfile(started_path):
            for name in (*REPORT_FILES, REPORT_COMMIT):
                path = os.path.join(run_folder, name)
                if os.path.isfile(path):
                    os.unlink(path)
        else:
            unexpected = [
                os.path.join(run_folder, name)
                for name in (*REPORT_FILES, REPORT_COMMIT)
                if os.path.exists(os.path.join(run_folder, name))
            ]
            if unexpected:
                raise FileExistsError(
                    f'run 中存在无交易标记的历史报告，禁止覆盖: {unexpected[0]}'
                )
        _atomic_write_text(started_path, started)
        created_paths: List[str] = []
        try:
            for name in REPORT_FILES:
                path = os.path.join(run_folder, name)
                _write_immutable_text(path, contents[name])
                created_paths.append(path)
            _write_immutable_text(commit_path, committed)
            created_paths.append(commit_path)
        except Exception:
            # Keep ``started`` so readers know this is not a legacy report.
            cleanup_paths = list(created_paths) + [
                os.path.join(run_folder, name)
                for name in (*REPORT_FILES, REPORT_COMMIT)
            ]
            for path in reversed(list(dict.fromkeys(cleanup_paths))):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise

    # The task-root files are a mutable latest alias. ``started`` is switched
    # first and ``commit`` last; readers validate all hashes, so a process
    # interruption can never expose a mixed three-file bundle as successful.
    _atomic_write_text(os.path.join(folder, REPORT_PUBLISH_STARTED), started)
    for name in REPORT_FILES:
        _atomic_write_text(os.path.join(folder, name), contents[name])
    _atomic_write_text(os.path.join(folder, REPORT_COMMIT), committed)


def load_report(task_id: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    folder = task_artifact_folder(task_id, run_id)
    if not report_bundle_is_committed(
        folder,
        task_id=task_id,
        run_id=(run_id if run_id and run_id != task_id else None),
    ):
        return None
    path = os.path.join(folder, 'report.json')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
