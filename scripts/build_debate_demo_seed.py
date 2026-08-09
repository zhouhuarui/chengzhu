#!/usr/bin/env python3
"""Build and validate the keyless CATL-vs-BYD evidence-debate demo.

All evidence in this demo is a synthetic fixture shaped like public disclosure
data.  It is deliberately labelled as synthetic in every artefact and must not
be interpreted as an actual filing or a statement about either company.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import Config
from app.models.debate import Challenge, ChallengeStatus, ClaimCard, ClaimStatus
from app.services.debate_orchestrator import (
    EvidenceAuditor,
    JudgeSynthesizer,
    load_debate_artifacts,
    replay_debate,
)
from app.services.financial_normalizer import FinancialNormalizer, write_facts_jsonl
from app.team.contracts import (
    DEFAULT_TEAM_DAG,
    build_task_contract,
    team_task_budget_allocations,
)
from app.utils.db import SCHEMA_SQL
from app.utils.report_commit import (
    REPORT_COMMIT,
    REPORT_FILES,
    REPORT_PUBLISH_STARTED,
    build_report_commit,
    report_bundle_is_committed,
)


TASK_ID = 'task_demo_catl_byd_debate'
RUN_ID = 'run_demo_catl_byd_h1_2025'
DIRECT_RUN_ID = 'run_demo_catl_byd_direct_2025'
CREATED_AT = '2026-07-31T10:00:00+08:00'
FINISHED_AT = '2026-07-31T10:00:08+08:00'
DIRECT_CREATED_AT = '2026-07-31T09:59:40+08:00'
DIRECT_FINISHED_AT = '2026-07-31T09:59:48+08:00'
FIXTURE_NOTICE = '比赛演示合成夹具；模拟公开披露结构，非真实公司披露，不可用于公司判断。'


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ''.join(
        json.dumps(row.to_dict() if hasattr(row, 'to_dict') else row, ensure_ascii=False) + '\n'
        for row in rows
    )
    path.write_text(text, encoding='utf-8')


def _report_bundle(
    folder: Path,
    *,
    task_id: str,
    run_id: str,
    report_json: str,
    report_md: str,
) -> None:
    """Write a deterministic, fully committed report bundle for replay."""

    transaction_id = f'demo_report_{run_id}'
    contents = {
        'report.json': report_json,
        'report.md': report_md,
        'full_report.md': report_md,
    }
    _json(folder / REPORT_PUBLISH_STARTED, {
        'schema_version': 1,
        'task_id': task_id,
        'run_id': run_id,
        'transaction_id': transaction_id,
        'started_at': CREATED_AT,
    })
    for name in REPORT_FILES:
        (folder / name).write_text(contents[name], encoding='utf-8')
    _json(folder / REPORT_COMMIT, build_report_commit(
        task_id=task_id,
        run_id=run_id,
        transaction_id=transaction_id,
        contents=contents,
    ))


def _fixture_card(
    uid: str,
    display_id: str,
    *,
    source_type: str,
    title: str,
    symbol: str,
    publish_time: str,
    excerpt: str,
    structured: Mapping[str, Any],
) -> Dict[str, Any]:
    card = {
        'source_type': source_type,
        'title': f'【合成夹具】{title}',
        'url': None,
        'publish_time': publish_time,
        'source_name': '比赛演示：公开披露结构合成夹具',
        'symbol': symbol,
        'excerpt': f'{FIXTURE_NOTICE}{excerpt}',
        'structured': {
            'synthetic_fixture': True,
            'fixture_notice': FIXTURE_NOTICE,
            **dict(structured),
        },
        'reliability': 3,
        'fetch_tool': 'demo_public_fixture',
        'card_id': None,
        'evidence_uid': uid,
        'provenance': {
            'provider': 'public_synthetic_fixture',
            'api': 'demo_public_disclosure_shape',
            'record_key': f'demo:{uid}',
            'as_of': publish_time[:10],
            'upstream_source': '比赛演示合成公开披露结构',
            'license_scope': 'public_synthetic_demo',
        },
    }
    return {'evidence_uid': uid, 'display_id': display_id, 'card': card}


def evidence_index() -> Dict[str, Any]:
    items = [
        _fixture_card(
            'ev_demo_catl_h1_2025', 'E1',
            source_type='financial_report',
            title='宁德时代 2025 年半年度财务摘要',
            symbol='300750',
            publish_time='2025-08-30T09:00:00+08:00',
            excerpt='模拟数据：H1 营业总收入 1788.86 亿元、归母净利润 304.85 亿元、经营活动现金流净额 586.87 亿元。',
            structured={
                'REPORT_DATE': '2025-06-30',
                'report_period': '2025-06-30',
                'report_type': 'H1',
                'statement': 'income',
                'merged_flag': 1,
                'accumulation_basis': 'cumulative',
                'currency_unit': 'CNY',
                'TOTAL_OPERATE_INCOME_yi': '1788.86',
                'PARENT_NETPROFIT_yi': '304.85',
                'NETCASH_OPERATE_yi': '586.87',
            },
        ),
        _fixture_card(
            'ev_demo_byd_h1_2025', 'E2',
            source_type='financial_report',
            title='比亚迪 2025 年半年度财务摘要',
            symbol='002594',
            publish_time='2025-08-30T09:05:00+08:00',
            excerpt='模拟数据：H1 营业总收入 3712.81 亿元、归母净利润 155.11 亿元、经营活动现金流净额 318.42 亿元。',
            structured={
                'REPORT_DATE': '2025-06-30',
                'report_period': '2025-06-30',
                'report_type': 'H1',
                'statement': 'income',
                'merged_flag': 1,
                'accumulation_basis': 'cumulative',
                'currency_unit': 'CNY',
                'TOTAL_OPERATE_INCOME_yi': '3712.81',
                'PARENT_NETPROFIT_yi': '155.11',
                'NETCASH_OPERATE_yi': '318.42',
            },
        ),
        _fixture_card(
            'ev_demo_byd_q1_2025', 'E3',
            source_type='financial_report',
            title='比亚迪 2025 年第一季度财务摘要',
            symbol='002594',
            publish_time='2025-04-30T09:00:00+08:00',
            excerpt='模拟数据：Q1 营业总收入 1703.60 亿元、归母净利润 91.55 亿元。',
            structured={
                'REPORT_DATE': '2025-03-31',
                'report_period': '2025-03-31',
                'report_type': 'Q1',
                'statement': 'income',
                'merged_flag': 1,
                'accumulation_basis': 'cumulative',
                'currency_unit': 'CNY',
                'TOTAL_OPERATE_INCOME_yi': '1703.60',
                'PARENT_NETPROFIT_yi': '91.55',
            },
        ),
        _fixture_card(
            'ev_demo_catl_operations', 'E4',
            source_type='announcement',
            title='宁德时代经营事项演示摘要',
            symbol='300750',
            publish_time='2025-07-15T10:00:00+08:00',
            excerpt='模拟经营事项描述：产能推进仍受项目节奏与需求兑现影响。',
            structured={'event_type': 'operations_demo'},
        ),
        _fixture_card(
            'ev_demo_byd_operations', 'E5',
            source_type='announcement',
            title='比亚迪经营事项演示摘要',
            symbol='002594',
            publish_time='2025-07-16T10:00:00+08:00',
            excerpt='模拟经营事项描述：产品与产能推进节奏仍需后续公开事项验证。',
            structured={'event_type': 'operations_demo'},
        ),
    ]
    return {
        'schema_version': 1,
        'task_id': TASK_ID,
        'run_id': RUN_ID,
        'created_at': CREATED_AT,
        'fixture_notice': FIXTURE_NOTICE,
        'items': items,
    }


def _find_fact(facts, subject: str, metric: str, period: str):
    matches = [
        fact for fact in facts
        if fact.subject == subject and fact.metric == metric and fact.period == period
    ]
    if len(matches) != 1:
        raise RuntimeError(f'无法唯一定位演示事实: {subject}/{metric}/{period}')
    return matches[0]


def _fact_assertion(fact) -> Dict[str, Any]:
    data = fact.to_dict()
    return {
        key: data[key]
        for key in (
            'fact_uid', 'subject', 'metric', 'value', 'unit', 'currency',
            'period', 'period_type', 'accumulation_basis',
            'consolidation_scope',
        )
    }


def build_debate(index: Dict[str, Any], facts):
    catl_h1_revenue = _find_fact(facts, '300750', '营业总收入', '2025-06-30')
    byd_h1_revenue = _find_fact(facts, '002594', '营业总收入', '2025-06-30')
    byd_q1_revenue = _find_fact(facts, '002594', '营业总收入', '2025-03-31')
    catl_h1_cash = _find_fact(facts, '300750', '经营活动现金流净额', '2025-06-30')

    claims = [
        ClaimCard(
            claim_id='claim_same_period_h1',
            dimension='盈利质量',
            assertion=EvidenceAuditor.canonical_fact_statement([
                catl_h1_revenue, byd_h1_revenue,
            ]),
            role='稳健与质量视角',
            round_number=1,
            evidence_uids=['ev_demo_catl_h1_2025', 'ev_demo_byd_h1_2025'],
            fact_uids=[catl_h1_revenue.fact_uid, byd_h1_revenue.fact_uid],
            fact_assertions=[
                _fact_assertion(catl_h1_revenue),
                _fact_assertion(byd_h1_revenue),
            ],
            supporting_quotes={
                'ev_demo_catl_h1_2025': ['H1 营业总收入 1788.86 亿元'],
                'ev_demo_byd_h1_2025': ['H1 营业总收入 3712.81 亿元'],
            },
            assumptions=[],
        ),
        ClaimCard(
            claim_id='claim_mixed_h1_q1',
            dimension='增长驱动',
            assertion=EvidenceAuditor.canonical_fact_statement([
                catl_h1_revenue, byd_q1_revenue,
            ]),
            role='成长与变化视角',
            round_number=1,
            evidence_uids=['ev_demo_catl_h1_2025', 'ev_demo_byd_q1_2025'],
            fact_uids=[catl_h1_revenue.fact_uid, byd_q1_revenue.fact_uid],
            fact_assertions=[
                _fact_assertion(catl_h1_revenue),
                _fact_assertion(byd_q1_revenue),
            ],
            supporting_quotes={
                'ev_demo_catl_h1_2025': ['H1 营业总收入 1788.86 亿元'],
                'ev_demo_byd_q1_2025': ['Q1 营业总收入 1703.60 亿元'],
            },
            assumptions=[],
            status=ClaimStatus.CHALLENGED,
        ),
        ClaimCard(
            claim_id='claim_cash_persistence',
            dimension='现金流与偿债',
            assertion=EvidenceAuditor.canonical_fact_statement([catl_h1_cash]),
            role='稳健与质量视角',
            round_number=1,
            evidence_uids=['ev_demo_catl_h1_2025'],
            fact_uids=[catl_h1_cash.fact_uid],
            fact_assertions=[_fact_assertion(catl_h1_cash)],
            supporting_quotes={
                'ev_demo_catl_h1_2025': ['经营活动现金流净额 586.87 亿元'],
            },
            assumptions=['单期累计现金流可代表后续期间'],
            status=ClaimStatus.WITHDRAWN,
        ),
        ClaimCard(
            claim_id='claim_operating_tempo',
            dimension='经营变化',
            assertion='产能推进仍受项目节奏与需求兑现影响',
            role='成长与变化视角',
            round_number=2,
            evidence_uids=['ev_demo_catl_operations', 'ev_demo_byd_operations'],
            fact_uids=[],
            supporting_quotes={
                'ev_demo_catl_operations': ['产能推进仍受项目节奏与需求兑现影响'],
                'ev_demo_byd_operations': ['产品与产能推进节奏仍需后续公开事项验证'],
            },
            assumptions=['经营事项描述尚未被后续披露验证'],
            status=ClaimStatus.DISPUTED,
        ),
    ]
    challenges = [
        Challenge(
            challenge_id='challenge_period_mismatch',
            target_claim_id='claim_mixed_h1_q1',
            challenge_type='period_mismatch',
            argument='H1 与 Q1 报告期不同，不能进入同一横向比较或图表。',
            evidence_uids=['ev_demo_catl_h1_2025', 'ev_demo_byd_q1_2025'],
            fact_uids=[catl_h1_revenue.fact_uid, byd_q1_revenue.fact_uid],
            fact_assertions=[
                _fact_assertion(catl_h1_revenue),
                _fact_assertion(byd_q1_revenue),
            ],
            fact_basis_statement=EvidenceAuditor.canonical_fact_statement([
                catl_h1_revenue, byd_q1_revenue,
            ]),
            supporting_quotes={
                'ev_demo_catl_h1_2025': ['H1 营业总收入 1788.86 亿元'],
                'ev_demo_byd_q1_2025': ['Q1 营业总收入 1703.60 亿元'],
            },
            response='EvidenceAuditor 确认期间不一致，原比较被拒绝。',
            resolution_status=ChallengeStatus.UPHELD,
            role='稳健与质量视角',
            round_number=2,
        ),
        Challenge(
            challenge_id='challenge_cash_extrapolation',
            target_claim_id='claim_cash_persistence',
            challenge_type='unsupported_extrapolation',
            argument='单个半年度累计现金流事实不能证明后续现金创造能力保持不变。',
            evidence_uids=['ev_demo_catl_h1_2025'],
            fact_uids=[catl_h1_cash.fact_uid],
            fact_assertions=[_fact_assertion(catl_h1_cash)],
            fact_basis_statement=EvidenceAuditor.canonical_fact_statement([catl_h1_cash]),
            supporting_quotes={
                'ev_demo_catl_h1_2025': ['经营活动现金流净额 586.87 亿元'],
            },
            response='原角色接受反证并撤回外推观点。',
            resolution_status=ChallengeStatus.UPHELD,
            role='成长与变化视角',
            round_number=2,
        ),
        Challenge(
            challenge_id='challenge_operating_tempo',
            target_claim_id='claim_operating_tempo',
            challenge_type='insufficient_follow_up',
            argument='产能推进仍受项目节奏与需求兑现影响',
            evidence_uids=['ev_demo_catl_operations', 'ev_demo_byd_operations'],
            supporting_quotes={
                'ev_demo_catl_operations': ['产能推进仍受项目节奏与需求兑现影响'],
                'ev_demo_byd_operations': ['产品与产能推进节奏仍需后续公开事项验证'],
            },
            response='等待后续公开事项，维持未决。',
            resolution_status=ChallengeStatus.OPEN,
            role='稳健与质量视角',
            round_number=2,
        ),
    ]
    auditor = EvidenceAuditor(
        index,
        facts,
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        dimensions=['盈利质量', '现金流与偿债', '增长驱动', '经营变化'],
    )
    scores = auditor.audit_all(claims, challenges)
    # The original role explicitly withdrew this extrapolation; audit_all must
    # not promote it even though its quoted number itself is traceable.
    next(claim for claim in claims if claim.claim_id == 'claim_cash_persistence').status = ClaimStatus.WITHDRAWN
    next(claim for claim in claims if claim.claim_id == 'claim_operating_tempo').status = ClaimStatus.DISPUTED
    valid_challenge_ids = {
        item['challenge_id'] for item in auditor.challenge_audits if item['hard_pass']
    }
    valid_challenges = [
        item for item in challenges if item.challenge_id in valid_challenge_ids
    ]

    verdict = JudgeSynthesizer().from_judge_payload(
        {
            'accepted_claim_ids': ['claim_same_period_h1', 'claim_mixed_h1_q1'],
            'evidence_gaps': [
                'H1 与 Q1 不具备同期间可比性；错误混比不得进入结论或图表。',
                '经营事项缺少后续兑现证据。',
            ],
            'follow_up_public_items': ['后续定期报告与经营事项进展公告。'],
        },
        claims,
        scores,
        valid_challenges,
    )
    payload = verdict.to_dict()
    payload['major_challenges'] = [
        'H1 与 Q1 报告期不一致，混比 Claim 已被 EvidenceAuditor 拒绝。',
        '单期现金流不可直接外推，相关 Claim 已撤回。',
    ]
    payload['fixture_notice'] = FIXTURE_NOTICE
    return claims, challenges, scores, list(auditor.challenge_audits), verdict, payload


def _report(index, claims, challenges, scores, verdict_payload) -> Dict[str, Any]:
    score_by_id = {score.claim_id: score for score in scores}
    mixed = score_by_id['claim_mixed_h1_q1']
    accepted = next(claim for claim in claims if claim.claim_id == 'claim_same_period_h1')
    withdrawn = next(claim for claim in claims if claim.claim_id == 'claim_cash_persistence')
    disputed = next(claim for claim in claims if claim.claim_id == 'claim_operating_tempo')
    sections = [
        {
            'title': '方法与演示声明',
            'goal': '说明演示数据边界',
            'content': (
                f'{FIXTURE_NOTICE}\n\n本报告用于展示证据冻结、两轮辩论、硬校验与裁决界面。'
                '全部数值均为演示输入，不代表宁德时代或比亚迪的真实财务数据。'
            ),
            'verdict': 'pass',
        },
        {
            'title': '共识事实',
            'goal': '仅展示通过全部硬校验的 Claim',
            'content': f'- {accepted.assertion}[E1][E2]',
            'verdict': 'pass',
        },
        {
            'title': '主要反证',
            'goal': '展示反证与硬校验结果',
            'content': (
                '- H1 与 Q1 报告期不一致，混比 Claim 被 EvidenceAuditor 拒绝；'
                '该比较未进入结论或图表。[E1][E3]\n'
                '- 单期累计现金流不能证明后续能力保持不变，原角色接受反证并撤回观点。[E1]'
            ),
            'verdict': 'pass',
        },
        {
            'title': '未决分歧',
            'goal': '保留尚未解决的反证',
            'content': f'- {disputed.assertion}[E4][E5]',
            'verdict': 'warning',
        },
        {
            'title': '撤回观点',
            'goal': '明确不进入最终结论的观点',
            'content': f'- {withdrawn.assertion}[E1]（已撤回）',
            'verdict': 'warning',
        },
        {
            'title': '证据不足',
            'goal': '披露硬校验失败与缺口',
            'content': (
                f'- 混比 Claim 的 comparability_pass={str(mixed.comparability_pass).lower()}，'
                'H1 与 Q1 不可直接比较。\n- 当前没有经营事项的后续兑现证据。'
            ),
            'verdict': 'warning',
        },
        {
            'title': '后续公开事项',
            'goal': '列出未来可公开验证事项',
            'content': '- 后续定期报告与经营事项进展公告。',
            'verdict': 'pass',
        },
    ]
    source_lines = []
    for item in index['items']:
        card = item['card']
        source_lines.append(
            f"- [{item['display_id']}] {card['source_type']} | {card['source_name']} | "
            f"{card['publish_time']} | {card['title']} | 合成记录：{card['provenance']['record_key']}"
        )
    sections.extend([
        {
            'title': '信息来源清单',
            'goal': '列出本演示冻结证据',
            'content': '以下均为模拟公开披露结构的合成夹具：\n\n' + '\n'.join(source_lines),
            'verdict': 'pass',
            'system': True,
        },
        {
            'title': '数据完整性说明',
            'goal': '声明数据来源边界',
            'content': '- 本演示只包含合成夹具，不包含采购、授权或真实公司披露数据。',
            'verdict': 'pass',
            'system': True,
        },
        {
            'title': '风险与关注点',
            'goal': '系统提示',
            'content': (
                '- 本报告仅用于产品功能演示，不构成任何投资建议或收益承诺。\n'
                '- 所有公司名称与证券代码仅用于界面辨识，合成数值不可用于现实判断。\n'
                '- 只有期间、累计口径、币种、单位与合并范围一致的数据才可比较。'
            ),
            'verdict': 'pass',
            'system': True,
        },
    ])
    title = '宁德时代 vs 比亚迪：证据化基本面辩论（合成演示）'
    summary = 'Keyless 比赛演示：同口径 Claim 通过，H1 与 Q1 混比被确定性 Auditor 拒绝。'
    disclaimer = '本报告使用合成夹具，仅供产品功能演示，不构成任何投资建议、证券推荐或收益承诺。'
    md = [
        f'# {title}', '', f'> {summary}', '',
        '*生成时间：2026-07-31 10:00:08 · 产品：成竹 Foresketch · 模式：离线回放*', '',
        f'> {disclaimer}', '',
    ]
    for section in sections:
        md.extend([f"## {section['title']}", '', section['content'], ''])
    return {
        'task_id': TASK_ID,
        'run_id': RUN_ID,
        'title': title,
        'summary': summary,
        'sections': sections,
        'markdown': '\n'.join(md),
        'cited_ids': [1, 2, 3, 4, 5],
        'statistics': {
            'total_cards': len(index['items']),
            'by_type': {'financial_report': 3, 'announcement': 2},
            'symbols': ['002594', '300750'],
            'run_id': RUN_ID,
        },
        'disclaimer': disclaimer,
        'created_at': FINISHED_AT,
        'mode': 'demo_replay',
        'analysis_mode': 'evidence_debate',
        'debate_status': 'completed',
        'debate_verdict': verdict_payload,
        'integrity_notes': ['本演示只使用明确标注的合成夹具。'],
        'demo_fixture': True,
        'fixture_notice': FIXTURE_NOTICE,
    }


def _direct_report(index, claims, challenges, scores, verdict_payload) -> Dict[str, Any]:
    """Build the keyless A/B counterpart from the identical frozen snapshot."""

    debate_report = _report(index, claims, challenges, scores, verdict_payload)
    accepted = next(claim for claim in claims if claim.claim_id == 'claim_same_period_h1')
    system_sections = [
        dict(section) for section in debate_report['sections']
        if section.get('system')
    ]
    sections = [
        {
            'title': '方法与演示声明',
            'goal': '说明演示数据边界',
            'content': (
                f'{FIXTURE_NOTICE}\n\n本 run 使用直接分析，与证据辩论 run '
                '复用完全相同的冻结证据和 FinancialFact。'
            ),
            'verdict': 'pass',
            'system': True,
        },
        {
            'title': '同口径财务事实',
            'goal': '直接呈现确定性标准化结果',
            'content': f'- {accepted.assertion}[E1][E2]',
            'verdict': 'pass',
            'deterministic_financial': True,
        },
        {
            'title': '口径限制',
            'goal': '阻断错期比较',
            'content': '- H1 与 Q1 不属于同一报告期，本 run 不生成对比或图表。[E1][E3]',
            'verdict': 'pass',
            'deterministic_financial': True,
        },
        *system_sections,
    ]
    title = '宁德时代 vs 比亚迪：直接基本面整理（合成演示）'
    summary = 'Keyless A/B 演示：直接分析与辩论分析使用同一冻结证据快照。'
    markdown = [
        f'# {title}', '', f'> {summary}', '',
        '*生成时间：2026-07-31 09:59:48 · 产品：成竹 Foresketch · 模式：离线回放*', '',
        f"> {debate_report['disclaimer']}", '',
    ]
    for section in sections:
        markdown.extend([f"## {section['title']}", '', section['content'], ''])
    direct = {
        **debate_report,
        'run_id': DIRECT_RUN_ID,
        'title': title,
        'summary': summary,
        'sections': sections,
        'markdown': '\n'.join(markdown),
        'created_at': DIRECT_FINISHED_AT,
        'mode': 'demo_replay_direct',
        'analysis_mode': 'direct',
        'debate_status': None,
    }
    direct.pop('debate_verdict', None)
    return direct


def _task_card(analysis_mode: str = 'evidence_debate') -> Dict[str, Any]:
    return {
        'deliverable': 'compare',
        'analysis_mode': analysis_mode,
        'execution_mode': 'replay',
        'symbols': [
            {'code': '300750', 'name': '宁德时代'},
            {'code': '002594', 'name': '比亚迪'},
        ],
        'time_window': {'start': '2025-01-01', 'end': '2025-12-31'},
        'info_types': ['financial_report', 'announcement'],
        'focus_points': ['盈利质量', '现金流与偿债', '增长驱动', '经营变化'],
        'compare_dimensions': ['营业总收入', '归母净利润', '经营活动现金流净额'],
        'output_language_style': 'professional_brief',
        'clarifications': ['本任务为合成夹具回放，不代表真实公司披露。'],
    }


def _task_json(claims, challenges, scores) -> Dict[str, Any]:
    return {
        'task_id': TASK_ID,
        'execution_mode': 'replay',
        'user_id': 'default',
        'requirement': '比赛演示：使用公开语境下的合成夹具，对比宁德时代与比亚迪并运行证据辩论。',
        'status': 'completed',
        'task_card': _task_card(),
        'progress': 100,
        'message': 'Keyless 证据辩论演示已生成',
        'error': None,
        'collect_failures': [],
        'created_at': CREATED_AT,
        'updated_at': FINISHED_AT,
        'current_run_id': RUN_ID,
        'progress_detail': {
            'stage': 'completed',
            'normalizing': {'facts': 8, 'source': 'synthetic_fixture'},
            'debate': {
                'status': 'completed',
                'current_round': 2,
                'current_role': 'Judge/Synthesizer',
                'claim_count': len(claims),
                'challenge_count': len(challenges),
                'withdrawn_count': sum(claim.status == ClaimStatus.WITHDRAWN for claim in claims),
                'audit_failure_count': sum(not score.hard_pass for score in scores),
            },
            'report_title': '宁德时代 vs 比亚迪：证据化基本面辩论（合成演示）',
            'mode': 'demo_replay',
        },
    }


def _agent_log() -> List[Dict[str, Any]]:
    entries = [
        ('normalizer', 'facts_ready', {'facts': 8, 'network_calls': 0}),
        ('稳健与质量视角', 'round_1_complete', {'claims': 2}),
        ('成长与变化视角', 'round_1_complete', {'claims': 2, 'challenges': 2}),
        ('稳健与质量视角', 'round_2_complete', {'withdrawn': 1}),
        ('成长与变化视角', 'round_2_complete', {'disputed': 1}),
        ('EvidenceAuditor', 'hard_checks_complete', {'audit_failures': 1, 'mixed_period_rejected': True}),
        ('Judge/Synthesizer', 'verdict_ready', {'accepted': 1, 'llm_calls': 0, 'mode': 'demo_replay'}),
        ('system', 'task_complete', {'report': 'report.json'}),
    ]
    return [
        {
            'timestamp': FINISHED_AT,
            'elapsed_seconds': index,
            'task_id': TASK_ID,
            'agent': agent,
            'action': action,
            'stage': 'demo_replay',
            'section_title': None,
            'section_index': None,
            'details': details,
        }
        for index, (agent, action, details) in enumerate(entries, start=1)
    ]


def _seed_agent_team_replay(
    connection: sqlite3.Connection,
    seed_dir: Path,
    *,
    run_id: str,
    analysis_mode: str,
    created_at: str,
    finished_at: str,
) -> None:
    """Persist a bounded, explicitly synthetic Team timeline for keyless UI."""

    team_id = f'team-{run_id}'
    trace_id = hashlib.sha256(
        f'{TASK_ID}:{run_id}:replay'.encode('utf-8')
    ).hexdigest()[:32]
    span_id = hashlib.sha256(
        f'{run_id}:span'.encode('utf-8')
    ).hexdigest()[:16]
    allocations = team_task_budget_allocations(2.0, analysis_mode)
    artifact_id = f'artifact-{run_id}-report'
    approval_id = f'approval-{run_id}-replay'
    degradation = (
        ['visual_skill_degraded'] if analysis_mode == 'evidence_debate' else []
    )
    connection.execute(
        """INSERT INTO agent_team_run
           (team_id, run_id, task_id, status, state_version, attempt_count,
            budget_cny, current_stage, trace_id, span_id, degraded,
            degradation_json, rejection_count, latest_artifact_id,
            config_json, idempotency_key, created_at, updated_at, finished_at)
           VALUES (?, ?, ?, 'published', 20, 0, 2.0, 'published', ?, ?, ?, ?,
                   0, ?, ?, ?, ?, ?, ?)""",
        (
            team_id,
            run_id,
            TASK_ID,
            trace_id,
            span_id,
            1 if degradation else 0,
            json.dumps(degradation, ensure_ascii=False),
            artifact_id,
            json.dumps({
                'execution_mode': 'replay',
                'analysis_mode': analysis_mode,
                'synthetic_fixture': True,
                'approval_authority': 'vue',
                'max_active_workers': 3,
            }, ensure_ascii=False, sort_keys=True),
            f'demo-create:{run_id}',
            created_at,
            finished_at,
            finished_at,
        ),
    )

    task_ids = {
        template.task_key: f'{team_id}:{template.task_key}'
        for template in DEFAULT_TEAM_DAG
    }
    skipped = (
        {'quality-analysis', 'growth-analysis'}
        if analysis_mode == 'direct' else set()
    )
    for ordinal, template in enumerate(DEFAULT_TEAM_DAG):
        team_task_id = task_ids[template.task_key]
        dependencies = [task_ids[item] for item in template.depends_on]
        status = 'skipped' if template.task_key in skipped else 'completed'
        contract = build_task_contract(
            goal=template.title,
            inputs=(
                [
                    {'type': 'team_task', 'team_task_id': dependency}
                    for dependency in dependencies
                ]
                or [{
                    'type': 'confirmed_task_card',
                    'artifact_ref': f'run://{run_id}/run.json',
                }]
            ),
            expected_outputs=[{
                'task_key': template.task_key,
                'result': 'durable_result_with_artifact_refs',
            }],
            acceptance_criteria=[
                'synthetic replay only',
                'all factual claims resolve to frozen fixture EvidenceCards',
                'publication is represented as an immutable historical event',
            ],
            deadline={'timeout_seconds': 480},
            budget={
                'currency': 'CNY',
                'limit_cny': allocations[template.task_key],
            },
            artifact_refs=[],
            trace_id=trace_id,
        )
        output = {
            'execution_mode': 'replay',
            'synthetic_fixture': True,
            'task_key': template.task_key,
            **({
                'visual_skill': 'degraded',
                'fallback': 'local-only-parser',
            } if template.task_key == 'disclosure-research' and degradation else {}),
            **({
                'accepted_claim_ids': ['claim_same_period_h1'],
                'rejected_claim_ids': ['claim_mixed_h1_q1'],
            } if template.task_key == 'evidence-judgement' else {}),
            **({
                'decision': 'pass',
                'candidate_sha256': hashlib.sha256(
                    (seed_dir / 'tasks' / TASK_ID / 'runs' / run_id / 'report.json').read_bytes()
                ).hexdigest(),
            } if template.task_key == 'compliance-review' else {}),
        }
        connection.execute(
            """INSERT INTO team_task
               (team_task_id, team_id, task_key, title, assigned_agent, role_id,
                status, state_version, attempt_count, budget_cny, ordinal,
                depends_on_json, input_json, output_json, error_code,
                idempotency_key, created_at, updated_at, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (
                team_task_id,
                team_id,
                template.task_key,
                template.title,
                template.assigned_agent,
                template.role_id,
                status,
                0 if status == 'skipped' else 1,
                allocations[template.task_key],
                ordinal,
                json.dumps(dependencies, ensure_ascii=False, sort_keys=True),
                json.dumps(contract, ensure_ascii=False, sort_keys=True),
                json.dumps(output, ensure_ascii=False, sort_keys=True),
                f'demo-task:{run_id}:{template.task_key}',
                created_at,
                finished_at,
                created_at,
                finished_at,
            ),
        )

    handoff_ordinal = 0
    for template in DEFAULT_TEAM_DAG:
        if template.task_key in skipped:
            continue
        for source_key in template.depends_on:
            if source_key in skipped:
                continue
            handoff_ordinal += 1
            source = next(item for item in DEFAULT_TEAM_DAG if item.task_key == source_key)
            contract = build_task_contract(
                goal=template.title,
                inputs=[{
                    'type': 'completed_team_task',
                    'team_task_id': task_ids[source_key],
                    'task_key': source_key,
                }],
                expected_outputs=[{
                    'task_key': template.task_key,
                    'result': 'durable_result_with_artifact_refs',
                }],
                acceptance_criteria=['synthetic replay handoff accepted'],
                deadline={'timeout_seconds': 480},
                budget={
                    'currency': 'CNY',
                    'limit_cny': allocations[template.task_key],
                },
                artifact_refs=[],
                trace_id=trace_id,
            )
            connection.execute(
                """INSERT INTO team_handoff
                   (handoff_id, team_id, source_task_id, target_task_id,
                    from_agent, to_agent, status, state_version, payload_json,
                    idempotency_key, created_at, updated_at, accepted_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'completed', 2, ?, ?, ?, ?, ?)""",
                (
                    f'handoff-{run_id}-{handoff_ordinal}',
                    team_id,
                    task_ids[source_key],
                    task_ids[template.task_key],
                    source.assigned_agent,
                    template.assigned_agent,
                    json.dumps({'task_contract': contract}, ensure_ascii=False, sort_keys=True),
                    f'demo-handoff:{run_id}:{source_key}:{template.task_key}',
                    created_at,
                    finished_at,
                    finished_at,
                ),
            )

    report_path = seed_dir / 'tasks' / TASK_ID / 'runs' / run_id / 'report.json'
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    connection.execute(
        """INSERT INTO human_approval
           (approval_id, team_id, artifact_id, decision, authority, actor,
            reason, team_state_version, idempotency_key, created_at)
           VALUES (?, ?, ?, 'approved', 'vue', 'demo-fixture', ?, 19, ?, ?)""",
        (
            approval_id,
            team_id,
            artifact_id,
            '合成夹具历史回放；非实时人工决策',
            f'demo-approval:{run_id}',
            finished_at,
        ),
    )
    connection.execute(
        """INSERT INTO artifact_manifest
           (artifact_id, team_id, run_id, artifact_type, artifact_version,
            uri, sha256, producer, schema_version, status, requires_approval,
            approval_id, is_latest, state_version, metadata_json,
            idempotency_key, created_at, published_at)
           VALUES (?, ?, ?, 'report', 1, ?, ?, 'report-writer', 1,
                   'published', 1, ?, 1, 1, ?, ?, ?, ?)""",
        (
            artifact_id,
            team_id,
            run_id,
            f'local://{TASK_ID}/{run_id}/report.json',
            report_sha,
            approval_id,
            json.dumps({
                'task_id': TASK_ID,
                'run_id': run_id,
                'execution_mode': 'replay',
                'synthetic_fixture': True,
            }, ensure_ascii=False, sort_keys=True),
            f'demo-artifact:{run_id}',
            finished_at,
            finished_at,
        ),
    )

    events = [
        ('team_created', 'chengzhu-backend', None, {
            'execution_mode': 'replay',
            'synthetic_fixture': True,
            'agent_roles': [template.role_id for template in DEFAULT_TEAM_DAG if template.role_id != 'system-freeze'],
        }),
        *[
            ('team_task_status_changed', template.assigned_agent, task_ids[template.task_key], {
                'to_status': 'skipped' if template.task_key in skipped else 'completed',
                'task_key': template.task_key,
                'synthetic_fixture': True,
            })
            for template in DEFAULT_TEAM_DAG
        ],
    ]
    if degradation:
        events.extend([
            ('demo_visual_failure_injected', 'chengzhu-backend', task_ids['disclosure-research'], {
                'scope': 'bailian-visual-proxy',
                'mode': 'synthetic-replay',
            }),
            ('official_skill_invoked', 'disclosure-researcher', task_ids['disclosure-research'], {
                'skill': 'alibabacloud-bailian-image-creator',
                'visual_skill': 'degraded',
                'fallback': 'local-only-parser',
                'synthetic_fixture': True,
            }),
            ('claim_audit_rejected', 'evidence-judge', task_ids['evidence-judgement'], {
                'claim_id': 'claim_mixed_h1_q1',
                'comparability_pass': False,
                'synthetic_fixture': True,
            }),
        ])
    events.append((
        'human_approval_replayed',
        'demo-fixture',
        task_ids['compliance-review'],
        {
            'decision': 'approved',
            'authority': 'vue',
            'synthetic_fixture': True,
            'not_a_live_decision': True,
        },
    ))
    for ordinal, (event_type, actor, team_task_id, payload) in enumerate(events):
        connection.execute(
            """INSERT INTO team_event
               (event_id, team_id, event_type, actor, team_task_id,
                payload_json, idempotency_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f'event-{run_id}-{ordinal}',
                team_id,
                event_type,
                actor,
                team_task_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                f'demo-event:{run_id}:{ordinal}',
                finished_at,
            ),
        )


def _update_database(seed_dir: Path, verdict: Dict[str, Any], claims, challenges, scores) -> None:
    target = seed_dir / 'chengzhu.db'
    fd, temp_name = tempfile.mkstemp(prefix='.chengzhu-demo-', suffix='.db', dir=str(seed_dir))
    os.close(fd)
    temp = Path(temp_name)
    try:
        if target.is_file():
            shutil.copy2(target, temp)
        connection = sqlite3.connect(str(temp))
        try:
            connection.executescript(SCHEMA_SQL)
            existing_teams = connection.execute(
                'SELECT team_id FROM agent_team_run WHERE task_id = ?',
                (TASK_ID,),
            ).fetchall()
            for (team_id,) in existing_teams:
                for table in (
                    'team_event', 'team_handoff', 'human_approval',
                    'artifact_manifest', 'team_task',
                ):
                    connection.execute(
                        f'DELETE FROM {table} WHERE team_id = ?', (team_id,)
                    )
            connection.execute(
                'DELETE FROM agent_team_run WHERE task_id = ?', (TASK_ID,)
            )
            for table in ('llm_call_log', 'tool_call_log', 'feedback'):
                connection.execute(
                    f'''DELETE FROM {table}
                        WHERE run_id IN (?, ?) OR run_id IN (
                          SELECT run_id FROM task_run WHERE task_id = ?
                        )''',
                    (RUN_ID, DIRECT_RUN_ID, TASK_ID),
                )
            connection.execute('DELETE FROM debate_run WHERE task_id = ?', (TASK_ID,))
            connection.execute('DELETE FROM task_run WHERE task_id = ?', (TASK_ID,))
            connection.execute(
                """INSERT INTO task_run
                   (run_id, task_id, user_id, task_card_json, status, started_at,
                    finished_at, llm_calls, llm_tokens, web_search_calls,
                    stage_timings_json, collect_failures_json, reflected)
                   VALUES (?, ?, 'default', ?, 'completed', ?, ?, 0, 0, 0, ?, '[]', 0)""",
                (
                    DIRECT_RUN_ID, TASK_ID,
                    json.dumps(_task_card('direct'), ensure_ascii=False),
                    DIRECT_CREATED_AT, DIRECT_FINISHED_AT,
                    json.dumps({'normalizing': 1, 'analyzing': 5, 'assembling': 1}),
                ),
            )
            connection.execute(
                """INSERT INTO task_run
                   (run_id, task_id, user_id, task_card_json, status, started_at,
                    finished_at, llm_calls, llm_tokens, web_search_calls,
                    stage_timings_json, collect_failures_json, reflected)
                   VALUES (?, ?, 'default', ?, 'completed', ?, ?, 0, 0, 0, ?, '[]', 0)""",
                (
                    RUN_ID, TASK_ID, json.dumps(_task_card(), ensure_ascii=False),
                    CREATED_AT, FINISHED_AT,
                    json.dumps({'normalizing': 1, 'debating': 5, 'adjudicating': 1, 'assembling': 1}),
                ),
            )
            connection.execute(
                """INSERT INTO debate_run
                   (run_id, task_id, status, current_round, current_role,
                    claim_count, challenge_count, withdrawn_count,
                    audit_failure_count, verdict_json, error, started_at, finished_at)
                   VALUES (?, ?, 'completed', 2, 'Judge/Synthesizer', ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    RUN_ID, TASK_ID, len(claims), len(challenges),
                    sum(claim.status == ClaimStatus.WITHDRAWN for claim in claims),
                    sum(not score.hard_pass for score in scores),
                    json.dumps(verdict, ensure_ascii=False), CREATED_AT, FINISHED_AT,
                ),
            )
            _seed_agent_team_replay(
                connection,
                seed_dir,
                run_id=DIRECT_RUN_ID,
                analysis_mode='direct',
                created_at=DIRECT_CREATED_AT,
                finished_at=DIRECT_FINISHED_AT,
            )
            _seed_agent_team_replay(
                connection,
                seed_dir,
                run_id=RUN_ID,
                analysis_mode='evidence_debate',
                created_at=CREATED_AT,
                finished_at=FINISHED_AT,
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def build_demo(seed_dir: Path) -> Path:
    seed_dir = Path(seed_dir).resolve()
    tasks_dir = seed_dir / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    staging = tasks_dir / f'.{TASK_ID}.staging'
    if staging.exists():
        shutil.rmtree(staging)
    run_dir = staging / 'runs' / RUN_ID
    direct_run_dir = staging / 'runs' / DIRECT_RUN_ID
    debate_dir = run_dir / 'debate'
    debate_dir.mkdir(parents=True)
    direct_run_dir.mkdir(parents=True)

    index = evidence_index()
    _json(run_dir / 'run.json', {
        'run_id': RUN_ID,
        'task_id': TASK_ID,
        'execution_mode': 'replay',
        'task_card': _task_card(),
        'created_at': CREATED_AT,
        'fixture_notice': FIXTURE_NOTICE,
    })
    _json(run_dir / 'evidence_index.json', index)
    facts = FinancialNormalizer(_task_card()['time_window']).normalize(index['items'])
    write_facts_jsonl(run_dir / 'normalized_facts.jsonl', facts)
    claims, challenges, scores, challenge_audits, verdict, verdict_payload = build_debate(index, facts)
    _jsonl(debate_dir / 'claims.jsonl', claims)
    _jsonl(debate_dir / 'challenges.jsonl', challenges)
    _jsonl(debate_dir / 'audit.jsonl', scores)
    _jsonl(debate_dir / 'challenge_audit.jsonl', challenge_audits)
    _json(debate_dir / 'verdict.json', verdict_payload)

    report = _report(index, claims, challenges, scores, verdict_payload)
    report_json = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    report_md = report['markdown'].rstrip() + '\n'
    _report_bundle(
        run_dir,
        task_id=TASK_ID,
        run_id=RUN_ID,
        report_json=report_json,
        report_md=report_md,
    )
    _json(run_dir / 'graph.json', {
        'nodes': [
            {'id': 'company_300750', 'type': 'Company', 'label': '宁德时代（演示）'},
            {'id': 'company_002594', 'type': 'Company', 'label': '比亚迪（演示）'},
        ],
        'edges': [],
        'statistics': {'nodes': 2, 'edges': 0, 'episodes': 5, 'backend': 'demo_fixture'},
        'fixture_notice': FIXTURE_NOTICE,
    })

    # A direct run over the byte-equivalent frozen evidence enables a real
    # keyless A/B switch in the competition UI.
    direct_index = json.loads(json.dumps(index, ensure_ascii=False))
    direct_index['run_id'] = DIRECT_RUN_ID
    _json(direct_run_dir / 'run.json', {
        'run_id': DIRECT_RUN_ID,
        'task_id': TASK_ID,
        'execution_mode': 'replay',
        'task_card': _task_card('direct'),
        'created_at': DIRECT_CREATED_AT,
        'fixture_notice': FIXTURE_NOTICE,
        'source_snapshot_run_id': RUN_ID,
    })
    _json(direct_run_dir / 'evidence_index.json', direct_index)
    write_facts_jsonl(direct_run_dir / 'normalized_facts.jsonl', facts)
    direct_report = _direct_report(index, claims, challenges, scores, verdict_payload)
    direct_report_json = json.dumps(direct_report, ensure_ascii=False, indent=2) + '\n'
    direct_report_md = direct_report['markdown'].rstrip() + '\n'
    _report_bundle(
        direct_run_dir,
        task_id=TASK_ID,
        run_id=DIRECT_RUN_ID,
        report_json=direct_report_json,
        report_md=direct_report_md,
    )
    shutil.copy2(run_dir / 'graph.json', direct_run_dir / 'graph.json')

    _json(staging / 'task.json', _task_json(claims, challenges, scores))
    _jsonl(staging / 'agent_log.jsonl', _agent_log())
    _report_bundle(
        staging,
        task_id=TASK_ID,
        run_id=RUN_ID,
        report_json=report_json,
        report_md=report_md,
    )
    shutil.copy2(run_dir / 'graph.json', staging / 'graph.json')
    _json(staging / 'demo_manifest.json', {
        'task_id': TASK_ID,
        'run_id': RUN_ID,
        'run_ids': {
            'evidence_debate': RUN_ID,
            'direct': DIRECT_RUN_ID,
        },
        'keyless': True,
        'llm_calls': 0,
        'fixture_notice': FIXTURE_NOTICE,
        'golden_assertions': {
            'accepted_claim_id': 'claim_same_period_h1',
            'mixed_period_rejected_claim_id': 'claim_mixed_h1_q1',
            'withdrawn_claim_id': 'claim_cash_persistence',
            'disputed_claim_id': 'claim_operating_tempo',
        },
    })

    target = tasks_dir / TASK_ID
    backup = tasks_dir / f'.{TASK_ID}.backup'
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)

    _update_database(seed_dir, verdict_payload, claims, challenges, scores)
    return target


@contextmanager
def _runtime_paths(seed_dir: Path):
    from app.utils import db as dbutil

    old_upload = Config.UPLOAD_FOLDER
    old_db = Config.DB_PATH
    old_debug = Config.DEBUG
    connection = getattr(dbutil._local, 'conn', None)
    if connection:
        connection.close()
        dbutil._local.conn = None
    Config.UPLOAD_FOLDER = str(seed_dir)
    Config.DB_PATH = str(seed_dir / 'chengzhu.db')
    Config.DEBUG = True
    try:
        yield
    finally:
        connection = getattr(dbutil._local, 'conn', None)
        if connection:
            connection.close()
            dbutil._local.conn = None
        Config.UPLOAD_FOLDER = old_upload
        Config.DB_PATH = old_db
        Config.DEBUG = old_debug


def validate_demo(seed_dir: Path, *, api_check: bool = True) -> Dict[str, Any]:
    seed_dir = Path(seed_dir).resolve()
    task_dir = seed_dir / 'tasks' / TASK_ID
    run_dir = task_dir / 'runs' / RUN_ID
    direct_run_dir = task_dir / 'runs' / DIRECT_RUN_ID
    required = [
        task_dir / 'task.json', task_dir / 'report.json', run_dir / 'run.json',
        run_dir / 'evidence_index.json', run_dir / 'normalized_facts.jsonl',
        run_dir / 'report.json', run_dir / 'debate' / 'claims.jsonl',
        run_dir / REPORT_COMMIT,
        run_dir / 'debate' / 'challenges.jsonl', run_dir / 'debate' / 'audit.jsonl',
        run_dir / 'debate' / 'challenge_audit.jsonl',
        run_dir / 'debate' / 'verdict.json', seed_dir / 'chengzhu.db',
        direct_run_dir / 'run.json', direct_run_dir / 'evidence_index.json',
        direct_run_dir / 'normalized_facts.jsonl', direct_run_dir / 'report.json',
        direct_run_dir / REPORT_COMMIT, task_dir / REPORT_COMMIT,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f'演示包缺少产物: {missing}')
    if not report_bundle_is_committed(str(run_dir), task_id=TASK_ID, run_id=RUN_ID):
        raise AssertionError('辩论 run 报告交易未完整提交')
    if not report_bundle_is_committed(
        str(direct_run_dir), task_id=TASK_ID, run_id=DIRECT_RUN_ID,
    ):
        raise AssertionError('direct run 报告交易未完整提交')
    if not report_bundle_is_committed(str(task_dir), task_id=TASK_ID, run_id=RUN_ID):
        raise AssertionError('latest 报告交易未完整提交')

    task = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
    report = json.loads((run_dir / 'report.json').read_text(encoding='utf-8'))
    direct_report = json.loads(
        (direct_run_dir / 'report.json').read_text(encoding='utf-8')
    )
    index = json.loads((run_dir / 'evidence_index.json').read_text(encoding='utf-8'))
    direct_index = json.loads(
        (direct_run_dir / 'evidence_index.json').read_text(encoding='utf-8')
    )
    artifacts = load_debate_artifacts(run_dir)
    replayed = replay_debate(run_dir)
    claims = {item['claim_id']: item for item in artifacts['claims']}
    audit = {item['claim_id']: item for item in artifacts['audit']}
    if task.get('current_run_id') != RUN_ID:
        raise AssertionError('task latest 未指向演示 run')
    if report.get('analysis_mode') != 'evidence_debate' or report.get('debate_status') != 'completed':
        raise AssertionError('报告未标记为已完成证据辩论')
    if direct_report.get('analysis_mode') != 'direct' or direct_report.get('debate_status') is not None:
        raise AssertionError('A/B 演示缺少 direct 报告')
    if direct_index.get('items') != index.get('items'):
        raise AssertionError('direct/debate 未复用同一冻结证据项')
    if replayed.status.value != 'complete' or 'claim_same_period_h1' not in replayed.accepted_claim_ids:
        raise AssertionError('离线 verdict 无法回放或缺少 accepted Claim')
    if 'claim_mixed_h1_q1' in replayed.accepted_claim_ids:
        raise AssertionError('错期混比 Claim 被错误接受')
    if audit['claim_mixed_h1_q1']['comparability_pass'] is not False:
        raise AssertionError('Auditor 未拒绝 H1/Q1 混比')
    if claims['claim_mixed_h1_q1']['status'] != 'rejected':
        raise AssertionError('混比 Claim 状态不是 rejected')
    if claims['claim_cash_persistence']['status'] != 'withdrawn':
        raise AssertionError('反证后撤回状态缺失')
    if claims['claim_operating_tempo']['status'] != 'disputed':
        raise AssertionError('未决 Claim 状态缺失')
    if '```chart' in report.get('markdown', ''):
        raise AssertionError('演示报告不应包含任何可能误导的混期图表')
    if FIXTURE_NOTICE not in report.get('markdown', ''):
        raise AssertionError('报告未明确披露合成夹具')
    if (task_dir / 'report.json').read_bytes() != (run_dir / 'report.json').read_bytes():
        raise AssertionError('任务根 latest 报告与 current run 不一致')
    if not all(item['card'].get('structured', {}).get('synthetic_fixture') for item in index['items']):
        raise AssertionError('存在未标注 synthetic_fixture 的演示证据')

    connection = sqlite3.connect(str(seed_dir / 'chengzhu.db'))
    connection.row_factory = sqlite3.Row
    try:
        run_row = connection.execute('SELECT * FROM task_run WHERE run_id = ?', (RUN_ID,)).fetchone()
        direct_run_row = connection.execute(
            'SELECT * FROM task_run WHERE run_id = ?', (DIRECT_RUN_ID,)
        ).fetchone()
        debate_row = connection.execute('SELECT * FROM debate_run WHERE run_id = ?', (RUN_ID,)).fetchone()
        llm_calls = connection.execute(
            'SELECT COUNT(*) FROM llm_call_log WHERE run_id IN (?, ?)',
            (RUN_ID, DIRECT_RUN_ID),
        ).fetchone()[0]
    finally:
        connection.close()
    if (
        not run_row or run_row['status'] != 'completed'
        or not direct_run_row or direct_run_row['status'] != 'completed'
        or not debate_row or debate_row['status'] != 'completed'
    ):
        raise AssertionError('SQLite run/debate 元数据不完整')
    if llm_calls != 0:
        raise AssertionError('Keyless demo 不应包含真实 LLM 调用记录')

    api_results: Dict[str, int] = {}
    if api_check:
        with _runtime_paths(seed_dir):
            from app import create_app
            app = create_app(Config)
            client = app.test_client()
            paths = {
                'runs': f'/api/task/{TASK_ID}/runs',
                'debate': f'/api/task/{TASK_ID}/debate?run_id={RUN_ID}',
                'report': f'/api/report/{TASK_ID}?run_id={RUN_ID}',
                'latest_report': f'/api/report/{TASK_ID}',
                'evidence': f'/api/task/{TASK_ID}/evidence?run_id={RUN_ID}',
                'direct_report': f'/api/report/{TASK_ID}?run_id={DIRECT_RUN_ID}',
                'direct_evidence': f'/api/task/{TASK_ID}/evidence?run_id={DIRECT_RUN_ID}',
                'team': f'/api/task/{TASK_ID}/team?run_id={RUN_ID}',
                'team_events': f'/api/task/{TASK_ID}/team/events?run_id={RUN_ID}',
                'direct_team': f'/api/task/{TASK_ID}/team?run_id={DIRECT_RUN_ID}',
            }
            for name, path in paths.items():
                response = client.get(path)
                api_results[name] = response.status_code
                if response.status_code != 200 or not (response.get_json(silent=True) or {}).get('success'):
                    raise AssertionError(f'API 不可读: {name} status={response.status_code}')
            debate_data = client.get(paths['debate']).get_json()['data']
            if debate_data['verdict']['accepted_claim_ids'] != ['claim_same_period_h1']:
                raise AssertionError('debate API accepted Claim 不符合演示契约')
            runs_data = client.get(paths['runs']).get_json()['data']
            modes = {
                item['run_id']: item.get('analysis_mode')
                for item in runs_data
            }
            if modes.get(RUN_ID) != 'evidence_debate' or modes.get(DIRECT_RUN_ID) != 'direct':
                raise AssertionError('runs API 未暴露 direct/debate A/B')
            team_data = client.get(paths['team']).get_json()['data']
            if (
                team_data.get('source') != 'replay'
                or len(team_data.get('agent_roles') or []) != 8
                or len(team_data.get('tasks') or []) != 9
            ):
                raise AssertionError('回放 Team 未暴露只读八角色/九节点状态')
            event_types = {
                item.get('event_type')
                for item in client.get(paths['team_events']).get_json()['data']['events']
            }
            if not {
                'official_skill_invoked',
                'claim_audit_rejected',
                'human_approval_replayed',
            }.issubset(event_types):
                raise AssertionError('回放 Team 缺少降级、审计拒绝或审批历史事件')
            direct_team = client.get(paths['direct_team']).get_json()['data']
            if direct_team.get('source') != 'replay':
                raise AssertionError('direct Team 未标记为只读回放')
            blocked = client.post(
                f'/api/task/{TASK_ID}/runs/{RUN_ID}/approval',
                json={'decision': 'approve', 'expected_version': 20},
            )
            if (
                blocked.status_code != 409
                or (blocked.get_json(silent=True) or {}).get('code') != 'replay_read_only'
            ):
                raise AssertionError('回放 Team 错误开放了人工批准入口')

    # Reuse the existing packaging guard; it scans text plus the entire SQLite
    # file and reports paths only, never fixture contents.
    from scripts.build_demo_seed import detect_private_datayes_artifacts
    findings = detect_private_datayes_artifacts(str(seed_dir))
    if findings:
        raise AssertionError(f'演示包包含禁止的数据源痕迹: {findings}')
    return {
        'task_id': TASK_ID,
        'run_id': RUN_ID,
        'direct_run_id': DIRECT_RUN_ID,
        'claims': len(claims),
        'challenges': len(artifacts['challenges']),
        'accepted': replayed.accepted_claim_ids,
        'api': api_results,
        'llm_calls': llm_calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Build the keyless evidence-debate demo seed')
    parser.add_argument('--seed-dir', default=str(ROOT / 'demo_seed'))
    parser.add_argument('--check', action='store_true', help='只校验现有产物，不重新生成')
    parser.add_argument('--skip-api', action='store_true', help='跳过 Flask test-client API 校验')
    args = parser.parse_args()
    seed_dir = Path(args.seed_dir)
    if not args.check:
        target = build_demo(seed_dir)
        print(f'built {target}')
    result = validate_demo(seed_dir, api_check=not args.skip_api)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
