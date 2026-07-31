"""Evidence debate protocol, auditor authority and offline replay tests."""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.debate import (
    Challenge,
    ChallengeStatus,
    ClaimCard,
    ClaimStatus,
    FinancialFact,
    VerdictStatus,
)
from app.services.debate_orchestrator import (
    DebateOrchestrator,
    EvidenceAuditor,
    JudgeSynthesizer,
    load_debate_artifacts,
    replay_debate,
)
from app.utils.llm_client import LLMResult


def _evidence():
    return {
        'run_id': 'run_test',
        'items': [
            {
                'evidence_uid': 'ev_quality',
                'display_id': 'E1',
                'card': {
                    'source_type': 'financial_report',
                    'title': '甲公司 2025 年报',
                    'symbol': '000001',
                    'publish_time': '2025-12-31',
                    'excerpt': '营业收入 100 亿元。',
                    'structured': {},
                },
            },
            {
                'evidence_uid': 'ev_growth',
                'display_id': 'E2',
                'card': {
                    'source_type': 'financial_report',
                    'title': '乙公司 2025 年报',
                    'symbol': '000002',
                    'publish_time': '2025-12-31',
                    'excerpt': '归母净利润 20 亿元。',
                    'structured': {},
                },
            },
        ],
    }


def _facts():
    common = {
        'unit': '亿元',
        'currency': 'CNY',
        'period': '2025-12-31',
        'period_type': 'FY',
        'accumulation_basis': 'cumulative',
        'consolidation_scope': 'consolidated',
        'disclosure_time': '2025-12-31',
    }
    return [
        FinancialFact(
            fact_uid='fact_revenue', evidence_uid='ev_quality', subject='000001',
            metric='营业总收入', value=Decimal('100'), **common,
        ),
        FinancialFact(
            fact_uid='fact_profit', evidence_uid='ev_growth', subject='000002',
            metric='归母净利润', value=Decimal('20'), **common,
        ),
    ]


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, messages, **kwargs):
        self.calls.append({'messages': messages, 'kwargs': kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _five_responses():
    return [
        {
            'claims': [{
                'claim_id': 'quality_1',
                'dimension': '盈利质量',
                'assertion': '000001在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的营业总收入为100亿元。',
                'evidence_uids': ['ev_quality'],
                'fact_uids': ['fact_revenue'],
                'fact_assertions': [{
                    'fact_uid': 'fact_revenue', 'subject': '000001',
                    'metric': '营业总收入', 'value': '100', 'unit': '亿元',
                    'currency': 'CNY', 'period': '2025-12-31',
                    'period_type': 'FY', 'accumulation_basis': 'cumulative',
                    'consolidation_scope': 'consolidated',
                }],
                'supporting_quotes': {'ev_quality': ['营业收入 100 亿元。']},
                'assumptions': [],
            }],
        },
        {
            'claims': [{
                'claim_id': 'growth_bad',
                'dimension': '增长驱动',
                'assertion': '000002在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的归母净利润为999亿元。',
                'evidence_uids': ['ev_growth'],
                'fact_uids': ['fact_profit'],
                'fact_assertions': [{
                    'fact_uid': 'fact_profit', 'subject': '000002',
                    'metric': '归母净利润', 'value': '20', 'unit': '亿元',
                    'currency': 'CNY', 'period': '2025-12-31',
                    'period_type': 'FY', 'accumulation_basis': 'cumulative',
                    'consolidation_scope': 'consolidated',
                }],
                'supporting_quotes': {'ev_growth': ['归母净利润 20 亿元。']},
                'assumptions': [],
            }],
            'challenges': [{
                'challenge_id': 'challenge_1',
                'target_claim_id': 'quality_1',
                'challenge_type': 'sustainability',
                'argument': '单期收入披露不足以证明持续性。',
                'evidence_uids': ['ev_quality'],
                'fact_uids': ['fact_revenue'],
                'fact_assertions': [{
                    'fact_uid': 'fact_revenue', 'subject': '000001',
                    'metric': '营业总收入', 'value': '100', 'unit': '亿元',
                    'currency': 'CNY', 'period': '2025-12-31',
                    'period_type': 'FY', 'accumulation_basis': 'cumulative',
                    'consolidation_scope': 'consolidated',
                }],
                'fact_basis_statement': '000001在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的营业总收入为100亿元。',
                'supporting_quotes': {'ev_quality': ['营业收入 100 亿元。']},
            }],
        },
        {
            'claims': [],
            'challenge_responses': [{
                'challenge_id': 'challenge_1',
                'response': '000001在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的营业总收入为100亿元。',
                'response_claim_id': 'quality_1',
                'resolution_status': 'resolved',
            }],
        },
        {'claims': [], 'challenge_responses': [{
            'challenge_id': 'challenge_1',
            'resolution_status': 'dismissed',
        }]},
        {
            # Judge deliberately tries to accept the unsupported 999 value.
            'accepted_claim_ids': ['quality_1', 'growth_bad'],
            'evidence_gaps': [],
            'follow_up_public_items': ['下一期定期报告'],
        },
    ]


def test_five_calls_and_judge_cannot_override_auditor(tmp_path):
    llm = ScriptedLLM(_five_responses())
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=llm,
        max_corrections=0,
    )
    verdict = orchestrator.run()

    assert len(llm.calls) == 5
    assert all(call['kwargs']['thinking'] is True for call in llm.calls)
    assert all(call['kwargs']['reasoning_effort'] == 'high' for call in llm.calls)
    assert verdict.status is VerdictStatus.COMPLETE
    assert verdict.accepted_claim_ids == ['quality_1']
    assert any('营业总收入为100亿元' in text for text in verdict.consensus_facts)
    assert all('999' not in text for text in verdict.consensus_facts)

    scores = {score.claim_id: score for score in orchestrator.audit_scores}
    assert scores['quality_1'].hard_pass
    assert not scores['growth_bad'].numeric_pass
    statuses = {claim.claim_id: claim.status for claim in orchestrator.claims}
    assert statuses['quality_1'] is ClaimStatus.ACCEPTED
    assert statuses['growth_bad'] is ClaimStatus.REJECTED
    assert any(
        request.reason.startswith('numeric:')
        for request in verdict.evidence_requests
    )

    artifacts = load_debate_artifacts(tmp_path)
    assert artifacts['verdict']['accepted_claim_ids'] == ['quality_1']
    assert len(artifacts['claims']) == 2
    assert len(artifacts['challenges']) == 1
    assert len(artifacts['audit']) == 2
    assert replay_debate(tmp_path).accepted_claim_ids == ['quality_1']


def test_judge_failure_requires_direct_pipeline_fallback(tmp_path):
    responses = _five_responses()
    responses[-1] = RuntimeError('judge unavailable')
    llm = ScriptedLLM(responses)
    verdict = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=llm,
        max_corrections=0,
    ).run()

    assert len(llm.calls) == 5
    assert verdict.status is VerdictStatus.DEGRADED
    assert verdict.generated_by == 'orchestrator_degraded'
    assert verdict.accepted_claim_ids == []
    assert verdict.degradation_reason


def test_single_role_failure_degrades_the_whole_debate(tmp_path):
    llm = ScriptedLLM([
        _five_responses()[0],
        RuntimeError('growth role unavailable'),
    ])
    verdict = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=llm,
        max_corrections=0,
    ).run()

    assert verdict.status is VerdictStatus.DEGRADED
    assert verdict.accepted_claim_ids == []
    assert verdict.degradation_reason == 'r1_growth: RuntimeError'


def test_reserved_result_settlement_failure_degrades_debate(
    tmp_path,
    monkeypatch,
):
    class ReservedResultLLM:
        def chat_json_result(self, _messages, **_kwargs):
            payload = _five_responses()[0]
            return LLMResult(
                content=json.dumps(payload, ensure_ascii=False),
                provider='deepseek',
                model='deepseek-v4-pro',
                finish_reason='stop',
                usage={'prompt_tokens': 10, 'completion_tokens': 5},
                parsed_json=payload,
                budget_reservation_id='llmres_real',
            )

    monkeypatch.setattr(
        'app.utils.llm_audit.record_llm_result',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('settlement unavailable')
        ),
    )
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        llm=ReservedResultLLM(),
        max_corrections=0,
    )

    verdict = orchestrator.run()

    assert verdict.status is VerdictStatus.DEGRADED
    assert verdict.accepted_claim_ids == []
    assert verdict.degradation_reason == 'r1_quality: OSError'


def test_invalid_challenge_cannot_block_or_enter_the_verdict(tmp_path):
    responses = _five_responses()
    responses[1] = dict(responses[1])
    responses[1]['challenges'] = [dict(responses[1]['challenges'][0])]
    responses[1]['challenges'][0]['evidence_uids'] = ['ev_missing']
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=ScriptedLLM(responses),
        max_corrections=0,
    )
    verdict = orchestrator.run()

    assert verdict.accepted_claim_ids == ['quality_1']
    assert orchestrator.valid_challenges == []
    assert orchestrator.challenge_audits[0]['hard_pass'] is False
    assert any(
        'unknown_evidence' in issue
        for issue in orchestrator.challenge_audits[0]['issues']
    )


def test_no_key_produces_persisted_degraded_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', None, raising=False)
    monkeypatch.setattr(Config, 'LLM_API_KEY', None)

    verdict = DebateOrchestrator(tmp_path, _evidence(), _facts()).run()

    assert verdict.status is VerdictStatus.DEGRADED
    assert '未配置' in (verdict.degradation_reason or '')
    persisted = json.loads((tmp_path / 'debate' / 'verdict.json').read_text(encoding='utf-8'))
    assert persisted['status'] == 'degraded'
    assert (tmp_path / 'debate' / 'claims.jsonl').is_file()
    assert (tmp_path / 'debate' / 'challenges.jsonl').is_file()
    assert (tmp_path / 'debate' / 'audit.jsonl').is_file()


def test_owned_debate_client_reserves_against_run_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'offline-key', raising=False)
    monkeypatch.setattr(Config, 'LLM_API_KEY', 'offline-key')
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        llm=None,
        auto_create_llm=True,
        run_id='run-debate-budget',
    )

    assert orchestrator.llm.budget_run_id == 'run-debate-budget'


def test_debate_prompt_retrieves_bounded_snippet_without_full_upload(tmp_path):
    evidence = _evidence()
    evidence['items'].append({
        'evidence_uid': 'ev_upload',
        'display_id': 'E3',
        'card': {
            'source_type': 'uploaded_document',
            'title': '长文档',
            'symbol': '000001',
            'publish_time': '2025-04-01',
            'excerpt': '文档首页暂无关键信息。',
            'structured': {
                'file_name': 'long.txt',
                'traditional_text': (
                    '无关背景。' * 4000
                    + '经营活动现金流净额为本文档的关键原文。'
                    + '其他内容。' * 4000
                ),
                'structured_markdown': '不应整段进入 Prompt',
            },
        },
    })
    orchestrator = DebateOrchestrator(
        tmp_path,
        evidence,
        _facts(),
        llm=ScriptedLLM([]),
        auto_create_llm=False,
    )

    context = orchestrator._evidence_context()
    uploaded = next(item for item in context if item['evidence_uid'] == 'ev_upload')
    serialized = json.dumps(uploaded, ensure_ascii=False)

    assert '经营活动现金流净额' in uploaded['excerpt']
    assert len(uploaded['excerpt']) <= 2400
    assert 'traditional_text' not in serialized
    assert 'structured_markdown' not in serialized


def test_compliance_failure_can_never_be_accepted():
    claim = ClaimCard(
        claim_id='bad_advice',
        dimension='盈利质量',
        assertion='建议买入，形成看多信号。',
        role='稳健与质量视角',
        round_number=1,
        evidence_uids=['ev_quality'],
    )
    score = EvidenceAuditor(_evidence(), _facts()).audit_claim(claim)
    assert not score.compliance_pass
    assert not score.hard_pass


def test_counterevidence_can_withdraw_a_claim(tmp_path):
    responses = _five_responses()
    responses[2] = {
        'claims': [{'claim_id': 'quality_1', 'status': 'withdrawn'}],
        'challenge_responses': [{
            'challenge_id': 'challenge_1',
            'response': '反证成立，撤回原论断。',
            'resolution_status': 'upheld',
        }],
    }
    responses[-1] = {'accepted_claim_ids': ['quality_1']}
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=ScriptedLLM(responses),
        max_corrections=0,
    )
    verdict = orchestrator.run()

    quality = next(claim for claim in orchestrator.claims if claim.claim_id == 'quality_1')
    assert quality.status is ClaimStatus.WITHDRAWN
    assert verdict.accepted_claim_ids == []
    assert quality.assertion in verdict.withdrawn_claims


def test_one_format_failure_gets_one_bounded_correction(tmp_path):
    responses = _five_responses()
    llm = ScriptedLLM([ValueError('bad json'), *responses])
    verdict = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=llm,
        max_corrections=1,
    ).run()

    assert len(llm.calls) == 6
    assert verdict.accepted_claim_ids == ['quality_1']


def test_opposing_role_cannot_withdraw_another_roles_claim(tmp_path):
    responses = _five_responses()
    responses[1] = dict(responses[1])
    responses[1]['claims'] = [
        {'claim_id': 'quality_1', 'status': 'withdrawn'},
        *responses[1]['claims'],
    ]
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        dimensions=['盈利质量', '增长驱动'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
        llm=ScriptedLLM(responses),
        max_corrections=0,
    )
    verdict = orchestrator.run()

    quality = next(claim for claim in orchestrator.claims if claim.claim_id == 'quality_1')
    assert quality.status is ClaimStatus.ACCEPTED
    assert verdict.accepted_claim_ids == ['quality_1']


def _quality_claim() -> ClaimCard:
    return ClaimCard.from_dict({
        'claim_id': 'quality_1',
        'dimension': '盈利质量',
        'assertion': '000001在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的营业总收入为100亿元。',
        'role': '稳健与质量视角',
        'round': 1,
        'evidence_uids': ['ev_quality'],
        'fact_uids': ['fact_revenue'],
        'fact_assertions': [{
            'fact_uid': 'fact_revenue', 'subject': '000001',
            'metric': '营业总收入', 'value': '100', 'unit': '亿元',
            'currency': 'CNY', 'period': '2025-12-31',
            'period_type': 'FY', 'accumulation_basis': 'cumulative',
            'consolidation_scope': 'consolidated',
        }],
        'supporting_quotes': {'ev_quality': ['营业收入 100 亿元。']},
    })


def test_assumption_advice_and_unsupported_forecast_are_hard_failures():
    claim = _quality_claim()
    claim.assumptions = ['建议买入，明年收入翻倍。']
    auditor = EvidenceAuditor(_evidence(), _facts(), dimensions=['盈利质量'])

    scores = auditor.audit_all([claim], [])
    score = scores[0]
    verdict = JudgeSynthesizer().from_judge_payload(
        {'accepted_claim_ids': [claim.claim_id]},
        [claim],
        scores,
        [],
    )

    assert not score.compliance_pass
    assert not score.citation_pass
    assert not score.hard_pass
    assert 'citation:unsupported_assumption_forecast:1' in score.issues
    assert any(issue.startswith('compliance:assumption:1:') for issue in score.issues)
    assert claim.status is ClaimStatus.REJECTED
    assert verdict.accepted_claim_ids == []
    assert verdict.assumptions == []


def test_unsupported_factual_assumption_is_a_hard_failure():
    for assumption in (
        '公司目前拥有行业第一的市场份额。',
        '行业景气度处于高位。',
    ):
        claim = _quality_claim()
        claim.assumptions = [assumption]

        score = EvidenceAuditor(_evidence(), _facts()).audit_claim(claim)

        assert not score.citation_pass
        assert not score.hard_pass
        assert 'citation:unsupported_assumption_fact:1' in score.issues


def test_clearly_conditional_nonfactual_assumption_is_allowed():
    claim = _quality_claim()
    claim.assumptions = [
        '假设未来原材料价格波动不超过10%，仅作为分析前提，不代表预测。'
    ]

    score = EvidenceAuditor(_evidence(), _facts()).audit_claim(claim)

    assert score.hard_pass
    assert not any('unsupported_assumption' in issue for issue in score.issues)


def test_exact_frozen_quote_is_valid_inside_assumptions():
    claim = _quality_claim()
    claim.assumptions = ['营业收入 100 亿元。']

    score = EvidenceAuditor(_evidence(), _facts()).audit_claim(claim)

    assert score.hard_pass


def test_runtime_type_error_is_not_retried_without_thinking(tmp_path):
    class RuntimeTypeErrorLLM:
        def __init__(self):
            self.calls = []

        def chat_json(self, messages, **kwargs):
            self.calls.append({'messages': messages, 'kwargs': kwargs})
            raise TypeError('runtime serialization bug')

    llm = RuntimeTypeErrorLLM()
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        llm=llm,
        auto_create_llm=False,
    )

    with pytest.raises(TypeError, match='runtime serialization bug'):
        orchestrator._invoke_once(
            [{'role': 'user', 'content': 'x'}],
            agent='quality_agent',
        )

    assert len(llm.calls) == 1
    assert llm.calls[0]['kwargs']['thinking'] is True
    assert llm.calls[0]['kwargs']['reasoning_effort'] == 'high'


def test_legacy_offline_fake_is_detected_before_call(tmp_path):
    class LegacyOfflineFake:
        def __init__(self):
            self.calls = []

        def chat_json(self, messages, temperature, max_tokens, max_attempts):
            self.calls.append({
                'messages': messages,
                'temperature': temperature,
                'max_tokens': max_tokens,
                'max_attempts': max_attempts,
            })
            return {'ok': True}

    llm = LegacyOfflineFake()
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        llm=llm,
        auto_create_llm=False,
    )

    assert orchestrator._invoke_once(
        [{'role': 'user', 'content': 'x'}],
        agent='quality_agent',
    ) == {'ok': True}
    assert len(llm.calls) == 1


def test_configured_client_without_reasoning_controls_fails_pre_call(tmp_path):
    class MisconfiguredClient:
        provider = 'deepseek'
        model = 'deepseek-v4-pro'

        def __init__(self):
            self.calls = 0

        def chat_json(self, messages, temperature, max_tokens, max_attempts):
            self.calls += 1
            return {'ok': True}

    llm = MisconfiguredClient()
    orchestrator = DebateOrchestrator(
        tmp_path,
        _evidence(),
        _facts(),
        llm=llm,
        auto_create_llm=False,
    )

    with pytest.raises(RuntimeError, match='must support thinking=True'):
        orchestrator._invoke_once(
            [{'role': 'user', 'content': 'x'}],
            agent='quality_agent',
        )
    assert llm.calls == 0


def _challenge(response: str, response_claim_id: str = 'quality_1') -> Challenge:
    return Challenge.from_dict({
        'challenge_id': 'challenge_1',
        'target_claim_id': 'quality_1',
        'challenge_type': 'sustainability',
        'argument': '单期收入披露不足以证明持续性。',
        'evidence_uids': ['ev_quality'],
        'fact_uids': ['fact_revenue'],
        'fact_assertions': [{
            'fact_uid': 'fact_revenue', 'subject': '000001',
            'metric': '营业总收入', 'value': '100', 'unit': '亿元',
            'currency': 'CNY', 'period': '2025-12-31',
            'period_type': 'FY', 'accumulation_basis': 'cumulative',
            'consolidation_scope': 'consolidated',
        }],
        'fact_basis_statement': '000001在2025-12-31（FY，CNY/亿元，cumulative，consolidated）的营业总收入为100亿元。',
        'supporting_quotes': {'ev_quality': ['营业收入 100 亿元。']},
        'response': response,
        'response_role': '稳健与质量视角',
        'response_claim_id': response_claim_id,
        'role': '成长与变化视角',
        'resolution_role': '成长与变化视角',
        'resolution_status': 'dismissed',
        'round': 2,
    })


def test_arbitrary_response_cannot_dismiss_valid_counterevidence():
    auditor = EvidenceAuditor(
        _evidence(),
        _facts(),
        dimensions=['盈利质量'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
    )
    claim = _quality_claim()
    challenge = _challenge('天气很好。')
    scores = auditor.audit_all([claim], [challenge])

    assert scores[0].hard_pass
    assert challenge.resolution_status is ChallengeStatus.OPEN
    assert claim.status is ClaimStatus.DISPUTED
    assert auditor.challenge_audits[0]['resolution_warnings'] == [
        'challenge:resolution_kept_open'
    ]


def test_unrelated_qualitative_challenge_is_rejected():
    auditor = EvidenceAuditor(
        _evidence(),
        _facts(),
        dimensions=['盈利质量'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
    )
    claim = _quality_claim()
    challenge = _challenge('')
    challenge.argument = '公司发生重大安全事故。'
    challenge.response_claim_id = ''
    challenge.resolution_role = ''
    challenge.resolution_status = ChallengeStatus.OPEN
    scores = auditor.audit_all([claim], [challenge])

    assert scores[0].hard_pass
    assert auditor.challenge_audits[0]['hard_pass'] is False
    assert 'challenge:semantic_mismatch' in auditor.challenge_audits[0]['issues']
    assert claim.status is ClaimStatus.PROPOSED


def test_hard_failed_response_claim_cannot_resolve_challenge():
    auditor = EvidenceAuditor(
        _evidence(),
        _facts(),
        dimensions=['盈利质量'],
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
    )
    target = _quality_claim()
    bad = _quality_claim()
    bad.claim_id = 'quality_bad'
    bad.assertion = bad.assertion.replace('100亿元', '999亿元')
    challenge = _challenge(bad.assertion, response_claim_id=bad.claim_id)
    auditor.audit_all([target, bad], [challenge])

    assert challenge.resolution_status is ChallengeStatus.OPEN
    assert target.status is ClaimStatus.DISPUTED
    bad_score = next(score for score in auditor.audit_all([target, bad], []) if score.claim_id == 'quality_bad')
    assert not bad_score.hard_pass


def test_factless_claim_cannot_append_an_inference_to_a_source_quote():
    claim = ClaimCard(
        claim_id='factless_suffix',
        dimension='盈利质量',
        assertion='营业收入 100 亿元。因此经营质量必然改善。',
        role='稳健与质量视角',
        round_number=1,
        evidence_uids=['ev_quality'],
        supporting_quotes={'ev_quality': ['营业收入 100 亿元。']},
    )
    score = EvidenceAuditor(_evidence(), _facts()).audit_claim(claim)

    assert not score.citation_pass
    assert 'citation:semantic_mismatch' in score.issues
