"""Transactional SQLite store for Agent Team runs and human gates.

All externally retried mutations use an idempotency key.  Mutable aggregate
state is updated with an explicit ``state_version`` compare-and-swap check.
"""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from typing import Any, Dict, List, Optional

from ..utils import db as dbutil
from ..config import Config
from .contracts import (
    APPROVAL_AUTHORITY,
    DEFAULT_AGENT_ROLE_IDS,
    DEFAULT_TEAM_DAG,
    HANDOFF_STATUSES,
    MAX_HUMAN_REJECTIONS,
    TASK_CONTRACT_FIELDS,
    TASK_STATUSES,
    TEAM_TASK_MAX_ATTEMPTS,
    TEAM_STATUSES,
    build_task_contract,
    team_task_budget_allocations,
)
from .errors import (
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    TeamNotFoundError,
)
from .redaction import redact_event_payload


_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')


def _identifier(value: Any, field: str) -> str:
    result = str(value or '').strip()
    if not _ID_RE.fullmatch(result):
        raise TeamInvariantError(f'invalid {field}')
    return result


def _idempotency_key(value: Any, fallback: str) -> str:
    result = str(value or fallback).strip()
    if not result or len(result) > 240 or any(ord(ch) < 32 for ch in result):
        raise TeamInvariantError('invalid idempotency_key')
    return result


def _bounded_text(value: Any, field: str, *, limit: int = 512) -> str:
    result = str(value or '').strip()
    if not result or len(result) > limit or any(ord(char) < 32 for char in result):
        raise TeamInvariantError(f'invalid {field}')
    return result


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _json_load(value: Any, fallback: Any) -> Any:
    if value in (None, ''):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _validated_handoff_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe_payload = redact_event_payload(payload or {})
    if set(safe_payload) != {'task_contract'}:
        raise TeamInvariantError('handoff payload must contain one TaskContract')
    contract = safe_payload.get('task_contract')
    if not isinstance(contract, dict) or set(contract) != set(TASK_CONTRACT_FIELDS):
        raise TeamInvariantError('invalid TaskContract fields')
    if not str(contract.get('goal') or '').strip() or not str(
        contract.get('trace_id') or ''
    ).strip():
        raise TeamInvariantError('TaskContract goal and trace_id are required')
    for field in ('inputs', 'expected_outputs', 'acceptance_criteria', 'artifact_refs'):
        if not isinstance(contract.get(field), list):
            raise TeamInvariantError(f'TaskContract {field} must be a list')
    if not isinstance(contract.get('deadline'), dict) or not isinstance(
        contract.get('budget'), dict
    ):
        raise TeamInvariantError('TaskContract deadline and budget must be objects')
    if not all(isinstance(item, dict) for item in contract['artifact_refs']):
        raise TeamInvariantError('TaskContract artifact_refs must contain objects')
    return safe_payload


def _new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'


def _team_or_raise(cur, team_id: str):
    cur.execute('SELECT * FROM agent_team_run WHERE team_id = ?', (team_id,))
    row = cur.fetchone()
    if row is None:
        raise TeamNotFoundError('Agent Team 不存在')
    return row


def _assert_version(row, expected_version: int) -> None:
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        raise TeamInvariantError('expected_version 必须为整数') from None
    current = int(row['state_version'])
    if expected != current:
        raise TeamConflictError(
            'state_version 已变化，请刷新后重试',
            current_version=current,
        )


def _insert_event(
    cur,
    *,
    team_id: str,
    event_type: str,
    actor: str,
    payload: Optional[Dict[str, Any]],
    idempotency_key: str,
    team_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    safe_payload = redact_event_payload(payload or {})
    encoded = _json_dump(safe_payload)
    cur.execute(
        'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
        (team_id, idempotency_key),
    )
    existing = cur.fetchone()
    if existing is not None:
        if (
            existing['event_type'] != event_type
            or existing['actor'] != actor
            or existing['team_task_id'] != team_task_id
            or existing['payload_json'] != encoded
        ):
            raise TeamIdempotencyError('team event idempotency_key 已用于不同请求')
        return dict(existing)
    event_id = _new_id('event')
    created_at = dbutil.now_iso()
    cur.execute(
        """INSERT INTO team_event
           (event_id, team_id, event_type, actor, team_task_id, payload_json,
            idempotency_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id, team_id, event_type, actor, team_task_id, encoded,
            idempotency_key, created_at,
        ),
    )
    cur.execute('SELECT * FROM team_event WHERE event_id = ?', (event_id,))
    return dict(cur.fetchone())


def _task_completed_after_cursor(
    cur,
    *,
    team_id: str,
    team_task_id: str,
    cursor: int,
) -> bool:
    """Return whether a durable task completion happened after ``cursor``.

    Human rejection reopens an already-completed Writer/Reviewer dependency
    chain.  The task row alone therefore cannot distinguish the old completion
    from the single post-rejection revision cycle; the ordered event log can.
    """

    cur.execute(
        """SELECT payload_json FROM team_event
           WHERE team_id = ? AND team_task_id = ? AND cursor > ?
             AND event_type = 'team_task_status_changed'
           ORDER BY cursor""",
        (team_id, team_task_id, int(cursor)),
    )
    return any(
        _json_load(row['payload_json'], {}).get('to_status') == 'completed'
        for row in cur.fetchall()
    )


class AgentTeamStore:
    """Persistence facade shared by the pipeline and the read/approval API."""

    @staticmethod
    def create_team_run(
        team_id: str,
        run_id: str,
        task_id: str,
        *,
        idempotency_key: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        run_id = _identifier(run_id, 'run_id')
        task_id = _identifier(task_id, 'task_id')
        idem = _idempotency_key(idempotency_key, f'create:{team_id}')
        safe_config = redact_event_payload(config or {})
        encoded_config = _json_dump(safe_config)
        task_budgets = team_task_budget_allocations(
            Config.LLM_COST_BUDGET_CNY,
            str(safe_config.get('analysis_mode') or 'evidence_debate'),
        )
        from ..utils.run_limits import deadline_epoch_for_run

        run_deadline = deadline_epoch_for_run(run_id)
        trace_id = str(safe_config.get('trace_id') or f'pending-dispatch:{run_id}')
        created = False

        with dbutil._immediate_cursor() as cur:
            cur.execute(
                """SELECT * FROM agent_team_run
                   WHERE team_id = ? OR run_id = ? OR idempotency_key = ?""",
                (team_id, run_id, idem),
            )
            rows = cur.fetchall()
            if rows:
                if not all(
                    item['team_id'] == team_id
                    and item['run_id'] == run_id
                    and item['task_id'] == task_id
                    and item['idempotency_key'] == idem
                    and item['config_json'] == encoded_config
                    for item in rows
                ):
                    raise TeamIdempotencyError(
                        'team_id、run_id 或 idempotency_key 已用于不同 Team run'
                    )
            else:
                timestamp = dbutil.now_iso()
                cur.execute(
                    """INSERT INTO agent_team_run
                       (team_id, run_id, task_id, status, state_version,
                        budget_cny, current_stage, rejection_count, config_json,
                        idempotency_key, created_at, updated_at)
                       VALUES (?, ?, ?, 'pending', 0, ?, 'confirmed', 0,
                               ?, ?, ?, ?)""",
                    (
                        team_id, run_id, task_id,
                        float(Config.LLM_COST_BUDGET_CNY),
                        encoded_config, idem, timestamp, timestamp,
                    ),
                )
                task_ids = {
                    template.task_key: f'{team_id}:{template.task_key}'
                    for template in DEFAULT_TEAM_DAG
                }
                for ordinal, template in enumerate(DEFAULT_TEAM_DAG):
                    team_task_id = task_ids[template.task_key]
                    depends_on = [task_ids[key] for key in template.depends_on]
                    task_contract = build_task_contract(
                        goal=template.title,
                        inputs=(
                            [
                                {
                                    'type': 'team_task',
                                    'team_task_id': dependency,
                                }
                                for dependency in depends_on
                            ]
                            or [{
                                'type': 'confirmed_task_card',
                                'artifact_ref': f'run://{run_id}/run.json',
                            }]
                        ),
                        expected_outputs=[{
                            'task_key': template.task_key,
                            'result': 'durable_result_with_artifact_refs',
                        }],
                        acceptance_criteria=[
                            'respect the assigned role and MCP allowlist',
                            'persist side effects with CAS and an idempotency key',
                            'return only bounded summaries and immutable ArtifactRefs',
                        ],
                        deadline={
                            'epoch_seconds': run_deadline,
                            'timeout_seconds': Config.PIPELINE_TIMEOUT_SECONDS,
                        },
                        budget={
                            'currency': 'CNY',
                            'limit_cny': task_budgets[template.task_key],
                        },
                        artifact_refs=[],
                        trace_id=trace_id,
                    )
                    cur.execute(
                        """INSERT INTO team_task
                           (team_task_id, team_id, task_key, title, assigned_agent,
                            role_id, status, state_version, attempt_count,
                            budget_cny, ordinal,
                            depends_on_json, input_json, idempotency_key,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?, ?,
                                   ?, ?, ?)""",
                        (
                            team_task_id, team_id, template.task_key, template.title,
                            template.assigned_agent, template.role_id,
                            task_budgets[template.task_key],
                            ordinal,
                            _json_dump(depends_on), _json_dump(task_contract),
                            f'dag:{template.task_key}',
                            timestamp, timestamp,
                        ),
                    )
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='team_created',
                    actor='chengzhu-backend',
                    payload={
                        'run_id': run_id,
                        'task_id': task_id,
                        'agent_roles': list(DEFAULT_AGENT_ROLE_IDS),
                        'task_count': len(DEFAULT_TEAM_DAG),
                    },
                    idempotency_key=f'{idem}:event',
                )
                created = True

        snapshot = AgentTeamStore.get_team(team_id)
        snapshot['created'] = created
        return snapshot

    @staticmethod
    def get_team(team_id: str) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        with dbutil.db_cursor() as cur:
            team_row = _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT * FROM team_task WHERE team_id = ? '
                'ORDER BY ordinal, team_task_id',
                (team_id,),
            )
            task_rows = cur.fetchall()
            cur.execute(
                'SELECT * FROM team_handoff WHERE team_id = ? '
                'ORDER BY created_at, handoff_id',
                (team_id,),
            )
            handoff_rows = cur.fetchall()
            cur.execute(
                'SELECT * FROM artifact_manifest WHERE team_id = ? '
                'ORDER BY artifact_type, artifact_version, artifact_id',
                (team_id,),
            )
            artifact_rows = cur.fetchall()
            cur.execute(
                'SELECT * FROM human_approval WHERE team_id = ? '
                'ORDER BY created_at, approval_id',
                (team_id,),
            )
            approval_rows = cur.fetchall()
            cur.execute(
                'SELECT COALESCE(MAX(cursor), 0) AS cursor FROM team_event '
                'WHERE team_id = ?',
                (team_id,),
            )
            event_cursor = int(cur.fetchone()['cursor'])
            latest_artifact_row = None
            if team_row['latest_artifact_id']:
                cur.execute(
                    'SELECT * FROM artifact_manifest WHERE artifact_id = ?',
                    (team_row['latest_artifact_id'],),
                )
                latest_artifact_row = cur.fetchone()

        team = dict(team_row)
        team['config'] = _json_load(team.pop('config_json', None), {})
        team.pop('idempotency_key', None)
        team['state_version'] = int(team['state_version'])
        team['attempt_count'] = int(team.get('attempt_count') or 0)
        team['budget_cny'] = float(team.get('budget_cny') or 0)
        team['degraded'] = bool(team.get('degraded'))
        team['degradation_reasons'] = _json_load(
            team.pop('degradation_json', None), []
        )
        team['rejection_count'] = int(team['rejection_count'])

        tasks: List[Dict[str, Any]] = []
        for row in task_rows:
            item = dict(row)
            item['depends_on'] = _json_load(item.pop('depends_on_json', None), [])
            item['input'] = _json_load(item.pop('input_json', None), {})
            item['output'] = _json_load(item.pop('output_json', None), None)
            item.pop('idempotency_key', None)
            item['state_version'] = int(item['state_version'])
            item['attempt_count'] = int(item.get('attempt_count') or 0)
            item['budget_cny'] = float(item.get('budget_cny') or 0)
            tasks.append(item)

        handoffs: List[Dict[str, Any]] = []
        for row in handoff_rows:
            item = dict(row)
            item['payload'] = _json_load(item.pop('payload_json', None), {})
            item.pop('idempotency_key', None)
            item['state_version'] = int(item['state_version'])
            handoffs.append(item)

        artifacts: List[Dict[str, Any]] = []
        for row in artifact_rows:
            item = dict(row)
            item['metadata'] = _json_load(item.pop('metadata_json', None), {})
            item.pop('idempotency_key', None)
            item['requires_approval'] = bool(item['requires_approval'])
            item['is_latest'] = bool(item['is_latest'])
            item['state_version'] = int(item['state_version'])
            item['schema_version'] = int(item.get('schema_version') or 1)
            artifacts.append(item)

        latest_artifact = None
        if latest_artifact_row is not None:
            latest_artifact = dict(latest_artifact_row)
            latest_artifact['metadata'] = _json_load(
                latest_artifact.pop('metadata_json', None), {}
            )
            latest_artifact.pop('idempotency_key', None)
            latest_artifact['requires_approval'] = bool(
                latest_artifact['requires_approval']
            )
            latest_artifact['is_latest'] = bool(latest_artifact['is_latest'])
            latest_artifact['state_version'] = int(latest_artifact['state_version'])
            latest_artifact['schema_version'] = int(
                latest_artifact.get('schema_version') or 1
            )

        approvals = []
        for row in approval_rows:
            approval = dict(row)
            approval.pop('idempotency_key', None)
            approvals.append(approval)

        return {
            'source': 'live',
            'team': team,
            'agent_roles': list(DEFAULT_AGENT_ROLE_IDS),
            'tasks': tasks,
            'handoffs': handoffs,
            'artifacts': artifacts,
            'latest_artifact': latest_artifact,
            'approvals': approvals,
            'event_cursor': event_cursor,
        }

    @staticmethod
    def record_dispatch_metadata(
        team_id: str,
        *,
        matrix_room_id: str,
        matrix_event_id: str,
        element_url: str,
        trace_id: str,
        span_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Persist Matrix/trace linkage without changing workflow state.

        Dispatch delivery is external metadata, not a second state transition.
        The event and linkage columns are written in one SQLite transaction so
        a process restart never leaves the UI dependent on Matrix history.
        """

        team_id = _identifier(team_id, 'team_id')
        matrix_room_id = _bounded_text(matrix_room_id, 'matrix_room_id')
        matrix_event_id = _bounded_text(matrix_event_id, 'matrix_event_id')
        element_url = _bounded_text(element_url, 'element_url', limit=2000)
        trace_id = _identifier(trace_id, 'trace_id')
        span_id = _identifier(span_id, 'span_id')
        idem = _idempotency_key(idempotency_key, '')
        event_key = f'dispatch:{idem}'
        payload = {
            'matrix_room_id': matrix_room_id,
            'matrix_event_id': matrix_event_id,
            'element_url': element_url,
            'trace_id': trace_id,
            'span_id': span_id,
        }
        with dbutil._immediate_cursor() as cur:
            team = _team_or_raise(cur, team_id)
            for column, value in payload.items():
                existing = team[column]
                if existing not in (None, '', value):
                    raise TeamIdempotencyError(
                        f'dispatch metadata conflicts on {column}'
                    )
            timestamp = dbutil.now_iso()
            cur.execute(
                """UPDATE agent_team_run
                   SET matrix_room_id = ?, matrix_event_id = ?, element_url = ?,
                       trace_id = ?, span_id = ?, updated_at = ?
                   WHERE team_id = ?""",
                (
                    matrix_room_id, matrix_event_id, element_url, trace_id, span_id,
                    timestamp, team_id,
                ),
            )
            cur.execute(
                'SELECT * FROM team_task WHERE team_id = ?',
                (team_id,),
            )
            for task_row in cur.fetchall():
                contract = _json_load(task_row['input_json'], {})
                if set(contract) != set(TASK_CONTRACT_FIELDS):
                    dependencies = _json_load(task_row['depends_on_json'], [])
                    contract = build_task_contract(
                        goal=str(task_row['title']),
                        inputs=(
                            [
                                {'type': 'team_task', 'team_task_id': dependency}
                                for dependency in dependencies
                            ]
                            or [{
                                'type': 'confirmed_task_card',
                                'artifact_ref': f'run://{team["run_id"]}/run.json',
                            }]
                        ),
                        expected_outputs=[{
                            'task_key': str(task_row['task_key']),
                            'result': 'durable_result_with_artifact_refs',
                        }],
                        acceptance_criteria=[
                            'respect the assigned role and MCP allowlist',
                            'persist side effects with CAS and an idempotency key',
                            'return only bounded summaries and immutable ArtifactRefs',
                        ],
                        deadline={
                            'timeout_seconds': Config.PIPELINE_TIMEOUT_SECONDS,
                        },
                        budget={
                            'currency': 'CNY',
                            'limit_cny': float(task_row['budget_cny'] or 0),
                        },
                        artifact_refs=[],
                        trace_id=trace_id,
                    )
                contract['trace_id'] = trace_id
                cur.execute(
                    'UPDATE team_task SET input_json = ?, updated_at = ? '
                    'WHERE team_task_id = ?',
                    (_json_dump(contract), timestamp, task_row['team_task_id']),
                )
            _insert_event(
                cur,
                team_id=team_id,
                event_type='matrix_dispatch_sent',
                actor='chengzhu-backend',
                payload=payload,
                idempotency_key=event_key,
            )
        return AgentTeamStore.get_team(team_id)

    @staticmethod
    def mark_degraded(team_id: str, reason: str) -> Dict[str, Any]:
        """Idempotently persist a bounded degradation reason on the run."""

        team_id = _identifier(team_id, 'team_id')
        safe_reason = _bounded_text(
            redact_event_payload(str(reason or 'unspecified')),
            'degradation_reason',
            limit=160,
        )
        digest = hashlib.sha256(safe_reason.encode('utf-8')).hexdigest()
        event_key = f'team-degraded:{digest}'
        with dbutil._immediate_cursor() as cur:
            team = _team_or_raise(cur, team_id)
            reasons = _json_load(team['degradation_json'], [])
            if not isinstance(reasons, list):
                reasons = []
            if safe_reason not in reasons:
                reasons.append(safe_reason)
            timestamp = dbutil.now_iso()
            cur.execute(
                """UPDATE agent_team_run
                   SET degraded = 1, degradation_json = ?, updated_at = ?
                   WHERE team_id = ?""",
                (_json_dump(reasons[:50]), timestamp, team_id),
            )
            _insert_event(
                cur,
                team_id=team_id,
                event_type='team_degraded',
                actor='chengzhu-backend',
                payload={'reason': safe_reason},
                idempotency_key=event_key,
            )
        return AgentTeamStore.get_team(team_id)

    @staticmethod
    def get_team_for_task(
        task_id: str,
        *,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve a Team by task/run ownership, defaulting to its latest run."""

        task_id = _identifier(task_id, 'task_id')
        if run_id is not None:
            run_id = _identifier(run_id, 'run_id')
        with dbutil.db_cursor() as cur:
            if run_id:
                cur.execute(
                    'SELECT team_id FROM agent_team_run '
                    'WHERE task_id = ? AND run_id = ?',
                    (task_id, run_id),
                )
            else:
                cur.execute(
                    'SELECT team_id FROM agent_team_run WHERE task_id = ? '
                    'ORDER BY created_at DESC, run_id DESC LIMIT 1',
                    (task_id,),
                )
            row = cur.fetchone()
            cur.execute(
                'SELECT team_id FROM agent_team_run WHERE task_id = ? '
                'ORDER BY created_at DESC, run_id DESC LIMIT 1',
                (task_id,),
            )
            latest_row = cur.fetchone()
        if row is None:
            raise TeamNotFoundError('run 不存在、不属于该任务或尚未创建 Agent Team')
        snapshot = AgentTeamStore.get_team(str(row['team_id']))
        if latest_row is not None and latest_row['team_id'] != row['team_id']:
            snapshot['source'] = 'replay'
            snapshot['replay'] = True
        task_status = ''
        retry_stage = ''
        research_task = None
        try:
            from ..models.research_task import ResearchTask, task_card_for_run

            research_task = ResearchTask.load(task_id)
            run_card = (
                task_card_for_run(research_task, str(snapshot['team']['run_id']))
                if research_task is not None else {}
            )
            if str((run_card or {}).get('execution_mode') or 'agentteams') == 'replay':
                snapshot['source'] = 'replay'
                snapshot['replay'] = True
            if (
                research_task is not None
                and research_task.current_run_id == snapshot['team']['run_id']
            ):
                task_status = research_task.status.value
                retry_stage = str(
                    (research_task.progress_detail or {}).get('team_stage') or ''
                )
        except Exception:
            pass
        with dbutil.db_cursor() as cur:
            cur.execute(
                """SELECT a.run_id, a.artifact_id, a.artifact_version,
                          t.created_at
                   FROM artifact_manifest a
                   JOIN agent_team_run t ON t.team_id = a.team_id
                   JOIN human_approval h ON h.approval_id = a.approval_id
                   WHERE t.task_id = ? AND a.is_latest = 0
                     AND a.artifact_type = 'report'
                     AND a.status = 'published' AND h.decision = 'approved'
                     AND h.authority = 'vue'
                   ORDER BY t.created_at DESC, a.artifact_version DESC""",
                (task_id,),
            )
            target_rows = cur.fetchall()
        seen_runs = set()
        rollback_targets = []
        for target in target_rows:
            target_run_id = str(target['run_id'])
            if target_run_id in seen_runs:
                continue
            seen_runs.add(target_run_id)
            rollback_targets.append({
                'run_id': target_run_id,
                'artifact_id': target['artifact_id'],
                'artifact_version': int(target['artifact_version']),
                'label': target_run_id,
            })
        is_live = snapshot.get('source') != 'replay'
        rollback_retry = is_live and retry_stage == 'rollback_retry_pending'
        retry_target = None
        if rollback_retry:
            target_run_id = str(
                (research_task.progress_detail or {}).get(
                    'rollback_target_run_id'
                ) or ''
            )
            if target_run_id:
                try:
                    target = AgentTeamStore.resolve_published_artifact(
                        task_id, target_run_id,
                    )
                    retry_target = {
                        'run_id': target_run_id,
                        'artifact_id': target['artifact_id'],
                        'artifact_version': int(target['artifact_version']),
                        'label': f'{target_run_id} (继续回滚)',
                    }
                    if target_run_id not in seen_runs:
                        rollback_targets.insert(0, retry_target)
                except (TeamNotFoundError, TeamInvariantError):
                    pass
        rollback_allowed = (
            is_live
            and snapshot['team']['status'] == 'published'
            and (
                task_status in {'completed', 'completed_partial'}
                or rollback_retry
            )
            and bool(rollback_targets)
        )
        snapshot['rollback'] = {
            'allowed': rollback_allowed,
            'targets': rollback_targets,
            **({
                'retry': {
                    'required': True,
                    'target': retry_target,
                    'expected_version': snapshot['team']['state_version'],
                },
            } if rollback_retry else {}),
        }
        approval_retry = (
            is_live
            and (
                snapshot['team']['status'] == 'approved'
                or retry_stage == 'publish_retry_pending'
            )
        )
        if approval_retry:
            approved_ids = {
                item.get('approval_id')
                for item in snapshot.get('approvals') or []
                if item.get('decision') == 'approved'
                and item.get('authority') == APPROVAL_AUTHORITY
            }
            approved_artifact = next((
                item for item in reversed(snapshot['artifacts'])
                if item.get('artifact_type') == 'report'
                and item.get('status') in {'approved', 'published'}
                and item.get('requires_approval')
                and item.get('approval_id') in approved_ids
            ), None)
            if approved_artifact:
                snapshot['approval'] = {
                    'required': True,
                    'status': 'waiting_approval',
                    'title': '人工批准已记录，等待完成发布',
                    'summary': '可重试相同批准操作以恢复报告发布。',
                    'artifact_id': approved_artifact['artifact_id'],
                    'expected_version': snapshot['team']['state_version'],
                    'source': APPROVAL_AUTHORITY,
                }
        return snapshot

    @staticmethod
    def resolve_approval_artifact(team_id: str) -> Dict[str, Any]:
        """Resolve the sole pending artifact, with a completed retry fallback."""

        team_id = _identifier(team_id, 'team_id')
        with dbutil.db_cursor() as cur:
            _team_or_raise(cur, team_id)
            cur.execute(
                """SELECT * FROM artifact_manifest
                   WHERE team_id = ? AND status = 'awaiting_approval'
                     AND requires_approval = 1 AND artifact_type = 'report'
                   ORDER BY artifact_version DESC, artifact_id DESC""",
                (team_id,),
            )
            pending = cur.fetchall()
            if len(pending) > 1:
                raise TeamInvariantError('存在多个待审批产物，无法唯一解析')
            if pending:
                row = pending[0]
            else:
                # A no-header HTTP retry must still derive the original key
                # after the first transaction changed the artifact status.
                cur.execute(
                    """SELECT * FROM artifact_manifest
                       WHERE team_id = ? AND requires_approval = 1
                         AND artifact_type = 'report'
                       ORDER BY artifact_version DESC, artifact_id DESC LIMIT 1""",
                    (team_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise TeamNotFoundError('当前 run 没有待审批产物')
        item = dict(row)
        item['metadata'] = _json_load(item.pop('metadata_json', None), {})
        item['requires_approval'] = bool(item['requires_approval'])
        item['is_latest'] = bool(item['is_latest'])
        return item

    @staticmethod
    def resolve_published_artifact(task_id: str, run_id: str) -> Dict[str, Any]:
        """Find the approved published artifact belonging to a historical run."""

        task_id = _identifier(task_id, 'task_id')
        run_id = _identifier(run_id, 'run_id')
        with dbutil.db_cursor() as cur:
            cur.execute(
                """SELECT a.* FROM artifact_manifest a
                   JOIN agent_team_run t ON t.team_id = a.team_id
                   JOIN human_approval h ON h.approval_id = a.approval_id
                   WHERE t.task_id = ? AND a.run_id = ?
                     AND a.artifact_type = 'report'
                     AND a.status = 'published' AND h.decision = 'approved'
                     AND h.authority = 'vue'
                   ORDER BY a.artifact_version DESC, a.artifact_id DESC LIMIT 1""",
                (task_id, run_id),
            )
            row = cur.fetchone()
        if row is None:
            raise TeamNotFoundError('目标 run 没有已批准发布的产物')
        item = dict(row)
        item['metadata'] = _json_load(item.pop('metadata_json', None), {})
        item['requires_approval'] = bool(item['requires_approval'])
        item['is_latest'] = bool(item['is_latest'])
        return item

    @staticmethod
    def resolve_published_artifact_by_id(
        task_id: str,
        artifact_id: str,
    ) -> Dict[str, Any]:
        """Resolve one approved published artifact by task ownership and ID."""

        task_id = _identifier(task_id, 'task_id')
        artifact_id = _identifier(artifact_id, 'artifact_id')
        with dbutil.db_cursor() as cur:
            cur.execute(
                """SELECT a.* FROM artifact_manifest a
                   JOIN agent_team_run t ON t.team_id = a.team_id
                   JOIN human_approval h ON h.approval_id = a.approval_id
                   WHERE t.task_id = ? AND a.artifact_id = ?
                     AND a.artifact_type = 'report'
                     AND a.status = 'published' AND h.decision = 'approved'
                     AND h.authority = 'vue'""",
                (task_id, artifact_id),
            )
            row = cur.fetchone()
        if row is None:
            raise TeamNotFoundError('目标产物未获批准、未发布或不属于该任务')
        item = dict(row)
        item['metadata'] = _json_load(item.pop('metadata_json', None), {})
        item['requires_approval'] = bool(item['requires_approval'])
        item['is_latest'] = bool(item['is_latest'])
        return item

    @staticmethod
    def list_events(
        team_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        try:
            cursor = max(0, int(after_cursor))
            bounded_limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            raise TeamInvariantError('cursor 与 limit 必须为整数') from None
        with dbutil.db_cursor() as cur:
            _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND cursor > ? '
                'ORDER BY cursor LIMIT ?',
                (team_id, cursor, bounded_limit + 1),
            )
            rows = cur.fetchall()
        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        events = []
        for row in rows:
            item = dict(row)
            item['payload'] = _json_load(item.pop('payload_json', None), {})
            item.pop('idempotency_key', None)
            events.append(item)
        return {
            'events': events,
            'next_cursor': int(events[-1]['cursor']) if events else cursor,
            'has_more': has_more,
        }

    @staticmethod
    def append_event(
        team_id: str,
        event_type: str,
        *,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
        team_task_id: Optional[str] = None,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        event_type = _identifier(event_type, 'event_type')
        actor = _identifier(actor, 'actor')
        idem = _idempotency_key(idempotency_key, '')
        if team_task_id is not None:
            team_task_id = _identifier(team_task_id, 'team_task_id')
        with dbutil._immediate_cursor() as cur:
            team = _team_or_raise(cur, team_id)
            row = _insert_event(
                cur,
                team_id=team_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                idempotency_key=idem,
                team_task_id=team_task_id,
            )
        row['payload'] = _json_load(row.pop('payload_json', None), {})
        row.pop('idempotency_key', None)
        return row

    @staticmethod
    def claim_demo_visual_failure(team_id: str) -> bool:
        """Atomically reserve the one server-controlled demo degradation.

        The marker is durable SQLite state rather than process memory, so an
        MCP restart cannot accidentally inject the same failure twice.
        """

        team_id = _identifier(team_id, 'team_id')
        event_key = 'demo:visual-failure-once'
        with dbutil._immediate_cursor() as cur:
            _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT 1 FROM team_event WHERE team_id = ? '
                'AND idempotency_key = ?',
                (team_id, event_key),
            )
            if cur.fetchone() is not None:
                return False
            _insert_event(
                cur,
                team_id=team_id,
                event_type='demo_visual_failure_injected',
                actor='chengzhu-backend',
                payload={
                    'scope': 'bailian-visual-proxy',
                    'mode': 'fail-once-before-upstream',
                },
                idempotency_key=event_key,
            )
            return True

    @staticmethod
    def transition_team(
        team_id: str,
        status: str,
        *,
        expected_version: int,
        idempotency_key: str,
        actor: str = 'chengzhu-backend',
        current_stage: Optional[str] = None,
        terminal_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        if status not in TEAM_STATUSES:
            raise TeamInvariantError('invalid team status')
        if status in {'approved', 'published', 'changes_requested', 'rejected_terminal'}:
            raise TeamInvariantError(
                'approval-controlled status 必须通过人工审批状态机转换'
            )
        actor = _identifier(actor, 'actor')
        if current_stage is not None:
            current_stage = _identifier(current_stage, 'current_stage')
        safe_terminal_reason = (
            redact_event_payload(str(terminal_reason)) if terminal_reason else None
        )
        idem = _idempotency_key(idempotency_key, '')
        with dbutil._immediate_cursor() as cur:
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
                (team_id, f'team-transition:{idem}'),
            )
            replay = cur.fetchone()
            team = _team_or_raise(cur, team_id)
            if replay is not None:
                payload = _json_load(replay['payload_json'], {})
                if (
                    replay['actor'] != actor
                    or payload.get('to_status') != status
                    or payload.get('current_stage') != current_stage
                    or payload.get('terminal_reason') != safe_terminal_reason
                ):
                    raise TeamIdempotencyError('transition idempotency_key 已用于不同状态')
            else:
                _assert_version(team, expected_version)
                if team['status'] == 'approved':
                    raise TeamInvariantError('人工批准已记录，只允许继续发布')
                if team['status'] in {'published', 'rejected_terminal', 'failed'}:
                    raise TeamInvariantError('terminal Team 不允许继续转换')
                timestamp = dbutil.now_iso()
                cur.execute(
                    """UPDATE agent_team_run
                       SET status = ?, current_stage = ?, terminal_reason = ?,
                           state_version = state_version + 1, updated_at = ?,
                           finished_at = CASE WHEN ? IN ('published','rejected_terminal','failed')
                                              THEN ? ELSE finished_at END
                       WHERE team_id = ? AND state_version = ?""",
                    (
                        status, current_stage, safe_terminal_reason, timestamp,
                        status, timestamp, team_id, int(expected_version),
                    ),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('Team CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='team_status_changed',
                    actor=actor,
                    payload={
                        'from_status': team['status'],
                        'to_status': status,
                        'state_version': int(expected_version) + 1,
                        'current_stage': current_stage,
                        'terminal_reason': safe_terminal_reason,
                    },
                    idempotency_key=f'team-transition:{idem}',
                )
        return AgentTeamStore.get_team(team_id)

    @staticmethod
    def transition_task(
        team_id: str,
        team_task_id: str,
        status: str,
        *,
        expected_version: int,
        idempotency_key: str,
        actor: str,
        output: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        team_task_id = _identifier(team_task_id, 'team_task_id')
        actor = _identifier(actor, 'actor')
        if status not in TASK_STATUSES:
            raise TeamInvariantError('invalid task status')
        idem = _idempotency_key(idempotency_key, '')
        safe_output = redact_event_payload(output) if output is not None else None
        if error_code is not None:
            error_code = _identifier(error_code, 'error_code')
        event_key = f'task-transition:{idem}'
        with dbutil._immediate_cursor() as cur:
            team = _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
                (team_id, event_key),
            )
            replay = cur.fetchone()
            cur.execute(
                'SELECT * FROM team_task WHERE team_id = ? AND team_task_id = ?',
                (team_id, team_task_id),
            )
            task = cur.fetchone()
            if task is None:
                raise TeamNotFoundError('Team task 不存在')
            if replay is not None:
                payload = _json_load(replay['payload_json'], {})
                if (
                    replay['actor'] != actor
                    or payload.get('to_status') != status
                    or payload.get('error_code') != error_code
                    or payload.get('output') != safe_output
                ):
                    raise TeamIdempotencyError('task transition key 已用于不同状态')
            else:
                _assert_version(task, expected_version)
                if status == 'running':
                    if team['status'] in {
                        'awaiting_approval', 'approved', 'published',
                        'rejected_terminal', 'failed',
                    }:
                        raise TeamInvariantError(
                            'Team 当前状态不允许启动新的 Worker 任务'
                        )
                    attempts = int(task['attempt_count'] or 0)
                    maximum_attempts = int(
                        TEAM_TASK_MAX_ATTEMPTS.get(str(task['task_key']), 2)
                    )
                    if task['status'] == 'running':
                        raise TeamInvariantError('task is already running')
                    if task['status'] == 'skipped':
                        raise TeamInvariantError('skipped task cannot be started')
                    if task['status'] == 'completed' and task['task_key'] not in {
                        'report-draft', 'compliance-review',
                    }:
                        raise TeamInvariantError('completed task cannot be restarted')
                    if attempts >= maximum_attempts:
                        raise TeamInvariantError('task retry/revision limit exceeded')

                    # A first human rejection permits exactly one fresh
                    # Writer -> Reviewer chain.  Old completed task rows are
                    # insufficient here, so anchor the rule to the ordered
                    # approval_rejected event and subsequent completions.
                    if (
                        int(team['rejection_count'] or 0) > 0
                        and task['task_key'] in {
                            'report-draft', 'compliance-review',
                        }
                    ):
                        cur.execute(
                            """SELECT MAX(cursor) AS rejection_cursor
                               FROM team_event
                               WHERE team_id = ?
                                 AND event_type = 'approval_rejected'""",
                            (team_id,),
                        )
                        rejection_cursor = int(
                            cur.fetchone()['rejection_cursor'] or 0
                        )
                        if _task_completed_after_cursor(
                            cur,
                            team_id=team_id,
                            team_task_id=team_task_id,
                            cursor=rejection_cursor,
                        ):
                            raise TeamInvariantError(
                                '人工驳回后只允许一次 Writer→Reviewer 修订周期'
                            )
                        if task['task_key'] == 'compliance-review':
                            cur.execute(
                                """SELECT team_task_id FROM team_task
                                   WHERE team_id = ? AND task_key = 'report-draft'""",
                                (team_id,),
                            )
                            writer = cur.fetchone()
                            if writer is None or not _task_completed_after_cursor(
                                cur,
                                team_id=team_id,
                                team_task_id=writer['team_task_id'],
                                cursor=rejection_cursor,
                            ):
                                raise TeamInvariantError(
                                    '人工驳回后必须先完成 Writer 修订'
                                )
                    dependencies = _json_load(task['depends_on_json'], [])
                    if dependencies:
                        placeholders = ','.join('?' for _ in dependencies)
                        cur.execute(
                            f'SELECT team_task_id, status FROM team_task '
                            f'WHERE team_id = ? AND team_task_id IN ({placeholders})',
                            (team_id, *dependencies),
                        )
                        dependency_rows = cur.fetchall()
                        completed = {
                            row['team_task_id'] for row in dependency_rows
                            if row['status'] in {'completed', 'skipped'}
                        }
                        if set(dependencies) != completed:
                            raise TeamInvariantError('task dependencies are not complete')
                timestamp = dbutil.now_iso()
                cur.execute(
                    """UPDATE team_task
                       SET status = ?, output_json = ?, error_code = ?,
                           attempt_count = attempt_count + CASE
                             WHEN ? = 'running' AND status != 'running' THEN 1 ELSE 0 END,
                           state_version = state_version + 1, updated_at = ?,
                           started_at = CASE WHEN ? = 'running' AND started_at IS NULL
                                             THEN ? ELSE started_at END,
                           finished_at = CASE WHEN ? IN ('completed','failed','skipped')
                                              THEN ? ELSE finished_at END
                       WHERE team_id = ? AND team_task_id = ? AND state_version = ?""",
                    (
                        status,
                        _json_dump(safe_output) if safe_output is not None else task['output_json'],
                        error_code, status, timestamp, status, timestamp, status, timestamp,
                        team_id, team_task_id, int(expected_version),
                    ),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('Team task CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='team_task_status_changed',
                    actor=actor,
                    team_task_id=team_task_id,
                    payload={
                        'from_status': task['status'],
                        'to_status': status,
                        'state_version': int(expected_version) + 1,
                        'error_code': error_code,
                        'output': safe_output,
                    },
                    idempotency_key=event_key,
                )
        return AgentTeamStore.get_team(team_id)

    @staticmethod
    def create_handoff(
        team_id: str,
        *,
        source_task_id: Optional[str],
        target_task_id: str,
        from_agent: str,
        to_agent: str,
        payload: Optional[Dict[str, Any]],
        idempotency_key: str,
        handoff_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        target_task_id = _identifier(target_task_id, 'target_task_id')
        source_task_id = (
            _identifier(source_task_id, 'source_task_id') if source_task_id else None
        )
        from_agent = _identifier(from_agent, 'from_agent')
        to_agent = _identifier(to_agent, 'to_agent')
        idem = _idempotency_key(idempotency_key, '')
        requested_handoff_id = (
            _identifier(handoff_id, 'handoff_id') if handoff_id else None
        )
        handoff_id = requested_handoff_id or _new_id('handoff')
        safe_payload = _validated_handoff_payload(payload)
        encoded_payload = _json_dump(safe_payload)
        with dbutil._immediate_cursor() as cur:
            _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT * FROM team_handoff WHERE team_id = ? AND idempotency_key = ?',
                (team_id, idem),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    (requested_handoff_id and existing['handoff_id'] != requested_handoff_id)
                    or existing['source_task_id'] != source_task_id
                    or existing['target_task_id'] != target_task_id
                    or existing['from_agent'] != from_agent
                    or existing['to_agent'] != to_agent
                    or existing['payload_json'] != encoded_payload
                ):
                    raise TeamIdempotencyError('handoff key 已用于不同交接')
                handoff_id = existing['handoff_id']
            else:
                task_ids = [target_task_id] + ([source_task_id] if source_task_id else [])
                placeholders = ','.join('?' for _ in task_ids)
                cur.execute(
                    f'SELECT team_task_id FROM team_task WHERE team_id = ? '
                    f'AND team_task_id IN ({placeholders})',
                    (team_id, *task_ids),
                )
                if len(cur.fetchall()) != len(set(task_ids)):
                    raise TeamNotFoundError('handoff task 不存在')
                timestamp = dbutil.now_iso()
                cur.execute(
                    """INSERT INTO team_handoff
                       (handoff_id, team_id, source_task_id, target_task_id,
                        from_agent, to_agent, status, state_version, payload_json,
                        idempotency_key, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                    (
                        handoff_id, team_id, source_task_id, target_task_id,
                        from_agent, to_agent, encoded_payload, idem, timestamp, timestamp,
                    ),
                )
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='handoff_created',
                    actor=from_agent,
                    team_task_id=target_task_id,
                    payload={
                        'handoff_id': handoff_id,
                        'source_task_id': source_task_id,
                        'target_task_id': target_task_id,
                        'to_agent': to_agent,
                        'payload': safe_payload,
                    },
                    idempotency_key=f'handoff:{idem}',
                )
        snapshot = AgentTeamStore.get_team(team_id)
        return next(item for item in snapshot['handoffs'] if item['handoff_id'] == handoff_id)

    @staticmethod
    def transition_handoff(
        team_id: str,
        handoff_id: str,
        status: str,
        *,
        expected_version: int,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        handoff_id = _identifier(handoff_id, 'handoff_id')
        actor = _identifier(actor, 'actor')
        if status not in HANDOFF_STATUSES:
            raise TeamInvariantError('invalid handoff status')
        idem = _idempotency_key(idempotency_key, '')
        event_key = f'handoff-transition:{idem}'
        with dbutil._immediate_cursor() as cur:
            _team_or_raise(cur, team_id)
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
                (team_id, event_key),
            )
            replay = cur.fetchone()
            cur.execute(
                'SELECT * FROM team_handoff WHERE team_id = ? AND handoff_id = ?',
                (team_id, handoff_id),
            )
            handoff = cur.fetchone()
            if handoff is None:
                raise TeamNotFoundError('handoff 不存在')
            if replay is not None:
                if (
                    replay['actor'] != actor
                    or _json_load(replay['payload_json'], {}).get('to_status') != status
                ):
                    raise TeamIdempotencyError('handoff transition key 已用于不同状态')
            else:
                _assert_version(handoff, expected_version)
                timestamp = dbutil.now_iso()
                cur.execute(
                    """UPDATE team_handoff
                       SET status = ?, state_version = state_version + 1,
                           updated_at = ?,
                           accepted_at = CASE WHEN ? = 'accepted' THEN ? ELSE accepted_at END
                       WHERE team_id = ? AND handoff_id = ? AND state_version = ?""",
                    (
                        status, timestamp, status, timestamp,
                        team_id, handoff_id, int(expected_version),
                    ),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('handoff CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='handoff_status_changed',
                    actor=actor,
                    team_task_id=handoff['target_task_id'],
                    payload={
                        'handoff_id': handoff_id,
                        'from_status': handoff['status'],
                        'to_status': status,
                        'state_version': int(expected_version) + 1,
                    },
                    idempotency_key=event_key,
                )
        snapshot = AgentTeamStore.get_team(team_id)
        return next(item for item in snapshot['handoffs'] if item['handoff_id'] == handoff_id)

    @staticmethod
    def register_artifact(
        team_id: str,
        *,
        artifact_type: str,
        uri: str,
        expected_version: int,
        idempotency_key: str,
        artifact_version: Optional[int] = None,
        artifact_id: Optional[str] = None,
        sha256: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        requires_approval: bool = True,
        producer: str = 'chengzhu-backend',
        schema_version: int = 1,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        artifact_type = _identifier(artifact_type, 'artifact_type')
        requested_artifact_id = (
            _identifier(artifact_id, 'artifact_id')
            if artifact_id else None
        )
        artifact_id = requested_artifact_id or _new_id('artifact')
        idem = _idempotency_key(idempotency_key, '')
        uri = str(uri or '').strip()
        if not uri or len(uri) > 2000:
            raise TeamInvariantError('invalid artifact uri')
        if sha256 and not re.fullmatch(r'[A-Fa-f0-9]{64}', str(sha256)):
            raise TeamInvariantError('invalid sha256')
        sha256 = str(sha256).lower() if sha256 else None
        producer = _identifier(producer, 'producer')
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError):
            raise TeamInvariantError('schema_version 必须为正整数') from None
        if schema_version <= 0:
            raise TeamInvariantError('schema_version 必须为正整数')
        safe_metadata = redact_event_payload(metadata or {})
        encoded_metadata = _json_dump(safe_metadata)
        with dbutil._immediate_cursor() as cur:
            cur.execute(
                'SELECT * FROM artifact_manifest WHERE team_id = ? AND idempotency_key = ?',
                (team_id, idem),
            )
            existing = cur.fetchone()
            team = _team_or_raise(cur, team_id)
            if existing is not None:
                requested_version = int(artifact_version or existing['artifact_version'])
                if (
                    (requested_artifact_id and existing['artifact_id'] != requested_artifact_id)
                    or existing['artifact_type'] != artifact_type
                    or existing['uri'] != uri
                    or int(existing['artifact_version']) != requested_version
                    or existing['sha256'] != sha256
                    or existing['producer'] != producer
                    or int(existing['schema_version']) != schema_version
                    or existing['metadata_json'] != encoded_metadata
                    or bool(existing['requires_approval']) != bool(requires_approval)
                ):
                    raise TeamIdempotencyError('artifact key 已用于不同产物')
                artifact_id = existing['artifact_id']
            else:
                _assert_version(team, expected_version)
                if team['status'] == 'approved':
                    raise TeamInvariantError('人工批准已记录，只允许继续发布')
                if team['status'] in {'published', 'rejected_terminal', 'failed'}:
                    raise TeamInvariantError('terminal Team 不允许登记新产物')
                if requires_approval and artifact_type == 'report':
                    cur.execute(
                        """SELECT COUNT(*) AS approval_count
                           FROM human_approval WHERE team_id = ?""",
                        (team_id,),
                    )
                    if int(cur.fetchone()['approval_count']) >= MAX_HUMAN_REJECTIONS:
                        raise TeamInvariantError('最多只允许两个发布审批周期')
                    cur.execute(
                        """SELECT 1 FROM artifact_manifest
                           WHERE team_id = ? AND artifact_type = 'report'
                             AND requires_approval = 1
                             AND status = 'awaiting_approval'
                           LIMIT 1""",
                        (team_id,),
                    )
                    if cur.fetchone() is not None:
                        raise TeamInvariantError('当前审批周期已有待审批报告')
                if artifact_version is None:
                    cur.execute(
                        """SELECT COALESCE(MAX(artifact_version), 0) + 1 AS next_version
                           FROM artifact_manifest
                           WHERE team_id = ? AND artifact_type = ?""",
                        (team_id, artifact_type),
                    )
                    artifact_version = int(cur.fetchone()['next_version'])
                if int(artifact_version) <= 0:
                    raise TeamInvariantError('artifact_version 必须为正整数')
                timestamp = dbutil.now_iso()
                status = 'awaiting_approval' if requires_approval else 'draft'
                cur.execute(
                    """INSERT INTO artifact_manifest
                       (artifact_id, team_id, run_id, artifact_type,
                        artifact_version, uri, sha256, producer, schema_version,
                        status, requires_approval,
                        is_latest, state_version, metadata_json, idempotency_key,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)""",
                    (
                        artifact_id, team_id, team['run_id'], artifact_type,
                        int(artifact_version), uri, sha256, producer,
                        schema_version, status,
                        1 if requires_approval else 0, encoded_metadata, idem, timestamp,
                    ),
                )
                next_team_status = 'awaiting_approval' if requires_approval else team['status']
                cur.execute(
                    """UPDATE agent_team_run
                       SET status = ?, current_stage = ?,
                           state_version = state_version + 1, updated_at = ?,
                           finished_at = NULL, terminal_reason = NULL
                       WHERE team_id = ? AND state_version = ?""",
                    (
                        next_team_status,
                        (
                            'awaiting_publish_approval'
                            if requires_approval else team['current_stage']
                        ),
                        timestamp, team_id, int(expected_version),
                    ),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('artifact registration CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='artifact_registered',
                    actor='chengzhu-backend',
                    payload={
                        'artifact_id': artifact_id,
                        'artifact_type': artifact_type,
                        'artifact_version': int(artifact_version),
                        'requires_approval': bool(requires_approval),
                        'team_state_version': int(expected_version) + 1,
                    },
                    idempotency_key=f'artifact:{idem}',
                )
        snapshot = AgentTeamStore.get_team(team_id)
        return next(item for item in snapshot['artifacts'] if item['artifact_id'] == artifact_id)

    @staticmethod
    def replay_artifact_registration(
        team_id: str,
        idempotency_key: str,
        *,
        artifact_type: str,
        sha256: str,
        producer: str,
        schema_version: int,
        task_id: str,
        run_id: str,
        candidate_path: str,
    ) -> Optional[Dict[str, Any]]:
        """Return an exact durable registration without touching storage.

        The lookup and identity comparison run against one SQLite read
        transaction.  A caller may therefore resolve a committed network
        retry before repeating an external artifact upload.  The URI and
        degradation metadata are deliberately outputs of the original
        registration, not inputs that a retry must reconstruct.
        """

        team_id = _identifier(team_id, 'team_id')
        idem = _idempotency_key(idempotency_key, '')
        artifact_type = _identifier(artifact_type, 'artifact_type')
        producer = _identifier(producer, 'producer')
        task_id = _identifier(task_id, 'task_id')
        run_id = _identifier(run_id, 'run_id')
        candidate_path = _bounded_text(
            candidate_path, 'candidate_path', limit=160
        )
        if not re.fullmatch(r'[A-Fa-f0-9]{64}', str(sha256 or '')):
            raise TeamInvariantError('invalid sha256')
        expected_sha = str(sha256).lower()
        try:
            expected_schema = int(schema_version)
        except (TypeError, ValueError):
            raise TeamInvariantError('schema_version 必须为正整数') from None
        if expected_schema <= 0:
            raise TeamInvariantError('schema_version 必须为正整数')

        with dbutil.db_cursor() as cur:
            team = _team_or_raise(cur, team_id)
            cur.execute(
                """SELECT * FROM artifact_manifest
                   WHERE team_id = ? AND idempotency_key = ?""",
                (team_id, idem),
            )
            existing = cur.fetchone()
            if existing is None:
                return None
            metadata = _json_load(existing['metadata_json'], {})
            if not isinstance(metadata, dict):
                metadata = {}
            if (
                team['task_id'] != task_id
                or team['run_id'] != run_id
                or existing['run_id'] != run_id
                or existing['artifact_type'] != artifact_type
                or existing['sha256'] != expected_sha
                or existing['producer'] != producer
                or int(existing['schema_version']) != expected_schema
                or not bool(existing['requires_approval'])
                or metadata.get('task_id') != task_id
                or metadata.get('run_id') != run_id
                or metadata.get('candidate_path') != candidate_path
            ):
                raise TeamIdempotencyError(
                    'artifact key 已用于不同产物'
                )
            artifact_id = str(existing['artifact_id'])

        snapshot = AgentTeamStore.get_team(team_id)
        return next(
            item for item in snapshot['artifacts']
            if item['artifact_id'] == artifact_id
        )

    @staticmethod
    def decide_approval(
        team_id: str,
        artifact_id: str,
        decision: str,
        *,
        expected_version: int,
        idempotency_key: str,
        authority: str,
        actor: str,
        reason: Optional[str] = None,
        publish_on_approve: bool = True,
    ) -> Dict[str, Any]:
        team_id = _identifier(team_id, 'team_id')
        artifact_id = _identifier(artifact_id, 'artifact_id')
        actor = _identifier(actor, 'actor')
        if authority != APPROVAL_AUTHORITY:
            raise TeamInvariantError('只有 Vue 人工审批入口具有审批权')
        if decision not in {'approved', 'rejected'}:
            raise TeamInvariantError('decision 必须为 approved 或 rejected')
        idem = _idempotency_key(idempotency_key, '')
        safe_reason = redact_event_payload(str(reason)) if reason else None
        replayed = False

        with dbutil._immediate_cursor() as cur:
            cur.execute(
                'SELECT * FROM human_approval WHERE team_id = ? AND idempotency_key = ?',
                (team_id, idem),
            )
            existing = cur.fetchone()
            team = _team_or_raise(cur, team_id)
            if existing is not None:
                if (
                    existing['artifact_id'] != artifact_id
                    or existing['decision'] != decision
                    or existing['authority'] != authority
                    or existing['actor'] != actor
                    or existing['reason'] != safe_reason
                ):
                    raise TeamIdempotencyError('approval key 已用于不同审批')
                cur.execute(
                    'SELECT payload_json FROM team_event '
                    'WHERE team_id = ? AND idempotency_key = ?',
                    (team_id, f'approval:{idem}'),
                )
                event = cur.fetchone()
                expected_published = decision == 'approved' and publish_on_approve
                if (
                    event is None
                    or bool(_json_load(event['payload_json'], {}).get('published'))
                    != expected_published
                ):
                    raise TeamIdempotencyError('approval key 的发布策略不一致')
                approval_id = existing['approval_id']
                replayed = True
            else:
                _assert_version(team, expected_version)
                if team['status'] in {'published', 'rejected_terminal', 'failed'}:
                    raise TeamInvariantError('terminal Team 不允许继续审批')
                cur.execute(
                    'SELECT * FROM artifact_manifest '
                    'WHERE team_id = ? AND artifact_id = ?',
                    (team_id, artifact_id),
                )
                artifact = cur.fetchone()
                if artifact is None:
                    raise TeamNotFoundError('待审批产物不存在')
                if artifact['artifact_type'] != 'report':
                    raise TeamInvariantError('人工发布审批仅适用于 report 产物')
                if not bool(artifact['requires_approval']):
                    raise TeamInvariantError('该产物不是人工审批对象')
                if artifact['status'] != 'awaiting_approval':
                    raise TeamInvariantError('产物已完成审批或不在待审批状态')
                cur.execute(
                    'SELECT 1 FROM human_approval '
                    'WHERE team_id = ? AND artifact_id = ? LIMIT 1',
                    (team_id, artifact_id),
                )
                if cur.fetchone() is not None:
                    raise TeamInvariantError('每个产物版本只能审批一次')

                approval_id = _new_id('approval')
                timestamp = dbutil.now_iso()
                resulting_version = int(expected_version) + 1
                cur.execute(
                    """INSERT INTO human_approval
                       (approval_id, team_id, artifact_id, decision, authority,
                        actor, reason, team_state_version, idempotency_key, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        approval_id, team_id, artifact_id, decision, authority,
                        actor, safe_reason, resulting_version, idem, timestamp,
                    ),
                )

                if decision == 'approved':
                    artifact_status = 'published' if publish_on_approve else 'approved'
                    if publish_on_approve:
                        cur.execute(
                            """UPDATE artifact_manifest SET is_latest = 0
                               WHERE is_latest = 1 AND team_id IN (
                                 SELECT team_id FROM agent_team_run WHERE task_id = ?
                               )""",
                            (team['task_id'],),
                        )
                    cur.execute(
                        """UPDATE artifact_manifest
                           SET status = ?, approval_id = ?, is_latest = ?,
                               state_version = state_version + 1,
                               published_at = CASE WHEN ? THEN ? ELSE published_at END
                           WHERE team_id = ? AND artifact_id = ?""",
                        (
                            artifact_status, approval_id,
                            1 if publish_on_approve else 0,
                            1 if publish_on_approve else 0, timestamp,
                            team_id, artifact_id,
                        ),
                    )
                    team_status = 'published' if publish_on_approve else 'approved'
                    current_stage = 'published' if publish_on_approve else 'approved'
                    latest_id = artifact_id if publish_on_approve else team['latest_artifact_id']
                    finished_at = timestamp if publish_on_approve else None
                    terminal_reason = None
                    event_type = 'approval_approved'
                else:
                    rejection_count = int(team['rejection_count']) + 1
                    terminal = rejection_count >= MAX_HUMAN_REJECTIONS
                    cur.execute(
                        """UPDATE artifact_manifest
                           SET status = 'rejected', approval_id = ?,
                               state_version = state_version + 1
                           WHERE team_id = ? AND artifact_id = ?""",
                        (approval_id, team_id, artifact_id),
                    )
                    team_status = 'rejected_terminal' if terminal else 'changes_requested'
                    current_stage = 'rejected' if terminal else 'revision'
                    latest_id = team['latest_artifact_id']
                    finished_at = timestamp if terminal else None
                    terminal_reason = 'human_rejected_twice' if terminal else None
                    event_type = 'approval_rejected'

                rejection_count_value = (
                    int(team['rejection_count'])
                    if decision == 'approved'
                    else int(team['rejection_count']) + 1
                )
                cur.execute(
                    """UPDATE agent_team_run
                       SET status = ?, current_stage = ?, rejection_count = ?,
                           latest_artifact_id = ?, terminal_reason = ?,
                           state_version = state_version + 1, updated_at = ?,
                           finished_at = ?
                       WHERE team_id = ? AND state_version = ?""",
                    (
                        team_status, current_stage, rejection_count_value,
                        latest_id, terminal_reason, timestamp, finished_at,
                        team_id, int(expected_version),
                    ),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('approval CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type=event_type,
                    actor=actor,
                    payload={
                        'approval_id': approval_id,
                        'artifact_id': artifact_id,
                        'decision': decision,
                        'authority': authority,
                        'reason': safe_reason,
                        'published': decision == 'approved' and publish_on_approve,
                        'rejection_count': rejection_count_value,
                        'terminal': team_status == 'rejected_terminal',
                        'team_state_version': resulting_version,
                    },
                    idempotency_key=f'approval:{idem}',
                )

        snapshot = AgentTeamStore.get_team(team_id)
        approval = next(
            item for item in snapshot['approvals']
            if item['approval_id'] == approval_id
        )
        return {'approval': approval, 'snapshot': snapshot, 'replayed': replayed}

    @staticmethod
    def publish_artifact(
        team_id: str,
        artifact_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        actor: str = 'chengzhu-backend',
    ) -> Dict[str, Any]:
        """Publish only an artifact carrying an affirmative human approval."""

        team_id = _identifier(team_id, 'team_id')
        artifact_id = _identifier(artifact_id, 'artifact_id')
        actor = _identifier(actor, 'actor')
        idem = _idempotency_key(idempotency_key, '')
        event_key = f'publish:{idem}'
        with dbutil._immediate_cursor() as cur:
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
                (team_id, event_key),
            )
            replay = cur.fetchone()
            team = _team_or_raise(cur, team_id)
            if replay is None:
                _assert_version(team, expected_version)
                if team['status'] in {'rejected_terminal', 'failed'}:
                    raise TeamInvariantError('terminal Team 不允许发布')
                cur.execute(
                    """SELECT a.* FROM artifact_manifest a
                       JOIN human_approval h ON h.approval_id = a.approval_id
                       WHERE a.team_id = ? AND a.artifact_id = ?
                         AND a.artifact_type = 'report'
                         AND a.status = 'approved' AND h.decision = 'approved'
                         AND h.authority = 'vue'""",
                    (team_id, artifact_id),
                )
                artifact = cur.fetchone()
                if artifact is None:
                    raise TeamInvariantError('发布必须先取得 Vue 人工批准')
                timestamp = dbutil.now_iso()
                cur.execute(
                    """UPDATE artifact_manifest SET is_latest = 0
                       WHERE is_latest = 1 AND team_id IN (
                         SELECT team_id FROM agent_team_run WHERE task_id = ?
                       )""",
                    (team['task_id'],),
                )
                cur.execute(
                    """UPDATE artifact_manifest
                       SET status = 'published', is_latest = 1,
                           state_version = state_version + 1, published_at = ?
                       WHERE team_id = ? AND artifact_id = ?""",
                    (timestamp, team_id, artifact_id),
                )
                cur.execute(
                    """UPDATE agent_team_run
                       SET status = 'published', current_stage = 'published',
                           latest_artifact_id = ?, state_version = state_version + 1,
                           updated_at = ?, finished_at = ?, terminal_reason = NULL
                       WHERE team_id = ? AND state_version = ?""",
                    (artifact_id, timestamp, timestamp, team_id, int(expected_version)),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('publish CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='artifact_published',
                    actor=actor,
                    payload={
                        'artifact_id': artifact_id,
                        'team_state_version': int(expected_version) + 1,
                    },
                    idempotency_key=event_key,
                )
            else:
                payload = _json_load(replay['payload_json'], {})
                if (
                    replay['actor'] != actor
                    or payload.get('artifact_id') != artifact_id
                ):
                    raise TeamIdempotencyError('publish key 已用于不同产物')
        return AgentTeamStore.get_team(team_id)

    @staticmethod
    def rollback_artifact(
        team_id: str,
        target_artifact_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        authority: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move only the latest pointer to an older approved published artifact."""

        team_id = _identifier(team_id, 'team_id')
        target_artifact_id = _identifier(target_artifact_id, 'target_artifact_id')
        actor = _identifier(actor, 'actor')
        if authority != APPROVAL_AUTHORITY:
            raise TeamInvariantError('只有 Vue 人工入口可以回滚发布版本')
        idem = _idempotency_key(idempotency_key, '')
        safe_reason = redact_event_payload(str(reason)) if reason else None
        event_key = f'rollback:{idem}'
        replayed = False
        with dbutil._immediate_cursor() as cur:
            cur.execute(
                'SELECT * FROM team_event WHERE team_id = ? AND idempotency_key = ?',
                (team_id, event_key),
            )
            replay = cur.fetchone()
            team = _team_or_raise(cur, team_id)
            if replay is not None:
                payload = _json_load(replay['payload_json'], {})
                if (
                    replay['actor'] != actor
                    or payload.get('authority') != authority
                    or payload.get('target_artifact_id') != target_artifact_id
                    or payload.get('reason') != safe_reason
                ):
                    raise TeamIdempotencyError('rollback key 已用于不同回滚')
                replayed = True
            else:
                _assert_version(team, expected_version)
                if team['status'] != 'published':
                    raise TeamInvariantError('仅 published Team 允许回滚')
                cur.execute(
                    """SELECT a.artifact_id FROM artifact_manifest a
                       JOIN agent_team_run latest_team
                         ON latest_team.team_id = a.team_id
                       WHERE latest_team.task_id = ? AND a.is_latest = 1
                         AND a.artifact_type = 'report'
                       LIMIT 1""",
                    (team['task_id'],),
                )
                latest = cur.fetchone()
                global_latest_artifact_id = (
                    latest['artifact_id'] if latest is not None else None
                )
                if global_latest_artifact_id == target_artifact_id:
                    raise TeamInvariantError('目标产物已经是 latest')
                cur.execute(
                    """SELECT a.artifact_id, a.run_id FROM artifact_manifest a
                       JOIN agent_team_run target_team
                         ON target_team.team_id = a.team_id
                       JOIN human_approval h ON h.approval_id = a.approval_id
                       WHERE target_team.task_id = ? AND a.artifact_id = ?
                         AND a.artifact_type = 'report'
                         AND a.status = 'published' AND h.decision = 'approved'
                         AND h.authority = 'vue'""",
                    (team['task_id'], target_artifact_id),
                )
                target = cur.fetchone()
                if target is None:
                    raise TeamInvariantError(
                        '只能回滚到同一任务已获批准且曾发布的产物'
                    )
                previous_artifact_id = global_latest_artifact_id
                timestamp = dbutil.now_iso()
                # Deliberately do not rewrite artifact status, URI, approval or
                # content metadata.  Rollback changes only the latest marker.
                cur.execute(
                    """UPDATE artifact_manifest SET is_latest = 0
                       WHERE is_latest = 1 AND team_id IN (
                         SELECT team_id FROM agent_team_run WHERE task_id = ?
                       )""",
                    (team['task_id'],),
                )
                cur.execute(
                    'UPDATE artifact_manifest SET is_latest = 1 '
                    'WHERE artifact_id = ?',
                    (target_artifact_id,),
                )
                cur.execute(
                    """UPDATE agent_team_run
                       SET latest_artifact_id = ?, state_version = state_version + 1,
                           updated_at = ?
                       WHERE team_id = ? AND state_version = ?""",
                    (target_artifact_id, timestamp, team_id, int(expected_version)),
                )
                if cur.rowcount != 1:
                    raise TeamConflictError('rollback CAS 更新失败')
                _insert_event(
                    cur,
                    team_id=team_id,
                    event_type='artifact_rollback',
                    actor=actor,
                    payload={
                        'previous_artifact_id': previous_artifact_id,
                        'target_artifact_id': target_artifact_id,
                        'target_run_id': target['run_id'],
                        'authority': authority,
                        'reason': safe_reason,
                        'team_state_version': int(expected_version) + 1,
                    },
                    idempotency_key=event_key,
                )
        snapshot = AgentTeamStore.get_team(team_id)
        return {'snapshot': snapshot, 'replayed': replayed}
