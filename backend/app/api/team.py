"""Read-only Agent Team views plus Vue-authoritative human actions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify, request

from ..team import (
    APPROVAL_AUTHORITY,
    AgentTeamStore,
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    TeamNotFoundError,
)
from ..team.publication import coordinate_approval, coordinate_rollback
from ..config import Config
from ..utils import db as dbutil


team_bp = Blueprint('team', __name__)
task_team_bp = Blueprint('task_team', __name__)


def _error(message: str, status: int, *, code: str, **details: Any):
    payload: Dict[str, Any] = {
        'success': False,
        'error': message,
        'code': code,
    }
    payload.update(details)
    return jsonify(payload), status


def _expected_version(body: Dict[str, Any]) -> int:
    value = body.get('expected_version')
    if isinstance(value, bool):
        raise TeamInvariantError('expected_version 必须为非负整数')
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise TeamInvariantError('expected_version 必须为非负整数') from None
    if parsed < 0:
        raise TeamInvariantError('expected_version 必须为非负整数')
    return parsed


def _idempotency_key(body: Dict[str, Any]) -> str:
    value = request.headers.get('Idempotency-Key') or body.get('idempotency_key')
    if not isinstance(value, str) or not value.strip():
        raise TeamInvariantError('Idempotency-Key 或 idempotency_key 必填')
    return value.strip()


def _authority(body: Dict[str, Any]) -> str:
    authority = str(
        request.headers.get('X-Approval-Source')
        or body.get('source')
        or body.get('authority')
        or ''
    )
    if authority != APPROVAL_AUTHORITY:
        raise PermissionError('只有 Vue 人工入口具有操作权')
    return authority


def _actor(body: Dict[str, Any]) -> str:
    return str(
        request.headers.get('X-Actor-Id')
        or body.get('actor')
        or 'vue-user'
    )


def _stable_idempotency_key(action: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    return f'vue-auto:{action}:{digest}'


def _header_or_stable_key(action: str, *parts: Any) -> str:
    header = request.headers.get('Idempotency-Key')
    if isinstance(header, str) and header.strip():
        return header.strip()
    return _stable_idempotency_key(action, *parts)


def _domain_error(error: Exception) -> Tuple[Any, int]:
    if isinstance(error, TeamNotFoundError):
        return _error(str(error), 404, code=error.code)
    if isinstance(error, TeamIdempotencyError):
        return _error(str(error), 409, code=error.code)
    if isinstance(error, TeamConflictError):
        details = {}
        if error.current_version is not None:
            details['current_version'] = error.current_version
        return _error(str(error), 409, code=error.code, **details)
    if isinstance(error, TeamInvariantError):
        if str(error) == '历史 run 仅支持只读回放':
            return _error(str(error), 409, code='replay_read_only')
        return _error(str(error), 400, code=error.code)
    return _error('Agent Team 操作失败', 500, code='team_internal_error')


def _iso_seconds(start: Any, end: Any) -> float:
    def parse(value: Any):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            return None

    left = parse(start)
    right = parse(end)
    if not left or not right:
        return 0.0
    return max(0.0, (right - left).total_seconds())


def _with_live_metrics(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Add bounded operational metrics without exposing prompts or secrets."""

    result = dict(snapshot)
    team = result.get('team') or {}
    run_id = str(team.get('run_id') or '')
    team_id = str(team.get('team_id') or '')
    if not run_id or not team_id:
        return result
    now = datetime.now(timezone.utc).isoformat()
    tasks = list(result.get('tasks') or [])
    terminal = [item for item in tasks if item.get('status') in {'completed', 'skipped', 'failed'}]
    successful = [item for item in terminal if item.get('status') in {'completed', 'skipped'}]
    stage_durations = {
        str(item.get('task_key')): round(
            _iso_seconds(item.get('started_at'), item.get('finished_at') or now), 3
        )
        for item in tasks if item.get('started_at')
    }
    degradation_reasons = list(team.get('degradation_reasons') or [])
    for item in tasks:
        output = item.get('output') or {}
        if output.get('degraded'):
            degradation_reasons.append(f'{item.get("task_key")}:degraded')
    budget = dbutil.llm_budget_totals(run_id)
    with dbutil.db_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(total_tokens), 0) AS tokens,
                      COALESCE(SUM(CASE WHEN cost_cny > 0 THEN cost_cny ELSE 0 END), 0) AS cost,
                      COALESCE(SUM(retry_count), 0) AS retries,
                      COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
                      COALESCE(AVG(latency_ms), 0) AS avg_latency
               FROM llm_call_log WHERE run_id = ?""",
            (run_id,),
        )
        llm = dict(cur.fetchone())
        cur.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failures,
                      COALESCE(SUM(CASE WHEN degraded = 1 THEN 1 ELSE 0 END), 0) AS degraded,
                      COALESCE(AVG(latency_ms), 0) AS avg_latency
               FROM tool_call_log WHERE run_id = ?""",
            (run_id,),
        )
        tools = dict(cur.fetchone())
        cur.execute(
            'SELECT claim_count, audit_failure_count FROM debate_run WHERE run_id = ?',
            (run_id,),
        )
        debate = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS retries FROM team_event "
            "WHERE team_id = ? AND event_type IN "
            "('collector_retry','worker_retry','mcp_tool_failed')",
            (team_id,),
        )
        team_retries = int(cur.fetchone()['retries'] or 0)
        cur.execute(
            """SELECT h.created_at AS decided_at, a.created_at AS requested_at
               FROM human_approval h
               JOIN artifact_manifest a ON a.artifact_id = h.artifact_id
               WHERE h.team_id = ? ORDER BY h.created_at DESC LIMIT 1""",
            (team_id,),
        )
        approval = cur.fetchone()
        cur.execute(
            "SELECT payload_json FROM team_event "
            "WHERE team_id = ? AND event_type = 'matrix_dispatch_sent' "
            'ORDER BY cursor DESC LIMIT 1',
            (team_id,),
        )
        dispatch_row = cur.fetchone()
    try:
        dispatch = json.loads(dispatch_row['payload_json']) if dispatch_row else {}
    except (TypeError, ValueError):
        dispatch = {}
    debate_claims = int(debate['claim_count'] or 0) if debate else 0
    audit_failures = int(debate['audit_failure_count'] or 0) if debate else 0
    tool_calls = int(tools.get('calls') or 0)
    tool_failures = int(tools.get('failures') or 0)
    committed = float(budget.get('committed_cny') or 0)
    metrics = {
        'success_rate': round(len(successful) / len(terminal), 4) if terminal else None,
        'completed_tasks': len(successful),
        'total_tasks': len(tasks),
        'duration_seconds': round(
            _iso_seconds(team.get('created_at'), team.get('finished_at') or now), 3
        ),
        'stage_durations_seconds': stage_durations,
        'llm_calls': int(llm.get('calls') or 0),
        'llm_tokens': int(llm.get('tokens') or 0),
        'llm_cost_cny': round(float(llm.get('cost') or 0), 6),
        'llm_failure_rate': (
            round(int(llm.get('failures') or 0) / int(llm.get('calls') or 1), 4)
            if int(llm.get('calls') or 0) else 0.0
        ),
        'tool_calls': tool_calls,
        'tool_failure_rate': round(tool_failures / tool_calls, 4) if tool_calls else 0.0,
        'tool_degraded_calls': int(tools.get('degraded') or 0),
        'audit_rejection_rate': (
            round(audit_failures / debate_claims, 4) if debate_claims else 0.0
        ),
        'retry_count': int(llm.get('retries') or 0) + team_retries,
        'approval_duration_seconds': (
            round(_iso_seconds(approval['requested_at'], approval['decided_at']), 3)
            if approval else None
        ),
        'budget': {
            'spent_cny': committed,
            'settled_cny': float(budget.get('settled_cny') or 0),
            'reserved_cny': float(budget.get('reserved_cny') or 0),
            'limit_cny': float(Config.LLM_COST_BUDGET_CNY),
            'remaining_cny': max(0.0, float(Config.LLM_COST_BUDGET_CNY) - committed),
            # AgentTeams v1.2.0 does not expose cumulative usage keyed by the
            # Chengzhu run_id.  Never present this backend ledger as combined
            # Worker + MCP spend until a run-aware model gateway is in place.
            'scope': 'chengzhu_observed_calls',
            'includes_agentteams_worker_usage': False,
        },
        'degraded': bool(degradation_reasons),
        'degradation_reasons': list(dict.fromkeys(degradation_reasons)),
    }
    result['metrics'] = metrics
    result['element_url'] = team.get('element_url') or dispatch.get('element_url')
    result['matrix_room_id'] = team.get('matrix_room_id') or dispatch.get('matrix_room_id')
    result['trace_id'] = team.get('trace_id') or dispatch.get('trace_id')
    result['span_id'] = team.get('span_id') or dispatch.get('span_id')
    return result


@team_bp.route('/<team_id>', methods=['GET'])
def get_team(team_id: str):
    try:
        return jsonify({
            'success': True,
            'data': _with_live_metrics(AgentTeamStore.get_team(team_id)),
        })
    except (TeamNotFoundError, TeamInvariantError) as error:
        return _domain_error(error)


@team_bp.route('/<team_id>/events', methods=['GET'])
def get_team_events(team_id: str):
    try:
        cursor = request.args.get('cursor', request.args.get('after_cursor', '0'))
        limit = request.args.get('limit', '100')
        data = AgentTeamStore.list_events(
            team_id,
            after_cursor=cursor,
            limit=limit,
        )
        return jsonify({'success': True, 'data': data})
    except (TeamNotFoundError, TeamInvariantError) as error:
        return _domain_error(error)


@team_bp.route('/<team_id>/approval', methods=['POST'])
def decide_team_approval(team_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error('JSON 对象必填', 400, code='invalid_request')
    try:
        authority = _authority(body)
        artifact_id = body.get('artifact_id')
        if not artifact_id:
            raise TeamInvariantError('artifact_id 必填')
        decision = str(body.get('decision') or '').lower()
        decision = {
            'approve': 'approved',
            'approved': 'approved',
            'reject': 'rejected',
            'rejected': 'rejected',
        }.get(decision, decision)
        data = coordinate_approval(
            team_id,
            str(artifact_id),
            decision,
            expected_version=_expected_version(body),
            idempotency_key=_idempotency_key(body),
            source=authority,
            actor=_actor(body),
            reason=body.get('reason'),
        )
        return jsonify({'success': True, 'data': data})
    except PermissionError as error:
        return _error(str(error), 403, code='approval_authority_required')
    except (
        TeamNotFoundError,
        TeamConflictError,
        TeamIdempotencyError,
        TeamInvariantError,
    ) as error:
        return _domain_error(error)
    except Exception:
        return _error(
            '审批决定可能已记录，报告发布未完成；使用相同请求重试即可',
            500,
            code='publication_retry_required',
        )


@team_bp.route('/<team_id>/rollback', methods=['POST'])
def rollback_team_artifact(team_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error('JSON 对象必填', 400, code='invalid_request')
    try:
        authority = _authority(body)
        target_artifact_id = body.get('target_artifact_id') or body.get('artifact_id')
        if not target_artifact_id:
            raise TeamInvariantError('target_artifact_id 必填')
        reason = body.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise TeamInvariantError('reason 必填')
        data = coordinate_rollback(
            team_id,
            str(target_artifact_id),
            expected_version=_expected_version(body),
            idempotency_key=_idempotency_key(body),
            source=authority,
            actor=_actor(body),
            reason=reason.strip(),
        )
        return jsonify({'success': True, 'data': data})
    except PermissionError as error:
        return _error(str(error), 403, code='approval_authority_required')
    except (
        TeamNotFoundError,
        TeamConflictError,
        TeamIdempotencyError,
        TeamInvariantError,
    ) as error:
        return _domain_error(error)
    except Exception:
        return _error(
            '回滚指针可能已记录，latest 镜像未完成；使用相同请求重试即可',
            500,
            code='rollback_retry_required',
        )


# ---------------------------------------------------------------------------
# Primary Vue contract: task/run scoped and free of internal Team/artifact IDs.


@task_team_bp.route('/<task_id>/team', methods=['GET'])
def get_task_team(task_id: str):
    try:
        data = AgentTeamStore.get_team_for_task(
            task_id,
            run_id=request.args.get('run_id') or None,
        )
        return jsonify({'success': True, 'data': _with_live_metrics(data)})
    except (TeamNotFoundError, TeamInvariantError) as error:
        return _domain_error(error)


@task_team_bp.route('/<task_id>/team/events', methods=['GET'])
def get_task_team_events(task_id: str):
    try:
        snapshot = AgentTeamStore.get_team_for_task(
            task_id,
            run_id=request.args.get('run_id') or None,
        )
        data = AgentTeamStore.list_events(
            snapshot['team']['team_id'],
            after_cursor=request.args.get(
                'from_cursor',
                request.args.get('cursor', '0'),
            ),
            limit=request.args.get('limit', '100'),
        )
        return jsonify({'success': True, 'data': data})
    except (TeamNotFoundError, TeamInvariantError) as error:
        return _domain_error(error)


@task_team_bp.route('/<task_id>/runs/<run_id>/approval', methods=['POST'])
def decide_task_run_approval(task_id: str, run_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error('JSON 对象必填', 400, code='invalid_request')
    try:
        snapshot = AgentTeamStore.get_team_for_task(task_id, run_id=run_id)
        if snapshot.get('source') == 'replay':
            raise TeamInvariantError('历史 run 仅支持只读回放')
        artifact = AgentTeamStore.resolve_approval_artifact(
            snapshot['team']['team_id']
        )
        decision_input = str(body.get('decision') or '').lower()
        decision = {
            'approve': 'approved',
            'reject': 'rejected',
        }.get(decision_input)
        if decision is None:
            raise TeamInvariantError('decision 必须为 approve 或 reject')
        expected = _expected_version(body)
        reason = body.get('reason')
        if reason is not None and not isinstance(reason, str):
            raise TeamInvariantError('reason 必须为字符串')
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        idem = _header_or_stable_key(
            'approval',
            task_id,
            run_id,
            artifact['artifact_id'],
            decision,
            expected,
            reason,
        )
        data = coordinate_approval(
            snapshot['team']['team_id'],
            artifact['artifact_id'],
            decision,
            expected_version=expected,
            idempotency_key=idem,
            source=APPROVAL_AUTHORITY,
            actor=_actor({}),
            reason=reason,
        )
        return jsonify({'success': True, 'data': data})
    except (
        TeamNotFoundError,
        TeamConflictError,
        TeamIdempotencyError,
        TeamInvariantError,
    ) as error:
        return _domain_error(error)
    except Exception:
        return _error(
            '审批决定可能已记录，报告发布未完成；使用相同请求重试即可',
            500,
            code='publication_retry_required',
        )


@task_team_bp.route('/<task_id>/runs/<run_id>/rollback', methods=['POST'])
def rollback_task_run(task_id: str, run_id: str):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error('JSON 对象必填', 400, code='invalid_request')
    try:
        snapshot = AgentTeamStore.get_team_for_task(task_id, run_id=run_id)
        if snapshot.get('source') == 'replay':
            raise TeamInvariantError('历史 run 仅支持只读回放')
        target_run_id = body.get('target_run_id')
        if not isinstance(target_run_id, str) or not target_run_id.strip():
            raise TeamInvariantError('target_run_id 必填')
        target_run_id = target_run_id.strip()
        target = AgentTeamStore.resolve_published_artifact(
            task_id,
            str(target_run_id),
        )
        expected = _expected_version(body)
        reason = body.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise TeamInvariantError('reason 必填')
        reason = reason.strip()
        idem = _header_or_stable_key(
            'rollback',
            task_id,
            run_id,
            target_run_id,
            target['artifact_id'],
            expected,
            reason,
        )
        data = coordinate_rollback(
            snapshot['team']['team_id'],
            target['artifact_id'],
            expected_version=expected,
            idempotency_key=idem,
            source=APPROVAL_AUTHORITY,
            actor=_actor({}),
            reason=reason,
        )
        return jsonify({'success': True, 'data': data})
    except (
        TeamNotFoundError,
        TeamConflictError,
        TeamIdempotencyError,
        TeamInvariantError,
    ) as error:
        return _domain_error(error)
    except Exception:
        return _error(
            '回滚指针可能已记录，latest 镜像未完成；使用相同请求重试即可',
            500,
            code='rollback_retry_required',
        )
