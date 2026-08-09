"""AgentTeams Writer may render only durable accepted/hard-pass Claims."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.mcp.server import TOOL_SCHEMAS
from app.models.research_task import ResearchTask
from app.models.task_card import SymbolRef, TaskCard
from app.services.agentteam_runtime import AgentTeamRuntime
from app.team import AgentTeamStore, TeamInvariantError
from app.team.contracts import DEFAULT_TEAM_DAG
from app.utils import db as dbmod


def _team_task(snapshot, task_key: str):
    return next(item for item in snapshot['tasks'] if item['task_key'] == task_key)


def _complete_prerequisite(team_id: str, task_key: str, ordinal: int) -> None:
    snapshot = AgentTeamStore.get_team(team_id)
    task = _team_task(snapshot, task_key)
    snapshot = AgentTeamStore.transition_task(
        team_id,
        task['team_task_id'],
        'running',
        expected_version=task['state_version'],
        idempotency_key=f'writer-gate-{ordinal}-{task_key}-running',
        actor=task['assigned_agent'],
    )
    task = _team_task(snapshot, task_key)
    AgentTeamStore.transition_task(
        team_id,
        task['team_task_id'],
        'completed',
        expected_version=task['state_version'],
        idempotency_key=f'writer-gate-{ordinal}-{task_key}-completed',
        actor=task['assigned_agent'],
        output={'_idempotency_key': f'writer-gate-{ordinal}-{task_key}'},
    )


def _audit_row(claim_id: str, *, hard_pass: bool) -> dict:
    return {
        'claim_id': claim_id,
        'citation_pass': hard_pass,
        'numeric_pass': hard_pass,
        'comparability_pass': hard_pass,
        'currentness_pass': hard_pass,
        'compliance_pass': hard_pass,
        # Deliberately opposite: runtime must recompute from component flags.
        'hard_pass': not hard_pass,
        'evidence_coverage': 1.0 if hard_pass else 0.0,
        'counterevidence_resilience': 1.0 if hard_pass else 0.0,
        'relevance': 1.0,
        'issues': [] if hard_pass else ['citation:test_failure'],
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
        encoding='utf-8',
    )


@pytest.fixture
def writer_gate(tmp_path, monkeypatch):
    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'writer-gate.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    dbmod.init_db()

    task = ResearchTask(
        task_id='task_writer_claim_gate',
        requirement='验证 Writer 只呈现已审计 Claim',
    )
    card = TaskCard(
        deliverable='summary',
        symbols=[SymbolRef(code='600000', name='浦发银行')],
        time_window={'start': '2025-01-01', 'end': '2025-06-30'},
        analysis_mode='evidence_debate',
        execution_mode='agentteams',
    )
    task.set_task_card(card)
    run_id = task.create_run(card.to_dict())
    team_id = f'team-{run_id}'
    AgentTeamStore.create_team_run(
        team_id,
        run_id,
        task.task_id,
        config={'analysis_mode': 'evidence_debate'},
    )
    for ordinal, template in enumerate(DEFAULT_TEAM_DAG[:7], start=1):
        _complete_prerequisite(team_id, template.task_key, ordinal)

    run_folder = Path(task.run_folder(run_id))
    evidence_index = {
        'schema_version': 1,
        'task_id': task.task_id,
        'run_id': run_id,
        'items': [{
            'evidence_uid': 'ev_public_1',
            'display_id': 'E1',
            'card': {
                'evidence_uid': 'ev_public_1',
                'source_type': 'announcement',
                'source_name': '交易所公告',
                'title': '公司经营公告',
                'publish_time': '2025-04-30',
                'symbol': '600000',
                'excerpt': '公司公告披露了经营情况。',
                'structured': {},
                'provenance': {'provider': 'fixture', 'license_scope': 'public'},
                'reliability': 5,
                'fetch_tool': 'fixture',
            },
        }],
    }
    (run_folder / 'evidence_index.json').write_text(
        json.dumps(evidence_index, ensure_ascii=False), encoding='utf-8'
    )
    claims = [
        {
            'claim_id': 'claim_accepted_1',
            'dimension': '经营变化',
            'assertion': '公司公告披露了经营情况。',
            'role': '稳健与质量视角',
            'round': 1,
            'evidence_uids': ['ev_public_1'],
            'supporting_quotes': {
                'ev_public_1': ['公司公告披露了经营情况。'],
            },
            'assumptions': ['若后续披露口径保持一致'],
            'status': 'accepted',
        },
        {
            'claim_id': 'claim_rejected_1',
            'dimension': '增长驱动',
            'assertion': '未接受的高速增长结论。',
            'role': '成长与变化视角',
            'round': 1,
            'evidence_uids': ['ev_public_1'],
            'supporting_quotes': {
                'ev_public_1': ['公司公告披露了经营情况。'],
            },
            'status': 'rejected',
        },
    ]
    _write_jsonl(run_folder / 'debate' / 'claims.jsonl', claims)
    _write_jsonl(
        run_folder / 'debate' / 'audit.jsonl',
        [
            _audit_row('claim_accepted_1', hard_pass=True),
            _audit_row('claim_rejected_1', hard_pass=False),
        ],
    )
    verdict_path = run_folder / 'debate' / 'verdict.json'
    verdict_path.write_text(
        json.dumps({
            'status': 'complete',
            'generated_by': 'judge',
            'accepted_claim_ids': ['claim_accepted_1'],
            # These strings must never be auto-expanded into the candidate.
            'unresolved_disputes': ['未接受的高速增长结论。'],
            'evidence_gaps': ['Worker 自由文本缺口'],
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    monkeypatch.setattr(
        AgentTeamRuntime,
        '_publish_refs',
        lambda self, paths, *, artifact_type, producer=None: ([], False),
    )
    yield {
        'task': task,
        'run_id': run_id,
        'team_id': team_id,
        'run_folder': run_folder,
        'verdict_path': verdict_path,
        'claims': claims,
    }

    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None


def _version(context) -> int:
    return int(AgentTeamStore.get_team(context['team_id'])['team']['state_version'])


def _write(context, draft: dict, key: str = 'writer-gated-draft') -> dict:
    return AgentTeamRuntime(
        context['task'].task_id,
        context['run_id'],
        'report-writer',
    ).store_report_draft(
        draft,
        expected_version=_version(context),
        idempotency_key=key,
    )


def test_hard_pass_accepted_claim_is_deterministically_written(writer_gate):
    result = _write(writer_gate, {
        'title': '恶意标题：未接受的高速增长结论',
        'summary': '未接受的高速增长结论。',
        'sections': [{
            'claim_ids': ['claim_accepted_1'],
            'title': '自由事实标题',
            'content': '未接受的高速增长结论。',
        }],
    })
    assert result['claim_gate_enforced'] is True
    assert result['accepted_claim_ids'] == ['claim_accepted_1']

    draft = json.loads(
        (writer_gate['run_folder'] / 'report_draft_v1.json').read_text(
            encoding='utf-8'
        )
    )
    assert draft['title'] == '成竹证据审计报告'
    assert '未接受的高速增长结论' not in draft['summary']
    assert draft['sections'][0]['claim_ids'] == ['claim_accepted_1']
    assert draft['sections'][0]['evidence_uids'] == ['ev_public_1']
    assert '公司公告披露了经营情况。 [E1]' in draft['sections'][0]['content']
    assert '若后续披露口径保持一致 [E1]' in draft['sections'][0]['content']
    assert '未接受的高速增长结论' not in json.dumps(
        draft, ensure_ascii=False
    )

    # Exercise the formal reviewed candidate too: the assembler must not
    # re-expand rejected/disputed verdict prose behind the Writer gate.
    validated = AgentTeamRuntime(
        writer_gate['task'].task_id,
        writer_gate['run_id'],
        'compliance-reviewer',
    ).validate_report(
        expected_version=_version(writer_gate),
        idempotency_key='validate-gated-candidate',
    )
    assert validated['valid'] is True
    candidate = json.loads(
        (writer_gate['run_folder'] / validated['candidate_path']).read_text(
            encoding='utf-8'
        )
    )
    candidate_text = json.dumps(candidate, ensure_ascii=False)
    assert '公司公告披露了经营情况。' in candidate_text
    assert '未接受的高速增长结论' not in candidate_text
    assert 'Worker 自由文本缺口' not in candidate_text


@pytest.mark.parametrize('claim_id', ['claim_rejected_1', 'missing_claim'])
def test_unaccepted_or_absent_claim_id_is_rejected(writer_gate, claim_id):
    with pytest.raises(
        TeamInvariantError,
        match='Judge 接受且 hard-pass',
    ):
        _write(
            writer_gate,
            {'sections': [{'claim_ids': [claim_id]}]},
            key=f'writer-reject-{claim_id}',
        )
    assert not (writer_gate['run_folder'] / 'report_draft_v1.json').exists()


def test_verdict_cannot_accept_a_hard_failed_claim(writer_gate):
    claims = list(writer_gate['claims'])
    claims[1] = {**claims[1], 'status': 'accepted'}
    _write_jsonl(writer_gate['run_folder'] / 'debate' / 'claims.jsonl', claims)
    writer_gate['verdict_path'].write_text(
        json.dumps({
            'status': 'complete',
            'generated_by': 'judge',
            'accepted_claim_ids': ['claim_rejected_1'],
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    with pytest.raises(TeamInvariantError, match='未通过确定性硬校验'):
        _write(
            writer_gate,
            {'sections': [{'claim_ids': ['claim_rejected_1']}]},
            key='writer-reject-hard-fail',
        )
    assert not (writer_gate['run_folder'] / 'report_draft_v1.json').exists()


def test_omitted_accepted_claim_is_automatically_covered(writer_gate):
    claims = list(writer_gate['claims'])
    claims.append({
        'claim_id': 'claim_accepted_2',
        'dimension': '经营变化',
        'assertion': '公司经营公告已进入冻结证据快照。',
        'role': '成长与变化视角',
        'round': 1,
        'evidence_uids': ['ev_public_1'],
        'supporting_quotes': {
            'ev_public_1': ['公司公告披露了经营情况。'],
        },
        'status': 'accepted',
    })
    _write_jsonl(writer_gate['run_folder'] / 'debate' / 'claims.jsonl', claims)
    _write_jsonl(
        writer_gate['run_folder'] / 'debate' / 'audit.jsonl',
        [
            _audit_row('claim_accepted_1', hard_pass=True),
            _audit_row('claim_rejected_1', hard_pass=False),
            _audit_row('claim_accepted_2', hard_pass=True),
        ],
    )
    writer_gate['verdict_path'].write_text(
        json.dumps({
            'status': 'complete',
            'generated_by': 'judge',
            'accepted_claim_ids': ['claim_accepted_1', 'claim_accepted_2'],
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    result = _write(
        writer_gate,
        {'sections': [{'claim_ids': ['claim_accepted_1']}]},
        key='writer-auto-cover',
    )
    assert result['accepted_claim_ids'] == [
        'claim_accepted_1', 'claim_accepted_2',
    ]
    draft = json.loads(
        (writer_gate['run_folder'] / 'report_draft_v1.json').read_text(
            encoding='utf-8'
        )
    )
    rendered_ids = [
        claim_id
        for section in draft['sections']
        for claim_id in section['claim_ids']
    ]
    assert rendered_ids == ['claim_accepted_1', 'claim_accepted_2']
    assert '公司经营公告已进入冻结证据快照。' in json.dumps(
        draft, ensure_ascii=False
    )


def test_empty_accepted_set_produces_safe_gap_version(writer_gate):
    claims = list(writer_gate['claims'])
    claims[0] = {**claims[0], 'status': 'disputed'}
    _write_jsonl(writer_gate['run_folder'] / 'debate' / 'claims.jsonl', claims)
    writer_gate['verdict_path'].write_text(
        json.dumps({
            'status': 'complete',
            'generated_by': 'judge',
            'accepted_claim_ids': [],
            'unresolved_disputes': ['未接受的高速增长结论。'],
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    result = _write(writer_gate, {
        'title': '自由标题',
        'summary': '未接受的高速增长结论。',
        'sections': [{
            'content': '未接受的高速增长结论。',
            'claim_ids': [],
        }],
    }, key='writer-safe-empty')
    assert result['accepted_claim_ids'] == []
    draft = json.loads(
        (writer_gate['run_folder'] / 'report_draft_v1.json').read_text(
            encoding='utf-8'
        )
    )
    assert len(draft['sections']) == 1
    assert draft['sections'][0]['title'] == '证据不足'
    assert draft['sections'][0]['claim_ids'] == []
    assert '不形成事实性结论' in draft['sections'][0]['content']
    assert '未接受的高速增长结论' not in json.dumps(
        draft, ensure_ascii=False
    )


def test_store_report_draft_schema_exposes_claim_selectors():
    schema = TOOL_SCHEMAS['store_report_draft']['inputSchema']
    draft = schema['properties']['draft']
    assert draft['required'] == ['sections']
    sections = draft['properties']['sections']
    assert sections['maxItems'] == 16
    assert sections['items']['properties']['claim_ids']['maxItems'] == 48
    assert sections['items']['additionalProperties'] is False
