"""Agent Team persistence, CAS, approval gate and rollback invariants."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services.agent_logger import AgentLogger
from app.team import (
    AgentTeamStore,
    TASK_CONTRACT_FIELDS,
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    build_task_contract,
)
from app.utils import db as dbmod


@pytest.fixture
def team_runtime(tmp_path, monkeypatch):
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'team.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    dbmod.init_db()
    yield tmp_path
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None


def _create(team_id: str = 'team_fixture'):
    return AgentTeamStore.create_team_run(
        team_id,
        f'run_{team_id}',
        f'task_{team_id}',
        idempotency_key=f'create-{team_id}',
        config={'mode': 'agent_team'},
    )


def _handoff_payload(goal: str = '采集公司披露'):
    return {
        'task_contract': build_task_contract(
            goal=goal,
            inputs=[{'type': 'team_task', 'team_task_id': 'upstream'}],
            expected_outputs=['durable_result'],
            acceptance_criteria=['return immutable ArtifactRefs'],
            deadline={'timeout_seconds': 480},
            budget={'currency': 'CNY', 'limit_cny': 0.25},
            artifact_refs=[],
            trace_id='a' * 32,
        ),
    }


def test_default_dag_has_eight_fixed_agents_plus_system_freeze(team_runtime):
    snapshot = _create()
    assert snapshot['created'] is True
    assert snapshot['agent_roles'] == [
        'research-lead',
        'disclosure-researcher',
        'market-context-researcher',
        'quality-analyst',
        'growth-analyst',
        'evidence-judge',
        'report-writer',
        'compliance-reviewer',
    ]
    assert len(snapshot['tasks']) == 9
    by_key = {item['task_key']: item for item in snapshot['tasks']}
    freeze = by_key['evidence-freeze']
    assert freeze['assigned_agent'] == 'chengzhu-backend'
    assert freeze['role_id'] == 'system-freeze'
    assert set(freeze['depends_on']) == {
        by_key['disclosure-research']['team_task_id'],
        by_key['market-context-research']['team_task_id'],
    }
    assert set(by_key['evidence-judgement']['depends_on']) == {
        by_key['quality-analysis']['team_task_id'],
        by_key['growth-analysis']['team_task_id'],
    }
    assert sum(item['budget_cny'] for item in snapshot['tasks']) == pytest.approx(
        Config.LLM_COST_BUDGET_CNY
    )
    assert snapshot['team']['budget_cny'] == pytest.approx(
        Config.LLM_COST_BUDGET_CNY
    )
    assert snapshot['team']['current_stage'] == 'confirmed'
    assert all(item['attempt_count'] == 0 for item in snapshot['tasks'])
    assert all(
        set(item['input']) == set(TASK_CONTRACT_FIELDS)
        for item in snapshot['tasks']
    )
    assert all(
        item['input']['budget']['limit_cny'] == item['budget_cny']
        for item in snapshot['tasks']
    )

    replay = _create()
    assert replay['created'] is False
    assert len(replay['tasks']) == 9
    with pytest.raises(TeamIdempotencyError):
        AgentTeamStore.create_team_run(
            'another_team',
            'run_team_fixture',
            'task_other',
            idempotency_key='another-create',
        )


def test_failed_worker_can_retry_once_but_not_restart_forever(team_runtime):
    snapshot = _create('team_retry_limit')
    task = next(item for item in snapshot['tasks'] if item['task_key'] == 'research-plan')
    for attempt in (1, 2):
        snapshot = AgentTeamStore.transition_task(
            'team_retry_limit',
            task['team_task_id'],
            'running',
            expected_version=task['state_version'],
            idempotency_key=f'start-{attempt}',
            actor='research-lead',
        )
        task = next(
            item for item in snapshot['tasks']
            if item['task_key'] == 'research-plan'
        )
        assert task['attempt_count'] == attempt
        snapshot = AgentTeamStore.transition_task(
            'team_retry_limit',
            task['team_task_id'],
            'failed',
            expected_version=task['state_version'],
            idempotency_key=f'fail-{attempt}',
            actor='research-lead',
            error_code='worker_crashed',
        )
        task = next(
            item for item in snapshot['tasks']
            if item['task_key'] == 'research-plan'
        )

    with pytest.raises(TeamInvariantError, match='retry/revision limit'):
        AgentTeamStore.transition_task(
            'team_retry_limit',
            task['team_task_id'],
            'running',
            expected_version=task['state_version'],
            idempotency_key='start-third-time',
            actor='research-lead',
        )


def test_dispatch_metadata_is_durable_and_idempotent(team_runtime):
    created = _create('team_dispatch_metadata')
    legacy_task_id = created['tasks'][0]['team_task_id']
    with dbmod.db_cursor() as cur:
        cur.execute(
            "UPDATE team_task SET input_json = '{}' WHERE team_task_id = ?",
            (legacy_task_id,),
        )
    first = AgentTeamStore.record_dispatch_metadata(
        'team_dispatch_metadata',
        matrix_room_id='!room:agentteams.local',
        matrix_event_id='$event:agentteams.local',
        element_url='http://127.0.0.1:18088/#/room/%21room%3Aagentteams.local',
        trace_id='a' * 32,
        span_id='b' * 16,
        idempotency_key='run-dispatch',
    )
    replay = AgentTeamStore.record_dispatch_metadata(
        'team_dispatch_metadata',
        matrix_room_id='!room:agentteams.local',
        matrix_event_id='$event:agentteams.local',
        element_url='http://127.0.0.1:18088/#/room/%21room%3Aagentteams.local',
        trace_id='a' * 32,
        span_id='b' * 16,
        idempotency_key='run-dispatch',
    )
    assert first['team']['matrix_room_id'] == '!room:agentteams.local'
    assert replay['team']['trace_id'] == 'a' * 32
    assert replay['event_cursor'] == first['event_cursor']
    assert all(
        item['input']['trace_id'] == 'a' * 32
        for item in replay['tasks']
    )
    assert all(
        set(item['input']) == set(TASK_CONTRACT_FIELDS)
        for item in replay['tasks']
    )


def test_demo_visual_failure_is_claimed_once_in_durable_state(team_runtime):
    _create('team_demo_visual_failure')

    assert AgentTeamStore.claim_demo_visual_failure('team_demo_visual_failure') is True
    assert AgentTeamStore.claim_demo_visual_failure('team_demo_visual_failure') is False
    events = AgentTeamStore.list_events('team_demo_visual_failure', limit=100)
    injected = [
        item for item in events['events']
        if item['event_type'] == 'demo_visual_failure_injected'
    ]
    assert len(injected) == 1
    assert injected[0]['payload'] == {
        'scope': 'bailian-visual-proxy',
        'mode': 'fail-once-before-upstream',
    }


def test_team_state_uses_cas_and_mutations_are_idempotent(team_runtime):
    _create()
    with pytest.raises(TeamInvariantError, match='人工审批'):
        AgentTeamStore.transition_team(
            'team_fixture',
            'published',
            expected_version=0,
            idempotency_key='bypass-approval',
        )
    first = AgentTeamStore.transition_team(
        'team_fixture',
        'running',
        expected_version=0,
        idempotency_key='start-once',
        current_stage='research-plan',
    )
    assert first['team']['state_version'] == 1

    # A transport retry with the same key is a no-op even if it carries the
    # old expected version.  A distinct mutation with that version conflicts.
    replay = AgentTeamStore.transition_team(
        'team_fixture',
        'running',
        expected_version=0,
        idempotency_key='start-once',
        current_stage='research-plan',
    )
    assert replay['team']['state_version'] == 1
    with pytest.raises(TeamIdempotencyError):
        AgentTeamStore.transition_team(
            'team_fixture',
            'running',
            expected_version=0,
            idempotency_key='start-once',
            current_stage='disclosure-research',
        )
    with pytest.raises(TeamConflictError) as caught:
        AgentTeamStore.transition_team(
            'team_fixture',
            'awaiting_approval',
            expected_version=0,
            idempotency_key='stale-write',
        )
    assert caught.value.current_version == 1


def test_events_are_redacted_idempotent_and_cursor_paginated(team_runtime):
    _create()
    event = AgentTeamStore.append_event(
        'team_fixture',
        'member_note',
        actor='quality-analyst',
        idempotency_key='safe-event',
        payload={
            'prompt': 'do not persist this',
            'messages': [{'content': 'private'}],
            'metadata': {
                'authorization': 'Bearer super-secret',
                'matrix_access_token': 'opaque-secret-value',
                'note': 'api_key=top-secret',
                'token_count': 42,
                'private_data': {'account': 'private-account'},
                'source_text': 'full licensed document text',
                'unlabelled_blob': 'A' * 512,
            },
        },
    )
    assert event['payload']['prompt'] == '[REDACTED]'
    assert event['payload']['messages'] == '[REDACTED]'
    assert event['payload']['metadata']['authorization'] == '[REDACTED]'
    assert event['payload']['metadata']['matrix_access_token'] == '[REDACTED]'
    assert 'top-secret' not in event['payload']['metadata']['note']
    assert event['payload']['metadata']['token_count'] == 42
    assert event['payload']['metadata']['private_data'] == '[REDACTED]'
    assert event['payload']['metadata']['source_text'] == '[REDACTED]'
    assert event['payload']['metadata']['unlabelled_blob'] == (
        '[binary payload omitted]'
    )
    durable_payload = dbmod.list_team_events(
        'team_fixture', after_cursor=event['cursor'] - 1, limit=1,
    )[0]['payload_json']
    assert 'super-secret' not in durable_payload
    assert 'opaque-secret-value' not in durable_payload
    assert 'do not persist this' not in durable_payload

    replay = AgentTeamStore.append_event(
        'team_fixture',
        'member_note',
        actor='quality-analyst',
        idempotency_key='safe-event',
        payload={
            'prompt': 'different secret is still redacted',
            'messages': ['different'],
            'metadata': {
                'authorization': 'Bearer changed',
                'matrix_access_token': 'different-opaque-secret',
                'note': 'api_key=changed',
                'token_count': 42,
                'private_data': {'account': 'different-private-account'},
                'source_text': 'different licensed document text',
                'unlabelled_blob': 'B' * 512,
            },
        },
    )
    assert replay['cursor'] == event['cursor']

    page_one = AgentTeamStore.list_events('team_fixture', limit=1)
    assert len(page_one['events']) == 1
    assert page_one['has_more'] is True
    page_two = AgentTeamStore.list_events(
        'team_fixture',
        after_cursor=page_one['next_cursor'],
        limit=10,
    )
    assert all(
        item['cursor'] > page_one['next_cursor']
        for item in page_two['events']
    )


def test_agent_log_uses_the_same_privacy_and_binary_redaction_gate(team_runtime):
    logger = AgentLogger('task_safe_log', agent='research-lead', run_id='run_safe_log')
    logger.log(
        'member_note',
        'collecting',
        {
            'prompt': 'hidden prompt',
            'password': 'hidden password',
            'document_text': 'licensed full text',
            'payload': 'C' * 512,
            'summary': 'bounded operational summary',
        },
    )
    with open(logger.log_file_path, 'r', encoding='utf-8') as handle:
        entry = json.loads(handle.readline())
    assert entry['details'] == {
        'prompt': '[REDACTED]',
        'password': '[REDACTED]',
        'document_text': '[REDACTED]',
        'payload': '[binary payload omitted]',
        'summary': 'bounded operational summary',
    }


def test_handoff_is_idempotent_and_its_status_uses_cas(team_runtime):
    snapshot = _create()
    by_key = {item['task_key']: item for item in snapshot['tasks']}
    payload = _handoff_payload()
    handoff = AgentTeamStore.create_handoff(
        'team_fixture',
        source_task_id=by_key['research-plan']['team_task_id'],
        target_task_id=by_key['disclosure-research']['team_task_id'],
        from_agent='research-lead',
        to_agent='disclosure-researcher',
        payload=payload,
        idempotency_key='lead-to-disclosure',
    )
    assert handoff['status'] == 'pending'
    replay = AgentTeamStore.create_handoff(
        'team_fixture',
        source_task_id=by_key['research-plan']['team_task_id'],
        target_task_id=by_key['disclosure-research']['team_task_id'],
        from_agent='research-lead',
        to_agent='disclosure-researcher',
        payload=payload,
        idempotency_key='lead-to-disclosure',
    )
    assert replay['handoff_id'] == handoff['handoff_id']
    with pytest.raises(TeamIdempotencyError):
        AgentTeamStore.create_handoff(
            'team_fixture',
            source_task_id=by_key['research-plan']['team_task_id'],
            target_task_id=by_key['disclosure-research']['team_task_id'],
            from_agent='research-lead',
            to_agent='market-context-researcher',
            payload=payload,
            idempotency_key='lead-to-disclosure',
        )

    with pytest.raises(TeamInvariantError, match='TaskContract'):
        AgentTeamStore.create_handoff(
            'team_fixture',
            source_task_id=by_key['research-plan']['team_task_id'],
            target_task_id=by_key['market-context-research']['team_task_id'],
            from_agent='research-lead',
            to_agent='market-context-researcher',
            payload={'summary': 'legacy free-form handoff'},
            idempotency_key='invalid-free-form-handoff',
        )

    accepted = AgentTeamStore.transition_handoff(
        'team_fixture',
        handoff['handoff_id'],
        'accepted',
        expected_version=0,
        idempotency_key='accept-handoff',
        actor='disclosure-researcher',
    )
    assert accepted['state_version'] == 1
    with pytest.raises(TeamConflictError):
        AgentTeamStore.transition_handoff(
            'team_fixture',
            handoff['handoff_id'],
            'completed',
            expected_version=0,
            idempotency_key='stale-handoff',
            actor='disclosure-researcher',
        )


def test_publication_requires_vue_approval_and_rejection_retry_is_terminal(team_runtime):
    _create('team_unapproved')
    artifact = AgentTeamStore.register_artifact(
        'team_unapproved',
        artifact_type='report',
        uri='runs/one/report.json',
        expected_version=0,
        idempotency_key='draft-one',
    )
    with pytest.raises(TeamIdempotencyError):
        AgentTeamStore.register_artifact(
            'team_unapproved',
            artifact_type='report',
            uri='runs/one/report.json',
            sha256='a' * 64,
            expected_version=0,
            idempotency_key='draft-one',
        )
    with pytest.raises(TeamInvariantError, match='批准'):
        AgentTeamStore.publish_artifact(
            'team_unapproved',
            artifact['artifact_id'],
            expected_version=1,
            idempotency_key='forbidden-publish',
        )
    with pytest.raises(TeamInvariantError, match='Vue'):
        AgentTeamStore.decide_approval(
            'team_unapproved',
            artifact['artifact_id'],
            'approved',
            expected_version=1,
            idempotency_key='agent-approval',
            authority='agent',
            actor='evidence-judge',
        )

    _create('team_rejected')
    first = AgentTeamStore.register_artifact(
        'team_rejected',
        artifact_type='report',
        uri='runs/one/report.json',
        expected_version=0,
        idempotency_key='report-v1',
    )
    rejected_once = AgentTeamStore.decide_approval(
        'team_rejected',
        first['artifact_id'],
        'rejected',
        expected_version=1,
        idempotency_key='reject-v1',
        authority='vue',
        actor='vue-user',
        reason='引用不足',
    )
    assert rejected_once['snapshot']['team']['status'] == 'changes_requested'
    assert rejected_once['snapshot']['team']['rejection_count'] == 1

    replay = AgentTeamStore.decide_approval(
        'team_rejected',
        first['artifact_id'],
        'rejected',
        expected_version=1,
        idempotency_key='reject-v1',
        authority='vue',
        actor='vue-user',
        reason='引用不足',
    )
    assert replay['replayed'] is True
    assert replay['snapshot']['team']['rejection_count'] == 1

    second = AgentTeamStore.register_artifact(
        'team_rejected',
        artifact_type='report',
        uri='runs/two/report.json',
        expected_version=2,
        idempotency_key='report-v2',
    )
    rejected_twice = AgentTeamStore.decide_approval(
        'team_rejected',
        second['artifact_id'],
        'rejected',
        expected_version=3,
        idempotency_key='reject-v2',
        authority='vue',
        actor='vue-user',
        reason='仍不通过',
    )
    team = rejected_twice['snapshot']['team']
    assert team['status'] == 'rejected_terminal'
    assert team['rejection_count'] == 2
    assert team['terminal_reason'] == 'human_rejected_twice'
    with pytest.raises(TeamInvariantError, match='terminal'):
        AgentTeamStore.register_artifact(
            'team_rejected',
            artifact_type='report',
            uri='runs/three/report.json',
            expected_version=4,
            idempotency_key='report-v3',
        )


def test_rollback_only_switches_latest_and_appends_audit_event(team_runtime):
    _create('team_rollback')
    first = AgentTeamStore.register_artifact(
        'team_rollback',
        artifact_type='report',
        uri='runs/one/report.json',
        sha256='a' * 64,
        metadata={'label': 'v1'},
        expected_version=0,
        idempotency_key='report-v1',
    )
    approved_one = AgentTeamStore.decide_approval(
        'team_rollback',
        first['artifact_id'],
        'approved',
        expected_version=1,
        idempotency_key='approve-v1',
        authority='vue',
        actor='vue-user',
    )
    assert approved_one['snapshot']['team']['latest_artifact_id'] == first['artifact_id']

    # A published Team is terminal. A new report version is a distinct run
    # and Team, while rollback may point that latest run at any earlier
    # approved artifact owned by the same task.
    AgentTeamStore.create_team_run(
        'team_rollback_v2',
        'run_team_rollback_v2',
        'task_team_rollback',
        idempotency_key='create-team-rollback-v2',
        config={'mode': 'agent_team'},
    )
    second = AgentTeamStore.register_artifact(
        'team_rollback_v2',
        artifact_type='report',
        uri='runs/two/report.json',
        sha256='b' * 64,
        metadata={'label': 'v2'},
        expected_version=0,
        idempotency_key='report-v2',
    )
    approved_two = AgentTeamStore.decide_approval(
        'team_rollback_v2',
        second['artifact_id'],
        'approved',
        expected_version=1,
        idempotency_key='approve-v2',
        authority='vue',
        actor='vue-user',
    )
    before_first = AgentTeamStore.resolve_published_artifact_by_id(
        'task_team_rollback', first['artifact_id'],
    )
    before_second = AgentTeamStore.resolve_published_artifact_by_id(
        'task_team_rollback', second['artifact_id'],
    )

    result = AgentTeamStore.rollback_artifact(
        'team_rollback_v2',
        first['artifact_id'],
        expected_version=2,
        idempotency_key='rollback-to-v1',
        authority='vue',
        actor='vue-user',
        reason='v2 展示异常',
    )
    snapshot = result['snapshot']
    assert snapshot['team']['latest_artifact_id'] == first['artifact_id']
    assert snapshot['latest_artifact']['artifact_id'] == first['artifact_id']
    after_first = AgentTeamStore.resolve_published_artifact_by_id(
        'task_team_rollback', first['artifact_id'],
    )
    after_second = AgentTeamStore.resolve_published_artifact_by_id(
        'task_team_rollback', second['artifact_id'],
    )
    for before, after in (
        (before_first, after_first),
        (before_second, after_second),
    ):
        assert {
            key: after[key]
            for key in ('status', 'uri', 'sha256', 'approval_id', 'metadata')
        } == {
            key: before[key]
            for key in ('status', 'uri', 'sha256', 'approval_id', 'metadata')
        }
    assert AgentTeamStore.list_events(
        'team_rollback_v2', after_cursor=0, limit=100,
    )['events'][-1]['event_type'] == 'artifact_rollback'

    replay = AgentTeamStore.rollback_artifact(
        'team_rollback_v2',
        first['artifact_id'],
        expected_version=2,
        idempotency_key='rollback-to-v1',
        authority='vue',
        actor='vue-user',
        reason='v2 展示异常',
    )
    assert replay['replayed'] is True
    assert replay['snapshot']['team']['state_version'] == 3
