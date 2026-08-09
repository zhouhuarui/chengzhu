"""Pinned AgentTeams v1.2.0 controller and dispatch contract tests."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.integrations.agentteams.client import (
    AgentTeamsClientError,
    AgentTeamsControllerClient,
)
from app.integrations.agentteams.dispatcher import AgentTeamsDispatcher
from app.team import DEFAULT_AGENT_ROLE_IDS, TASK_CONTRACT_FIELDS


def _controller_payload(path: str):
    if path == '/api/v1/version':
        return {'version': 'v1.2.0'}
    if path == '/api/v1/managers/default':
        return {
            'phase': 'Running',
            'roomID': '!manager:agentteams.local',
            'matrixUserID': '@manager:agentteams.local',
        }
    if path == '/api/v1/teams/chengzhu-research-team':
        return {
            # This is the exact healthy phase written by the v1.2.0 Team
            # controller, not the Worker/Manager ``Running`` phase.
            'phase': 'Active',
            'leaderReady': True,
            'workerMembers': [
                {
                    'name': name,
                    'role': 'team_leader' if name == 'research-lead' else 'worker',
                }
                for name in DEFAULT_AGENT_ROLE_IDS
            ],
        }
    raise AssertionError(path)


def test_v120_active_team_is_ready_for_dispatch(monkeypatch):
    client = AgentTeamsControllerClient(token='controller-token')
    monkeypatch.setattr(client, 'get', _controller_payload)

    state = client.preflight()

    assert state['team']['phase'] == 'Active'
    assert len(state['team']['workerMembers']) == 8


def test_controller_preflight_still_rejects_incomplete_team(monkeypatch):
    client = AgentTeamsControllerClient(token='controller-token')

    def incomplete(path: str):
        payload = _controller_payload(path)
        if path.endswith('/chengzhu-research-team'):
            payload = {**payload, 'workerMembers': payload['workerMembers'][:-1]}
        return payload

    monkeypatch.setattr(client, 'get', incomplete)
    with pytest.raises(AgentTeamsClientError) as caught:
        client.preflight()
    assert caught.value.code == 'agentteams_team_must_have_eight_roles'


def test_controller_preflight_rejects_wrong_roster_or_leader(monkeypatch):
    client = AgentTeamsControllerClient(token='controller-token')

    def wrong_roster(path: str):
        payload = _controller_payload(path)
        if path.endswith('/chengzhu-research-team'):
            members = list(payload['workerMembers'])
            members[-1] = {'name': 'unknown-worker', 'role': 'worker'}
            return {**payload, 'workerMembers': members}
        return payload

    monkeypatch.setattr(client, 'get', wrong_roster)
    with pytest.raises(AgentTeamsClientError) as caught:
        client.preflight()
    assert caught.value.code == 'agentteams_team_roster_mismatch'

    def wrong_leader(path: str):
        payload = _controller_payload(path)
        if path.endswith('/chengzhu-research-team'):
            members = [dict(member) for member in payload['workerMembers']]
            members[0]['role'] = 'worker'
            members[1]['role'] = 'team_leader'
            return {**payload, 'workerMembers': members}
        return payload

    monkeypatch.setattr(client, 'get', wrong_leader)
    with pytest.raises(AgentTeamsClientError) as caught:
        client.preflight()
    assert caught.value.code == 'agentteams_team_leader_mismatch'


def test_project_request_contains_only_bounded_contract_metadata(monkeypatch):
    monkeypatch.setattr(Config, 'AGENTTEAMS_MAX_ACTIVE_WORKERS', 3)
    body = AgentTeamsDispatcher._safe_project_request(
        'task-safe',
        'run-safe',
        {
            'analysis_mode': 'evidence_debate',
            'symbols': [{'code': '300750', 'name': '宁德时代'}],
            'time_window': {'start': '2026-01-01', 'end': '2026-06-30'},
            # Arbitrary TaskCard fields must not be mirrored into Matrix.
            'private_document': 'never-copy-this',
            'api_key': 'never-copy-this-either',
        },
        trace_id='a' * 32,
    )
    contract = json.loads(body.split('\n\n')[1])

    assert contract['project_id'] == 'chengzhu-run-safe'
    assert contract['max_active_workers'] == 3
    assert len(contract['workflow']) == 9
    task_contract = contract['task_contract']
    assert set(task_contract) == set(TASK_CONTRACT_FIELDS)
    allocations = task_contract['budget']['task_allocations_cny']
    assert sum(allocations.values()) == pytest.approx(
        Config.LLM_COST_BUDGET_CNY
    )
    assert task_contract['trace_id'] == 'a' * 32
    assert 'private_document' not in body
    assert 'api_key' not in body
    assert 'never-copy-this' not in body


def test_direct_request_uses_seven_node_teamharness_dag():
    body = AgentTeamsDispatcher._safe_project_request(
        'task-direct',
        'run-direct',
        {'analysis_mode': 'direct', 'symbols': []},
    )
    contract = json.loads(body.split('\n\n')[1])
    by_key = {item['key']: item for item in contract['workflow']}

    assert len(by_key) == 7
    assert 'quality-analysis' not in by_key
    assert 'growth-analysis' not in by_key
    assert by_key['evidence-judgement']['depends_on'] == ['evidence-freeze']
    assert contract['mode_policy'] == 'use_dag_plan_direct_without_analyst_nodes'
    allocations = contract['task_contract']['budget']['task_allocations_cny']
    assert allocations['quality-analysis'] == 0
    assert allocations['growth-analysis'] == 0
    assert sum(allocations.values()) == pytest.approx(
        Config.LLM_COST_BUDGET_CNY
    )
