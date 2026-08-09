"""AgentTeams MCP authentication, role isolation and idempotent dispatch tests."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.mcp.server import create_mcp_app
from app.models.research_task import ResearchTask
from app.models.task_card import SymbolRef, TaskCard
from app.team import AgentTeamStore
from app.utils import db as dbmod


@pytest.fixture
def mcp_runtime(tmp_path, monkeypatch):
    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'mcp.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    monkeypatch.setattr(Config, 'AGENTTEAMS_MCP_GATEWAY_TOKEN', 'gateway-secret')
    dbmod.init_db()

    task = ResearchTask(task_id='task_mcp', requirement='测试 AgentTeams MCP')
    card = TaskCard(
        deliverable='summary',
        symbols=[SymbolRef(code='600000', name='浦发银行')],
        time_window={'start': '2025-01-01', 'end': '2025-06-30'},
        analysis_mode='evidence_debate',
        execution_mode='agentteams',
    )
    task.set_task_card(card)
    run_id = task.create_run(card.to_dict())
    assert run_id
    AgentTeamStore.create_team_run(
        f'team-{run_id}',
        run_id,
        task.task_id,
        idempotency_key=f'create-{run_id}',
        config={'execution_mode': 'agentteams'},
    )
    app = create_mcp_app(Config)
    app.config['TESTING'] = False
    yield app.test_client(), run_id

    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None


def _rpc(client, role, method, params=None, *, token='gateway-secret', request_id=1):
    return client.post(
        '/mcp',
        headers={
            'Authorization': f'Bearer {token}',
            'X-AgentTeams-Worker': role,
        },
        json={
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            **({'params': params} if params is not None else {}),
        },
    )


def _structured(response):
    payload = response.get_json()
    return payload['result']['structuredContent']


def test_mcp_rejects_missing_gateway_auth_and_reports_pinned_protocol(mcp_runtime):
    client, _run_id = mcp_runtime
    unauthorized = _rpc(
        client, 'research-lead', 'initialize', token='wrong-secret'
    )
    assert unauthorized.status_code == 401

    initialized = _rpc(client, 'research-lead', 'initialize')
    assert initialized.status_code == 200
    result = initialized.get_json()['result']
    assert result['protocolVersion'] == '2024-11-05'
    assert result['serverInfo']['name'] == 'chengzhu'


def test_higress_role_path_is_authoritative_and_header_cannot_override(mcp_runtime):
    client, _run_id = mcp_runtime
    routed = client.post(
        '/mcp/disclosure-researcher',
        headers={'Authorization': 'Bearer gateway-secret'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
    )
    assert routed.status_code == 200
    names = {item['name'] for item in routed.get_json()['result']['tools']}
    assert names == {'collect_evidence', 'bailian_visual_proxy'}

    mismatch = client.post(
        '/mcp/disclosure-researcher',
        headers={
            'Authorization': 'Bearer gateway-secret',
            'X-AgentTeams-Worker': 'research-lead',
        },
        json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    )
    assert mismatch.status_code == 401


def test_mcp_tool_lists_are_role_scoped_and_never_expose_publish(mcp_runtime):
    client, _run_id = mcp_runtime
    disclosure = _rpc(client, 'disclosure-researcher', 'tools/list').get_json()
    disclosure_names = {
        item['name'] for item in disclosure['result']['tools']
    }
    assert disclosure_names == {'collect_evidence', 'bailian_visual_proxy'}

    writer = _rpc(client, 'report-writer', 'tools/list').get_json()
    writer_names = {item['name'] for item in writer['result']['tools']}
    assert writer_names == {'get_frozen_context', 'store_report_draft'}
    assert 'publish_approved_report' not in writer_names
    assert 'request_publish_approval' not in writer_names


def test_mcp_role_denial_and_start_are_durable_idempotent(mcp_runtime):
    client, run_id = mcp_runtime
    denied = _rpc(client, 'report-writer', 'tools/call', {
        'name': 'collect_evidence',
        'arguments': {
            'task_id': 'task_mcp',
            'run_id': run_id,
            'source_group': 'disclosure',
            'expected_version': 0,
            'idempotency_key': 'forbidden-call',
        },
    })
    assert _structured(denied)['error'] == 'role_permission_denied'

    arguments = {
        'task_id': 'task_mcp',
        'run_id': run_id,
        'expected_version': 0,
        'idempotency_key': 'start-team-once',
    }
    first = _rpc(client, 'research-lead', 'tools/call', {
        'name': 'start_team_run', 'arguments': arguments,
    })
    assert _structured(first)['ok'] is True
    assert _structured(first)['team']['team']['current_stage'] == 'collecting'

    # The exact network retry carries the old expected version but replays the
    # first durable result instead of dispatching or billing a second time.
    replay = _rpc(client, 'research-lead', 'tools/call', {
        'name': 'start_team_run', 'arguments': arguments,
    })
    assert _structured(replay)['ok'] is True
    snapshot = AgentTeamStore.get_team(f'team-{run_id}')
    start_events = [
        item for item in AgentTeamStore.list_events(
            f'team-{run_id}', after_cursor=0, limit=100
        )['events']
        if item['event_type'] == 'team_task_status_changed'
        and item['payload'].get('to_status') == 'completed'
    ]
    assert len(start_events) == 1


def test_mcp_errors_do_not_echo_tokens_or_hidden_payloads(mcp_runtime):
    client, run_id = mcp_runtime
    response = _rpc(client, 'quality-analyst', 'tools/call', {
        'name': 'submit_claims',
        'arguments': {
            'task_id': 'task_mcp',
            'run_id': run_id,
            'claims': [{'assertion': 'secret prompt should not escape'}],
            'round_number': 1,
            'finalize': True,
            'expected_version': 999,
            'idempotency_key': 'secret-api_key=value',
        },
    })
    serialized = json.dumps(response.get_json(), ensure_ascii=False)
    assert 'gateway-secret' not in serialized
    assert 'secret prompt should not escape' not in serialized
    assert response.get_json()['result']['isError'] is True
    assert _structured(response)['error'] == 'invalid_tool_arguments'
