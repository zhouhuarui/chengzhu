"""FinancialFact normalisation and hard-comparability tests."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.models.debate import ClaimCard
from app.services.analyst import Analyst
from app.services.debate_orchestrator import EvidenceAuditor
from app.services.financial_normalizer import FinancialNormalizer, load_facts_jsonl


def _card(
    uid: str,
    symbol: str,
    period: str,
    value,
    *,
    merged_flag=1,
    api='getFdmtIS',
    accumulation_basis=None,
    publish_time='2025-08-30',
):
    structured = {
        'REPORT_DATE': period,
        'report_period': period,
        'report_type': {'03-31': 'Q1', '06-30': 'H1', '09-30': 'Q3', '12-31': 'FY'}[period[5:]],
        'statement': 'income',
        'merged_flag': merged_flag,
        'TOTAL_OPERATE_INCOME_yi': value,
        'currency_unit': 'CNY',
    }
    if accumulation_basis:
        structured['accumulation_basis'] = accumulation_basis
    return {
        'evidence_uid': uid,
        'display_id': 'E1',
        'card': {
            'source_type': 'financial_report',
            'title': f'{symbol} 财务数据',
            'symbol': symbol,
            'publish_time': publish_time,
            'structured': structured,
            'provenance': {'api': api, 'record_key': f'{uid}-record'},
        },
    }


def test_decimal_and_metadata_are_preserved(tmp_path):
    output = tmp_path / 'normalized_facts.jsonl'
    facts = FinancialNormalizer({'start': '2025-01-01', 'end': '2025-12-31'}).normalize(
        [_card('ev_a', '300750', '2025-06-30', '1888.1200')],
        output,
    )

    assert len(facts) == 1
    assert str(facts[0].value) == '1888.1200'
    assert facts[0].unit == '亿元'
    assert facts[0].currency == 'CNY'
    assert facts[0].period_type == 'H1'
    assert facts[0].accumulation_basis == 'cumulative'
    assert facts[0].consolidation_scope == 'consolidated'
    assert not facts[0].quality_flags

    persisted = json.loads(output.read_text(encoding='utf-8').strip())
    assert persisted['value'] == '1888.1200'
    assert load_facts_jsonl(output)[0].fact_uid == facts[0].fact_uid


def test_null_unknown_scope_and_old_period_are_explicitly_flagged():
    card = _card('ev_bad', '002594', '2024-12-31', None, merged_flag=None)
    fact = FinancialNormalizer({'start': '2025-01-01', 'end': '2025-12-31'}).normalize([card])[0]

    assert fact.value is None
    assert {'missing_value', 'unknown_consolidation_scope', 'outside_time_window'}.issubset(
        set(fact.quality_flags)
    )


def test_h1_and_q1_cannot_enter_one_comparison():
    cards = [
        _card('ev_h1', '300750', '2025-06-30', '1888.12'),
        _card('ev_q1', '002594', '2025-03-31', '1703.60'),
    ]
    normalizer = FinancialNormalizer({'start': '2025-01-01', 'end': '2025-12-31'})
    facts = normalizer.normalize(cards)
    claim = ClaimCard(
        claim_id='mixed_period',
        dimension='增长驱动',
        assertion=EvidenceAuditor.canonical_fact_statement(facts),
        role='成长与变化视角',
        round_number=1,
        evidence_uids=['ev_h1', 'ev_q1'],
        fact_uids=[fact.fact_uid for fact in facts],
        fact_assertions=[fact.to_dict() for fact in facts],
        supporting_quotes={
            'ev_h1': ['300750 财务数据'],
            'ev_q1': ['002594 财务数据'],
        },
    )
    score = EvidenceAuditor(
        {'items': cards},
        facts,
        time_window={'start': '2025-01-01', 'end': '2025-12-31'},
    ).audit_claim(claim)

    assert score.citation_pass
    assert score.numeric_pass
    assert not score.comparability_pass
    assert any('comparability:mismatch' in issue for issue in score.issues)


def test_cumulative_and_single_quarter_cannot_be_compared_even_same_period():
    cards = [
        _card('ev_cum', '300750', '2025-06-30', '1888.12', accumulation_basis='cumulative'),
        _card('ev_q', '002594', '2025-06-30', '900.00', accumulation_basis='single_quarter'),
    ]
    facts = FinancialNormalizer().normalize(cards)
    assert not facts[0].is_comparable_with(facts[1])


def test_consolidated_parent_scope_and_unknown_unit_are_rejected():
    consolidated = _card('ev_consolidated', '300750', '2025-06-30', '1888.12', merged_flag=1)
    parent = _card('ev_parent', '002594', '2025-06-30', '900.00', merged_flag=0)
    facts = FinancialNormalizer().normalize([consolidated, parent])
    assert not facts[0].is_comparable_with(facts[1])

    unknown_unit = _card('ev_unit', '300750', '2025-06-30', '1')
    structured = unknown_unit['card']['structured']
    structured['TOTAL_OPERATE_INCOME'] = structured.pop('TOTAL_OPERATE_INCOME_yi')
    structured.pop('currency_unit')
    untyped = FinancialNormalizer().normalize([unknown_unit])[0]
    assert {'missing_unit', 'missing_currency'}.issubset(set(untyped.quality_flags))


def test_collection_as_of_is_never_used_as_disclosure_time():
    card = _card(
        'ev_as_of_only', '300750', '2025-06-30', '1888.12',
        publish_time='',
    )
    card['card']['provenance']['as_of'] = '2025-08-30'

    fact = FinancialNormalizer().normalize([card])[0]

    assert fact.disclosure_time == ''
    assert 'missing_disclosure_time' in fact.quality_flags


def test_missing_and_pre_period_disclosure_are_hard_currentness_failures():
    missing_card = _card(
        'ev_missing_disclosure', '300750', '2025-06-30', '1888.12',
        publish_time='',
    )
    early_card = _card(
        'ev_early_disclosure', '002594', '2025-06-30', '1703.60',
        publish_time='2025-06-29',
    )
    facts = FinancialNormalizer().normalize([missing_card, early_card])
    by_uid = {fact.evidence_uid: fact for fact in facts}

    assert 'missing_disclosure_time' in by_uid['ev_missing_disclosure'].quality_flags
    assert 'invalid_disclosure_time' in by_uid['ev_early_disclosure'].quality_flags

    for fact in facts:
        # Normalisation sorts facts, so select the matching immutable evidence.
        matching_card = early_card if fact.evidence_uid == 'ev_early_disclosure' else missing_card
        claim = ClaimCard(
            claim_id=f'claim_{fact.evidence_uid}',
            dimension='增长驱动',
            assertion=EvidenceAuditor.canonical_fact_statement([fact]),
            role='成长与变化视角',
            round_number=1,
            evidence_uids=[fact.evidence_uid],
            fact_uids=[fact.fact_uid],
            fact_assertions=[fact.to_dict()],
            supporting_quotes={fact.evidence_uid: [matching_card['card']['title']]},
        )
        score = EvidenceAuditor({'items': [matching_card]}, [fact]).audit_claim(claim)
        assert not score.currentness_pass


def test_disclosure_on_or_after_period_end_remains_valid():
    fact = FinancialNormalizer().normalize([
        _card(
            'ev_valid_disclosure', '300750', '2025-06-30', '1888.12',
            publish_time='2025-08-30',
        )
    ])[0]

    assert fact.disclosure_time == '2025-08-30'
    assert 'missing_disclosure_time' not in fact.quality_flags
    assert 'invalid_disclosure_time' not in fact.quality_flags


def test_direct_financial_body_does_not_reintroduce_rejected_disclosure(monkeypatch):
    fact = FinancialNormalizer().normalize([
        _card(
            'ev_early_body', '300750', '2025-06-30', '1888.12',
            publish_time='2025-06-29',
        )
    ])[0]
    evidence = SimpleNamespace(
        evidence_uid='ev_early_body',
        excerpt='营业总收入为 1888.12 亿元。',
        title='300750 财务数据',
    )

    class Store:
        @staticmethod
        def search(_query, limit=8):
            return [evidence][:limit]

        @staticmethod
        def display_id(_card):
            return 'E1'

    analyst = object.__new__(Analyst)
    analyst.store = Store()
    analyst.has_private_evidence = False
    analyst.logger = SimpleNamespace(log=lambda *_args, **_kwargs: None)
    analyst._load_normalized_facts = lambda: [fact.to_dict()]
    monkeypatch.setattr('app.services.memory_service.style_directives', lambda: '')
    monkeypatch.setattr('app.services.playbook.get_rules', lambda *_args, **_kwargs: [])

    body = analyst.write_section_heuristic(
        {'title': '财务表现', 'goal': '整理财务指标'},
        [],
    )

    assert '暂无同口径数据' in body
    assert '1888.12' not in body
    assert '[E1]' not in body
