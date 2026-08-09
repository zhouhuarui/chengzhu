"""Offline contract coverage for the seven-node direct AgentTeams path."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus
from app.models.task_card import SymbolRef, TaskCard
from app.services import agentteam_runtime as runtime_module
from app.services.agentteam_runtime import AgentTeamRuntime
from app.services.artifact_store import ArtifactRef
from app.team import (
    AgentTeamStore,
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
)
from app.team.publication import coordinate_approval
from app.utils import db as dbmod


def _task(snapshot, task_key: str):
    return next(item for item in snapshot['tasks'] if item['task_key'] == task_key)


def _version(context) -> int:
    return int(
        AgentTeamStore.get_team(context['team_id'])['team']['state_version']
    )


@pytest.fixture
def direct_runtime(tmp_path, monkeypatch):
    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'direct-runtime.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    dbmod.init_db()

    failed_groups: set[str] = set()

    task = ResearchTask(
        task_id='task_direct_contract',
        requirement='验证 direct 模式 AgentTeams 契约',
    )
    card = TaskCard(
        deliverable='summary',
        symbols=[SymbolRef(code='600000', name='浦发银行')],
        time_window={'start': '2025-01-01', 'end': '2025-06-30'},
        analysis_mode='direct',
        execution_mode='agentteams',
    )
    task.set_task_card(card)
    run_id = task.create_run(card.to_dict())
    task.begin_run(
        run_id,
        ResearchTaskStatus.COLLECTING,
        'AgentTeams 已确认',
        1,
        analysis_mode='direct',
    )
    dbmod.insert_task_run(run_id, task.task_id, card.to_dict(), 'collecting')
    team_id = f'team-{run_id}'
    AgentTeamStore.create_team_run(
        team_id,
        run_id,
        task.task_id,
        idempotency_key=f'create-{run_id}',
        config={'analysis_mode': 'direct', 'execution_mode': 'agentteams'},
    )

    # Collection stays entirely offline but still writes realistic staging
    # artifacts and returns the same bounded result contract as production.
    from app.services import collect_orchestrator

    monkeypatch.setattr(
        collect_orchestrator,
        '_collect_uploaded_files',
        lambda *args, **kwargs: None,
    )

    def fake_collector(
        research_task,
        agent,
        _card,
        _logger,
        selected_run_id=None,
        _deadline=None,
    ):
        group = (
            'disclosure'
            if agent in {'announcement', 'financial'}
            else 'market_context'
        )
        ok = group not in failed_groups
        evidence_path = (
            Path(research_task.run_folder(selected_run_id))
            / 'evidence'
            / f'{agent}.jsonl'
        )
        if ok:
            evidence_path.write_text(
                json.dumps(
                    {
                        'source_type': agent,
                        'title': f'{agent} fixture',
                        'excerpt': '可核验证据原文',
                    },
                    ensure_ascii=False,
                )
                + '\n',
                encoding='utf-8',
            )
        return {
            'agent': agent,
            'ok': ok,
            'cards': 1 if ok else 0,
            **({} if ok else {'error': f'{group}_offline_failure'}),
        }

    monkeypatch.setattr(collect_orchestrator, '_run_one_collector', fake_collector)

    def fake_ingest(task_id, *, logger, run_id):
        graph_path = Path(task.run_folder(run_id)) / 'graph.json'
        graph_path.write_text('{"nodes": [], "links": []}', encoding='utf-8')
        return {'nodes': 0, 'links': 0}

    monkeypatch.setattr(runtime_module, 'ingest_task_evidence', fake_ingest)

    def fake_freeze(research_task, selected_run_id):
        index = {
            'schema_version': 1,
            'items': [{
                'evidence_uid': 'ev_direct_fixture',
                'display_id': '[E001]',
                'card': {
                    'source_type': 'announcement',
                    'title': '冻结证据',
                    'excerpt': '可核验证据原文',
                    'publish_time': '2025-03-01',
                    'symbol': '600000',
                },
            }],
        }
        index_path = Path(research_task.run_folder(selected_run_id)) / 'evidence_index.json'
        index_path.write_text(
            json.dumps(index, ensure_ascii=False), encoding='utf-8'
        )
        return SimpleNamespace(cards=[index['items'][0]['card']]), index

    monkeypatch.setattr(runtime_module, 'freeze_evidence', fake_freeze)
    monkeypatch.setattr(
        runtime_module.FinancialNormalizer,
        'normalize',
        lambda self, items: [],
    )

    # Produce immutable local ArtifactRefs without attempting MinIO.  This is
    # a storage-port fake, not a bypass of the runtime/state-machine contract.
    publish_state = {'calls': 0, 'forbidden': False}

    def fake_publish_refs(
        runtime,
        paths,
        *,
        artifact_type,
        producer=None,
    ):
        publish_state['calls'] += 1
        if publish_state['forbidden']:
            raise AssertionError('artifact publish must not be called')
        refs = []
        for raw_path in paths:
            path = Path(raw_path)
            if not path.is_file():
                continue
            payload = path.read_bytes()
            relative = path.relative_to(Path(runtime.run_folder)).as_posix()
            refs.append(ArtifactRef(
                artifact_type=artifact_type,
                uri=f'local://{runtime.task_id}/{runtime.run_id}/{relative}',
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                producer=producer or runtime.role,
                schema_version=1,
            ))
        return refs, False

    monkeypatch.setattr(AgentTeamRuntime, '_publish_refs', fake_publish_refs)

    def fake_review(self, draft):
        return {
            **draft,
            'sections': [
                {**section, 'issues': [], 'verdict': 'pass'}
                for section in draft.get('sections') or []
            ],
        }

    monkeypatch.setattr(runtime_module.Reviewer, 'run', fake_review)

    def fake_assemble(task_id, reviewed, *, run_id=None, publish=False):
        return {
            'task_id': task_id,
            'run_id': run_id,
            'title': reviewed.get('title') or 'Direct 契约报告',
            'sections': list(reviewed.get('sections') or []),
            'markdown': '# Direct 契约报告',
        }

    monkeypatch.setattr(runtime_module, 'assemble_report', fake_assemble)

    # Publication coordination still persists the Vue decision and promotes
    # the artifact; only filesystem/graph mirrors are replaced with no-ops.
    from app.team import publication

    monkeypatch.setattr(publication, '_publish_candidate', lambda *args: None)
    monkeypatch.setattr(
        publication.graph_ingest,
        'publish_latest_graph',
        lambda *args: None,
    )

    context = {
        'task': task,
        'run_id': run_id,
        'team_id': team_id,
        'failed_groups': failed_groups,
        'publish_state': publish_state,
    }
    yield context

    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None


def _start_and_collect(context, failed_groups=()):
    context['failed_groups'].update(failed_groups)
    AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'research-lead'
    ).start_team_run(
        expected_version=_version(context),
        idempotency_key='direct-start',
    )
    disclosure = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'disclosure-researcher'
    ).collect_evidence(
        'disclosure',
        expected_version=_version(context),
        idempotency_key='direct-collect-disclosure',
    )
    market = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'market-context-researcher'
    ).collect_evidence(
        'market_context',
        expected_version=_version(context),
        idempotency_key='direct-collect-market',
    )
    return disclosure, market


def _freeze(context):
    return AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'research-lead'
    ).freeze_evidence(
        expected_version=_version(context),
        idempotency_key='direct-freeze',
    )


def _advance_to_approval_request(context):
    judged = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'evidence-judge'
    ).audit_debate(
        [],
        expected_version=_version(context),
        idempotency_key='direct-judge',
    )
    written = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'report-writer'
    ).store_report_draft(
        {
            'title': 'Direct 契约报告',
            'sections': [{
                'title': '证据说明',
                'content': '本报告仅整理冻结证据，不给出投资建议。',
            }],
        },
        expected_version=_version(context),
        idempotency_key='direct-writer',
    )
    reviewer = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'compliance-reviewer'
    )
    validated = reviewer.validate_report(
        expected_version=_version(context),
        idempotency_key='direct-validate',
    )
    assert validated['valid'] is True
    reviewed = reviewer.submit_review(
        'pass',
        [],
        expected_version=_version(context),
        idempotency_key='direct-review-pass',
    )
    expected = _version(context)
    approval = AgentTeamRuntime(
        context['task'].task_id, context['run_id'], 'research-lead'
    ).request_publish_approval(
        expected_version=expected,
        idempotency_key='direct-request-approval',
    )
    return judged, written, reviewed, approval, expected


def test_direct_runtime_skips_analysts_and_reaches_vue_approval(direct_runtime):
    disclosure, market = _start_and_collect(direct_runtime)
    assert disclosure['degraded'] is False
    assert market['degraded'] is False

    frozen = _freeze(direct_runtime)
    assert frozen['completed_partial'] is False
    snapshot = AgentTeamStore.get_team(direct_runtime['team_id'])
    for analyst_key in ('quality-analysis', 'growth-analysis'):
        analyst = _task(snapshot, analyst_key)
        assert analyst['status'] == 'skipped'
        assert analyst['attempt_count'] == 0
        assert analyst['budget_cny'] == 0
        assert analyst['output'] == {
            'analysis_mode': 'direct',
            'reason': 'dual analyst debate disabled by TaskCard',
        }
        # Assert the recovery source of truth, not just the assembled snapshot.
        durable = dbmod.get_team_task(
            direct_runtime['team_id'], analyst['team_task_id']
        )
        assert durable['status'] == 'skipped'

    judge = _task(snapshot, 'evidence-judgement')
    assert set(judge['depends_on']) == {
        _task(snapshot, 'quality-analysis')['team_task_id'],
        _task(snapshot, 'growth-analysis')['team_task_id'],
    }

    judged, written, reviewed, approval, pre_approval_version = (
        _advance_to_approval_request(direct_runtime)
    )
    assert judged['accepted_claim_ids'] == []
    assert written['writer_version'] == 1
    assert reviewed['decision'] == 'pass'
    assert approval['team']['team']['status'] == 'awaiting_approval'
    assert approval['team']['team']['current_stage'] == (
        'awaiting_publish_approval'
    )
    assert approval['artifact']['requires_approval'] is True
    assert approval['artifact']['status'] == 'awaiting_approval'

    # The same idempotency key replays despite carrying the old team version,
    # without requiring MinIO again. A distinct stale request fails before
    # any external upload.
    calls_before_retry = direct_runtime['publish_state']['calls']
    direct_runtime['publish_state']['forbidden'] = True
    replay = AgentTeamRuntime(
        direct_runtime['task'].task_id,
        direct_runtime['run_id'],
        'research-lead',
    ).request_publish_approval(
        expected_version=pre_approval_version,
        idempotency_key='direct-request-approval',
    )
    assert replay['artifact']['artifact_id'] == approval['artifact']['artifact_id']
    assert direct_runtime['publish_state']['calls'] == calls_before_retry
    with pytest.raises(TeamConflictError):
        AgentTeamRuntime(
            direct_runtime['task'].task_id,
            direct_runtime['run_id'],
            'research-lead',
        ).request_publish_approval(
            expected_version=pre_approval_version,
            idempotency_key='direct-request-approval-stale-new',
        )
    assert direct_runtime['publish_state']['calls'] == calls_before_retry

    # Reusing the committed key after the candidate bytes change is an
    # idempotency conflict and also remains side-effect free.
    candidate_path = (
        Path(direct_runtime['task'].run_folder(direct_runtime['run_id']))
        / approval['artifact']['metadata']['candidate_path']
    )
    candidate_path.write_text('{"changed": true}', encoding='utf-8')
    with pytest.raises(TeamIdempotencyError):
        AgentTeamRuntime(
            direct_runtime['task'].task_id,
            direct_runtime['run_id'],
            'research-lead',
        ).request_publish_approval(
            expected_version=pre_approval_version,
            idempotency_key='direct-request-approval',
        )
    assert direct_runtime['publish_state']['calls'] == calls_before_retry


def test_single_collection_group_failure_publishes_completed_partial(
    direct_runtime,
):
    disclosure, market = _start_and_collect(
        direct_runtime, failed_groups={'disclosure'}
    )
    assert disclosure['degraded'] is True
    assert market['degraded'] is False

    frozen = _freeze(direct_runtime)
    assert frozen['completed_partial'] is True
    assert frozen['degraded'] is True
    assert frozen['team']['team']['status'] == 'running'
    assert frozen['team']['team']['degraded'] is True

    *_chain, approval, _expected = _advance_to_approval_request(direct_runtime)
    approved = coordinate_approval(
        direct_runtime['team_id'],
        approval['artifact']['artifact_id'],
        'approved',
        expected_version=_version(direct_runtime),
        idempotency_key='vue-approve-direct-partial',
        source='vue',
        actor='vue-user',
        reason='证据缺口已披露',
    )
    assert approved['snapshot']['team']['status'] == 'published'
    persisted_task = ResearchTask.load(direct_runtime['task'].task_id)
    assert persisted_task.status is ResearchTaskStatus.COMPLETED_PARTIAL
    assert persisted_task.progress_detail['report_ready'] is True
    assert dbmod.get_task_run(direct_runtime['run_id'])['status'] == (
        ResearchTaskStatus.COMPLETED_PARTIAL.value
    )


def test_both_collection_groups_failure_terminates_before_freeze(direct_runtime):
    disclosure, market = _start_and_collect(
        direct_runtime,
        failed_groups={'disclosure', 'market_context'},
    )
    assert disclosure['degraded'] is True
    assert market['degraded'] is True

    with pytest.raises(TeamInvariantError, match='禁止冻结空证据'):
        _freeze(direct_runtime)

    snapshot = AgentTeamStore.get_team(direct_runtime['team_id'])
    assert snapshot['team']['status'] == 'failed'
    assert snapshot['team']['current_stage'] == 'failed'
    assert snapshot['team']['terminal_reason'] == 'all_collectors_failed'
    assert _task(snapshot, 'evidence-freeze')['status'] == 'failed'
    assert _task(snapshot, 'quality-analysis')['status'] == 'pending'
    assert _task(snapshot, 'growth-analysis')['status'] == 'pending'
    assert _task(snapshot, 'evidence-judgement')['status'] == 'pending'
    persisted_task = ResearchTask.load(direct_runtime['task'].task_id)
    assert persisted_task.status is ResearchTaskStatus.FAILED
    assert dbmod.get_task_run(direct_runtime['run_id'])['status'] == (
        ResearchTaskStatus.FAILED.value
    )
