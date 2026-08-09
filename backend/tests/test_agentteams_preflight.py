"""Application-level competition preflight contract tests."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.integrations.agentteams.preflight import (
    CompetitionPreflightError,
    EXPECTED_MODELS,
    EXPECTED_WORKER_IMAGE,
    _gateway_routes_deny_anonymous,
    _validate_workers,
)
from app.team import DEFAULT_AGENT_ROLE_IDS


class _Workers:
    def __init__(self, mutate=None):
        self.mutate = mutate

    def worker(self, role):
        payload = {
            'name': role,
            'model': EXPECTED_MODELS[role],
            'runtime': 'copaw',
            'image': EXPECTED_WORKER_IMAGE,
            'state': 'Running' if role == 'research-lead' else 'Sleeping',
        }
        return self.mutate(role, payload) if self.mutate else payload


def test_worker_preflight_requires_exact_pinned_profiles():
    _validate_workers(_Workers())

    with pytest.raises(CompetitionPreflightError, match='worker_model_mismatch'):
        _validate_workers(_Workers(
            lambda role, payload: (
                {**payload, 'model': 'wrong'}
                if role == 'evidence-judge' else payload
            )
        ))
    with pytest.raises(CompetitionPreflightError, match='worker_lifecycle_mismatch'):
        _validate_workers(_Workers(
            lambda role, payload: (
                {**payload, 'state': 'Running'}
                if role == 'growth-analyst' else payload
            )
        ))


def test_mcp_route_preflight_requires_anonymous_denial(monkeypatch):
    calls = []

    class _Response:
        status_code = 401

    def denied(url, **_kwargs):
        calls.append(url)
        return _Response()

    monkeypatch.setattr(
        'app.integrations.agentteams.preflight.httpx.get', denied,
    )
    _gateway_routes_deny_anonymous()
    assert len(calls) == len(DEFAULT_AGENT_ROLE_IDS)
    assert all('/mcp-servers/mcp-chengzhu-' in url for url in calls)

    class _OpenResponse:
        status_code = 200

    monkeypatch.setattr(
        'app.integrations.agentteams.preflight.httpx.get',
        lambda *_args, **_kwargs: _OpenResponse(),
    )
    with pytest.raises(
        CompetitionPreflightError,
        match='mcp_gateway_anonymous_not_denied',
    ):
        _gateway_routes_deny_anonymous()
