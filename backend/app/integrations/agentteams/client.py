"""Strict clients for the AgentTeams controller and its Matrix homeserver."""

from __future__ import annotations

import html
import os
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import httpx

from ...config import Config
from ...team import DEFAULT_AGENT_ROLE_IDS


class AgentTeamsClientError(RuntimeError):
    """Operational error whose message never includes response bodies."""

    def __init__(self, code: str, *, status_code: Optional[int] = None):
        self.code = code
        self.status_code = status_code
        suffix = f' status={status_code}' if status_code is not None else ''
        super().__init__(f'{code}{suffix}')


def _read_token() -> str:
    if Config.AGENTTEAMS_AUTH_TOKEN:
        return str(Config.AGENTTEAMS_AUTH_TOKEN).strip()
    path = str(Config.AGENTTEAMS_AUTH_TOKEN_FILE or '').strip()
    if not path:
        return ''
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read(16 * 1024).strip()
    except OSError as error:
        raise AgentTeamsClientError('agentteams_auth_token_unreadable') from error


class AgentTeamsControllerClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = str(base_url or Config.AGENTTEAMS_CONTROLLER_URL).rstrip('/')
        self.token = _read_token() if token is None else str(token)
        self.timeout = float(timeout or Config.AGENTTEAMS_HTTP_TIMEOUT_SECONDS)

    def _headers(self) -> Dict[str, str]:
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def get(self, path: str) -> Dict[str, Any]:
        try:
            response = httpx.get(
                f'{self.base_url}{path}',
                headers=self._headers(),
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as error:
            raise AgentTeamsClientError('agentteams_controller_timeout') from error
        except httpx.HTTPError as error:
            raise AgentTeamsClientError('agentteams_controller_unavailable') from error
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentTeamsClientError(
                'agentteams_controller_rejected', status_code=response.status_code
            )
        try:
            value = response.json()
        except ValueError as error:
            raise AgentTeamsClientError('agentteams_controller_invalid_json') from error
        if not isinstance(value, dict):
            raise AgentTeamsClientError('agentteams_controller_invalid_payload')
        return value

    def manager(self, name: Optional[str] = None) -> Dict[str, Any]:
        manager_name = urllib.parse.quote(
            str(name or Config.AGENTTEAMS_MANAGER_NAME), safe=''
        )
        return self.get(f'/api/v1/managers/{manager_name}')

    def team(self, name: Optional[str] = None) -> Dict[str, Any]:
        team_name = urllib.parse.quote(str(name or Config.AGENTTEAMS_TEAM_NAME), safe='')
        return self.get(f'/api/v1/teams/{team_name}')

    def worker(self, name: str) -> Dict[str, Any]:
        return self.get(f'/api/v1/workers/{urllib.parse.quote(str(name), safe="")}')

    def preflight(self) -> Dict[str, Any]:
        version = self.get('/api/v1/version')
        reported = str(
            version.get('version')
            or (version.get('data') or {}).get('version')
            or ''
        )
        if reported and reported.lstrip('v') != Config.AGENTTEAMS_VERSION.lstrip('v'):
            raise AgentTeamsClientError('agentteams_version_mismatch')

        manager = self.manager()
        team = self.team()
        if str(manager.get('phase') or '').lower() != 'running':
            raise AgentTeamsClientError('agentteams_manager_not_ready')
        if not manager.get('roomID') or not manager.get('matrixUserID'):
            raise AgentTeamsClientError('agentteams_manager_matrix_not_ready')
        # AgentTeams v1.2.0's Team controller reports a healthy reconciled
        # Team as ``Active``. Keep the older aliases for defensive API
        # compatibility, but never require a non-upstream phase value.
        if str(team.get('phase') or '').lower() not in {'active', 'running', 'ready'}:
            raise AgentTeamsClientError('agentteams_team_not_ready')
        if not bool(team.get('leaderReady')):
            raise AgentTeamsClientError('agentteams_leader_not_ready')
        members = (
            team.get('workerMembers')
            or (team.get('spec') or {}).get('workerMembers')
            or []
        )
        if len(members) != 8:
            raise AgentTeamsClientError('agentteams_team_must_have_eight_roles')
        names = []
        leaders = []
        for member in members:
            if isinstance(member, str):
                name = member
                role = ''
            elif isinstance(member, dict):
                metadata = member.get('metadata') or {}
                name = str(
                    member.get('name')
                    or member.get('workerName')
                    or metadata.get('name')
                    or ''
                )
                role = str(member.get('role') or '').lower()
            else:
                name = ''
                role = ''
            names.append(name)
            if role == 'team_leader':
                leaders.append(name)
        if len(set(names)) != len(names) or set(names) != set(DEFAULT_AGENT_ROLE_IDS):
            raise AgentTeamsClientError('agentteams_team_roster_mismatch')
        # The v1beta1 Team response preserves the declared member role.  A
        # ready flag alone is insufficient because a different Worker could
        # otherwise become the privileged TeamHarness leader.
        if leaders != ['research-lead']:
            raise AgentTeamsClientError('agentteams_team_leader_mismatch')
        return {'version': version, 'manager': manager, 'team': team}


class MatrixClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = str(base_url or Config.AGENTTEAMS_MATRIX_URL).rstrip('/')
        self.access_token = str(
            access_token if access_token is not None
            else Config.AGENTTEAMS_MATRIX_ACCESS_TOKEN
        ).strip()
        self.timeout = float(timeout or Config.AGENTTEAMS_HTTP_TIMEOUT_SECONDS)

    def _login(self) -> str:
        if self.access_token:
            return self.access_token
        if not Config.AGENTTEAMS_ADMIN_USER or not Config.AGENTTEAMS_ADMIN_PASSWORD:
            raise AgentTeamsClientError('agentteams_matrix_credentials_missing')
        payload = {
            'type': 'm.login.password',
            'identifier': {
                'type': 'm.id.user',
                'user': Config.AGENTTEAMS_ADMIN_USER,
            },
            'password': Config.AGENTTEAMS_ADMIN_PASSWORD,
        }
        try:
            response = httpx.post(
                f'{self.base_url}/_matrix/client/v3/login',
                json=payload,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as error:
            raise AgentTeamsClientError('agentteams_matrix_timeout') from error
        except httpx.HTTPError as error:
            raise AgentTeamsClientError('agentteams_matrix_unavailable') from error
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentTeamsClientError(
                'agentteams_matrix_login_rejected', status_code=response.status_code
            )
        try:
            token = str((response.json() or {}).get('access_token') or '')
        except ValueError as error:
            raise AgentTeamsClientError('agentteams_matrix_invalid_json') from error
        if not token:
            raise AgentTeamsClientError('agentteams_matrix_token_missing')
        self.access_token = token
        return token

    def send_message(
        self,
        room_id: str,
        body: str,
        *,
        mention_user_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
    ) -> str:
        token = self._login()
        encoded_room = urllib.parse.quote(str(room_id), safe='')
        txn = urllib.parse.quote(
            str(transaction_id or f'chengzhu-{uuid.uuid4().hex}'), safe=''
        )
        content: Dict[str, Any] = {
            'msgtype': 'm.text',
            'body': str(body),
            'format': 'org.matrix.custom.html',
            'formatted_body': '<pre>' + html.escape(str(body)) + '</pre>',
        }
        if mention_user_id:
            content['m.mentions'] = {'user_ids': [str(mention_user_id)]}
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        try:
            response = httpx.put(
                f'{self.base_url}/_matrix/client/v3/rooms/{encoded_room}'
                f'/send/m.room.message/{txn}',
                headers=headers,
                json=content,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as error:
            raise AgentTeamsClientError('agentteams_matrix_timeout') from error
        except httpx.HTTPError as error:
            raise AgentTeamsClientError('agentteams_matrix_unavailable') from error
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentTeamsClientError(
                'agentteams_matrix_send_rejected', status_code=response.status_code
            )
        try:
            event_id = str((response.json() or {}).get('event_id') or '')
        except ValueError as error:
            raise AgentTeamsClientError('agentteams_matrix_invalid_json') from error
        if not event_id:
            raise AgentTeamsClientError('agentteams_matrix_event_missing')
        return event_id
