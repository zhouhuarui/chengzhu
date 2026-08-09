"""Human-gated report publication and rollback side-effect coordinator.

The durable human decision is always written before filesystem publication.
Publication and rollback are deliberately resumable: repeating the same
operation repairs an interrupted task-root compatibility mirror without
creating a second approval or deleting an immutable run artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from typing import Any, Dict, Optional, Tuple

from ..models.research_task import (
    ResearchTask,
    ResearchTaskStatus,
    task_artifact_folder,
)
from ..observability import traced_span
from ..services import graph_ingest, report_assembler
from ..utils import db as dbutil
from ..utils.llm_audit import safe_error_summary
from ..utils.report_commit import report_bundle_is_committed
from ..utils.task_run_lock import task_run_lock
from .contracts import APPROVAL_AUTHORITY
from .errors import TeamInvariantError, TeamNotFoundError
from .store import AgentTeamStore


_CANDIDATE_RE = re.compile(
    r'^report_candidate_(?:safe_)?v[1-9][0-9]*\.json$'
)
_MAX_CANDIDATE_BYTES = 10 * 1024 * 1024


def _live_context(team_id: str) -> Tuple[Dict[str, Any], ResearchTask]:
    snapshot = AgentTeamStore.get_team(team_id)
    team = snapshot['team']
    scoped = AgentTeamStore.get_team_for_task(
        str(team['task_id']),
        run_id=str(team['run_id']),
    )
    if scoped.get('source') == 'replay' or scoped['team']['team_id'] != team_id:
        raise TeamInvariantError('历史 run 仅支持只读回放')
    task = ResearchTask.load(str(team['task_id']))
    if (
        task is None
        or task.current_run_id != team['run_id']
        or not task.has_run(str(team['run_id']))
    ):
        raise TeamInvariantError('当前 ResearchTask 与 Agent Team run 不一致')
    return scoped, task


def _owned_artifact(snapshot: Dict[str, Any], artifact_id: str) -> Dict[str, Any]:
    artifact = next((
        item for item in snapshot.get('artifacts') or []
        if item.get('artifact_id') == artifact_id
    ), None)
    if artifact is None:
        raise TeamNotFoundError('审批产物不存在或不属于当前 Team')
    return artifact


def _approval_for_artifact(
    snapshot: Dict[str, Any],
    artifact_id: str,
) -> Optional[Dict[str, Any]]:
    return next((
        item for item in reversed(snapshot.get('approvals') or [])
        if item.get('artifact_id') == artifact_id
    ), None)


def _mirror_human_event(
    team_id: str,
    *,
    event_name: str,
    operation_id: str,
    task_id: str,
    run_id: str,
    target_run_id: Optional[str] = None,
) -> None:
    """Best-effort Matrix mirror; SQLite remains the sole authority."""
    try:
        with dbutil.db_cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM team_event "
                "WHERE team_id = ? AND event_type = 'matrix_dispatch_sent' "
                'ORDER BY cursor DESC LIMIT 1',
                (team_id,),
            )
            row = cur.fetchone()
        try:
            dispatch = json.loads(row['payload_json']) if row else {}
        except (TypeError, ValueError):
            dispatch = {}
        room_id = str(dispatch.get('matrix_room_id') or '')
        if not room_id:
            return
        payload = {
            'type': event_name,
            'task_id': task_id,
            'run_id': run_id,
            **({'target_run_id': target_run_id} if target_run_id else {}),
            'authority': APPROVAL_AUTHORITY,
        }
        operation_digest = hashlib.sha256(
            operation_id.encode('utf-8')
        ).hexdigest()
        try:
            from ..integrations.agentteams.client import MatrixClient

            matrix_event_id = MatrixClient().send_message(
                room_id,
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                transaction_id=f'chengzhu-{operation_digest}',
            )
            try:
                AgentTeamStore.append_event(
                    team_id,
                    'matrix_human_event_sent',
                    actor='chengzhu-backend',
                    payload={
                        'human_event': event_name,
                        'matrix_event_id': matrix_event_id,
                    },
                    idempotency_key=f'matrix-human:{operation_digest}:sent',
                )
            except Exception:
                pass
        except Exception:
            try:
                AgentTeamStore.append_event(
                    team_id,
                    'matrix_mirror_degraded',
                    actor='chengzhu-backend',
                    payload={
                        'human_event': event_name,
                        'error_code': 'matrix_mirror_failed',
                    },
                    idempotency_key=(
                        f'matrix-human:{operation_digest}:degraded'
                    ),
                )
            except Exception:
                pass
    except Exception:
        # Even discovery/audit failures must never become a publication gate.
        pass


def _validate_report_artifact(
    task_id: str,
    run_id: str,
    artifact: Dict[str, Any],
) -> None:
    if (
        artifact.get('artifact_type') != 'report'
        or not artifact.get('requires_approval')
        or artifact.get('run_id') != run_id
    ):
        raise TeamInvariantError('只能审批当前 run 的 report 产物')
    metadata = artifact.get('metadata') or {}
    if metadata.get('task_id') != task_id or metadata.get('run_id') != run_id:
        raise TeamInvariantError('candidate metadata 与 Team run 不一致')


def _load_candidate(
    task_id: str,
    run_id: str,
    artifact: Dict[str, Any],
) -> Dict[str, Any]:
    _validate_report_artifact(task_id, run_id, artifact)
    metadata = artifact.get('metadata') or {}
    candidate_name = metadata.get('candidate_path')
    if not isinstance(candidate_name, str) or not _CANDIDATE_RE.fullmatch(candidate_name):
        raise TeamInvariantError('candidate_path 非法')
    expected_sha = str(artifact.get('sha256') or '').lower()
    if not re.fullmatch(r'[a-f0-9]{64}', expected_sha):
        raise TeamInvariantError('candidate artifact 缺少有效 SHA-256')

    run_folder = os.path.abspath(task_artifact_folder(task_id, run_id))
    runs_root = os.path.dirname(run_folder)
    if (
        os.path.basename(runs_root) != 'runs'
        or os.path.basename(run_folder) != run_id
        or os.path.islink(run_folder)
    ):
        raise TeamInvariantError('run 目录非法')
    try:
        run_stat = os.stat(run_folder, follow_symlinks=False)
        if not stat.S_ISDIR(run_stat.st_mode):
            raise OSError('run path is not a directory')
        if os.path.commonpath((
            os.path.realpath(run_folder),
            os.path.realpath(runs_root),
        )) != os.path.realpath(runs_root):
            raise OSError('run path escapes runs root')
        directory_flags = (
            os.O_RDONLY
            | getattr(os, 'O_CLOEXEC', 0)
            | getattr(os, 'O_DIRECTORY', 0)
            | getattr(os, 'O_NOFOLLOW', 0)
        )
        file_flags = (
            os.O_RDONLY
            | getattr(os, 'O_CLOEXEC', 0)
            | getattr(os, 'O_NOFOLLOW', 0)
        )
        directory_fd = os.open(run_folder, directory_flags)
        try:
            candidate_fd = os.open(
                candidate_name,
                file_flags,
                dir_fd=directory_fd,
            )
            try:
                candidate_stat = os.fstat(candidate_fd)
                if (
                    not stat.S_ISREG(candidate_stat.st_mode)
                    or candidate_stat.st_size > _MAX_CANDIDATE_BYTES
                ):
                    raise OSError('candidate is not a bounded regular file')
                chunks = []
                total = 0
                while True:
                    chunk = os.read(
                        candidate_fd,
                        min(1024 * 1024, _MAX_CANDIDATE_BYTES + 1 - total),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_CANDIDATE_BYTES:
                        raise OSError('candidate exceeds size limit')
                payload = b''.join(chunks)
            finally:
                os.close(candidate_fd)
        finally:
            os.close(directory_fd)
        if hashlib.sha256(payload).hexdigest() != expected_sha:
            raise TeamInvariantError('candidate 文件 SHA-256 不匹配')
        candidate = json.loads(payload.decode('utf-8'))
    except TeamInvariantError:
        raise
    except (OSError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise TeamInvariantError('candidate JSON 无法读取') from error
    if not isinstance(candidate, dict):
        raise TeamInvariantError('candidate JSON 必须为对象')
    if candidate.get('task_id') != task_id or candidate.get('run_id') != run_id:
        raise TeamInvariantError('candidate 报告归属不匹配')
    return candidate


def _same_report(left: Optional[Dict[str, Any]], right: Dict[str, Any]) -> bool:
    return isinstance(left, dict) and left == right


def _publish_candidate(
    task_id: str,
    run_id: str,
    candidate: Dict[str, Any],
) -> None:
    run_folder = task_artifact_folder(task_id, run_id)
    if report_bundle_is_committed(run_folder, task_id=task_id, run_id=run_id):
        if not _same_report(
            report_assembler.load_report(task_id, run_id=run_id), candidate,
        ):
            raise TeamInvariantError('run 已提交不同报告，禁止覆盖')
    else:
        report_assembler.publish_report(task_id, candidate, run_id=run_id)

    if not _same_report(
        report_assembler.load_report(task_id, run_id=run_id), candidate,
    ):
        raise TeamInvariantError('run 报告提交校验失败')

    # A crash may occur after the immutable run commit but before the mutable
    # task-root alias.  Repair only that alias on retry.
    if not _same_report(report_assembler.load_report(task_id), candidate):
        report_assembler.publish_report(task_id, candidate, run_id=None)
    if not _same_report(report_assembler.load_report(task_id), candidate):
        raise TeamInvariantError('task latest 报告镜像校验失败')


def _load_published_run(task_id: str, target_run_id: str) -> Dict[str, Any]:
    report = report_assembler.load_report(task_id, run_id=target_run_id)
    if not isinstance(report, dict):
        raise TeamInvariantError('目标 run 没有完整已提交报告')
    if report.get('task_id') != task_id or report.get('run_id') != target_run_id:
        raise TeamInvariantError('目标 run 报告归属不匹配')
    return report


def _mirror_published_run(task_id: str, target_run_id: str) -> Dict[str, Any]:
    report = _load_published_run(task_id, target_run_id)
    if not _same_report(report_assembler.load_report(task_id), report):
        report_assembler.publish_report(task_id, report, run_id=None)
    if not _same_report(report_assembler.load_report(task_id), report):
        raise TeamInvariantError('回滚后的 task latest 报告镜像校验失败')
    graph_ingest.publish_latest_graph(task_id, target_run_id)
    return report


def _save_task_state(
    task: ResearchTask,
    *,
    status: ResearchTaskStatus,
    message: str,
    progress: int,
    team_stage: str,
    report_ready: bool,
    extra: Optional[Dict[str, Any]] = None,
    terminal_error: Optional[str] = None,
) -> None:
    task.progress_detail = {
        **(task.progress_detail or {}),
        'stage': status.value,
        'team_stage': team_stage,
        'run_id': task.current_run_id,
        'execution_mode': 'agentteams',
        'report_ready': report_ready,
        **(extra or {}),
    }
    task.error = terminal_error
    task.set_status(status, message, progress=progress)
    run_id = str(task.current_run_id or '')
    if not run_id or not dbutil.get_task_run(run_id):
        return
    if status in {
        ResearchTaskStatus.COMPLETED,
        ResearchTaskStatus.COMPLETED_PARTIAL,
        ResearchTaskStatus.FAILED,
    }:
        dbutil.finish_task_run(run_id, status.value)
    else:
        dbutil.update_task_run(run_id, status=status.value, finished_at=None)


def _mark_publication_retry(
    task: ResearchTask,
    artifact_id: str,
    error: Exception,
) -> None:
    _save_task_state(
        task,
        status=ResearchTaskStatus.REVIEWING,
        message='人工批准已记录，报告发布未完成，可安全重试',
        progress=99,
        team_stage='publish_retry_pending',
        report_ready=False,
        extra={
            'approval_recorded': True,
            'approval_artifact_id': artifact_id,
            'publication_error': safe_error_summary(error),
        },
    )


def _mark_rollback_retry(
    task: ResearchTask,
    target_run_id: str,
    error: Exception,
) -> None:
    _save_task_state(
        task,
        status=ResearchTaskStatus.REVIEWING,
        message='回滚指针已记录，latest 兼容镜像未完成，可安全重试',
        progress=99,
        team_stage='rollback_retry_pending',
        report_ready=False,
        extra={
            'rollback_target_run_id': target_run_id,
            'rollback_error': safe_error_summary(error),
        },
    )


def coordinate_approval(
    team_id: str,
    artifact_id: str,
    decision: str,
    *,
    expected_version: int,
    idempotency_key: str,
    source: str,
    actor: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a Vue decision, then publish, then mark the artifact latest."""

    if source != APPROVAL_AUTHORITY:
        raise PermissionError('只有 Vue 人工入口具有审批权')
    if decision not in {'approved', 'rejected'}:
        raise TeamInvariantError('decision 必须为 approved 或 rejected')

    with task_run_lock(AgentTeamStore.get_team(team_id)['team']['task_id']):
        snapshot, task = _live_context(team_id)
        team = snapshot['team']
        artifact = _owned_artifact(snapshot, artifact_id)
        artifact_status = str(artifact.get('status') or '')
        _validate_report_artifact(
            str(team['task_id']), str(team['run_id']), artifact,
        )
        superseded_request = (
            artifact_status == 'published'
            and team.get('latest_artifact_id') != artifact_id
        )
        candidate: Optional[Dict[str, Any]] = None
        if decision == 'approved' and not superseded_request:
            try:
                # This is a read-only preflight.  For a fresh request it runs
                # before the approval transaction, while publish_report still
                # runs strictly after the durable human decision.
                candidate = _load_candidate(
                    str(team['task_id']), str(team['run_id']), artifact,
                )
            except Exception as error:
                if artifact_status in {'approved', 'published'}:
                    try:
                        _mark_publication_retry(task, artifact_id, error)
                    except Exception:
                        pass
                    raise RuntimeError('report publication retry required') from error
                raise

        if artifact_status in {'approved', 'published'}:
            if decision != 'approved':
                raise TeamInvariantError('已批准产物不能改为驳回')
            approval = _approval_for_artifact(snapshot, artifact_id)
            if (
                not approval
                or approval.get('decision') != 'approved'
                or approval.get('authority') != APPROVAL_AUTHORITY
            ):
                raise TeamInvariantError('产物缺少有效 Vue 人工批准记录')
            decision_result = {
                'approval': approval,
                'snapshot': snapshot,
                'replayed': True,
            }
        else:
            with traced_span(
                'agentteams.human_approval.persist',
                trace_id=str(team.get('trace_id') or '') or None,
                attributes={
                    'task_id': str(team['task_id']),
                    'run_id': str(team['run_id']),
                    'team_id': team_id,
                    'decision': decision,
                    'authority': source,
                },
            ):
                decision_result = AgentTeamStore.decide_approval(
                    team_id,
                    artifact_id,
                    decision,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    authority=source,
                    actor=actor,
                    reason=reason,
                    publish_on_approve=False,
                )

        if decision == 'rejected':
            rejected = decision_result['snapshot']['team']
            if (
                decision_result.get('replayed')
                and artifact_status == 'rejected'
                and rejected.get('status') not in {
                    'changes_requested', 'rejected_terminal',
                }
            ):
                return {
                    **decision_result,
                    'superseded': True,
                }
            approval_id = str(decision_result['approval']['approval_id'])
            _mirror_human_event(
                team_id,
                event_name='human.rejected',
                operation_id=approval_id,
                task_id=str(team['task_id']),
                run_id=str(team['run_id']),
            )
            count = int(rejected.get('rejection_count') or 0)
            terminal = rejected.get('status') == 'rejected_terminal'
            _save_task_state(
                task,
                status=(
                    ResearchTaskStatus.FAILED
                    if terminal else ResearchTaskStatus.REVIEWING
                ),
                message=(
                    '报告第二次被人工驳回，本次运行已终止'
                    if terminal else '报告被人工驳回，等待团队修订后重新提交'
                ),
                progress=98 if terminal else 95,
                team_stage='rejected' if terminal else 'revision',
                report_ready=False,
                extra={
                    'approval_decision': 'rejected',
                    'approval_rejection_count': count,
                },
                terminal_error='human_rejected_twice' if terminal else None,
            )
            return decision_result

        # An exact approval retry may arrive after a later rollback changed
        # the durable latest pointer.  Idempotency means that retry must not
        # re-promote the superseded report or overwrite the rollback mirror.
        if superseded_request:
            return {
                'approval': decision_result['approval'],
                'snapshot': snapshot,
                'replayed': True,
                'superseded': True,
                'report_ready': True,
            }

        approval_id = str(decision_result['approval']['approval_id'])
        _mirror_human_event(
            team_id,
            event_name='human.approved',
            operation_id=approval_id,
            task_id=str(team['task_id']),
            run_id=str(team['run_id']),
        )
        try:
            if candidate is None:
                raise TeamInvariantError('candidate preflight 未完成')
            with traced_span(
                'agentteams.human_approval.publish',
                trace_id=str(team.get('trace_id') or '') or None,
                attributes={
                    'task_id': str(team['task_id']),
                    'run_id': str(team['run_id']),
                    'team_id': team_id,
                    'artifact_id': artifact_id,
                },
            ):
                _publish_candidate(
                    str(team['task_id']), str(team['run_id']), candidate,
                )
                graph_ingest.publish_latest_graph(
                    str(team['task_id']), str(team['run_id']),
                )

            latest_snapshot = AgentTeamStore.get_team(team_id)
            latest_artifact = _owned_artifact(latest_snapshot, artifact_id)
            if latest_artifact.get('status') == 'published':
                if latest_snapshot['team'].get('latest_artifact_id') != artifact_id:
                    raise TeamInvariantError('已发布产物不是当前 latest')
                published_snapshot = latest_snapshot
            else:
                published_snapshot = AgentTeamStore.publish_artifact(
                    team_id,
                    artifact_id,
                    expected_version=int(latest_snapshot['team']['state_version']),
                    idempotency_key=f'human-approval:{approval_id}',
                    actor='chengzhu-backend',
                )
            partial = bool((task.progress_detail or {}).get('degraded')) or any(
                bool((item.get('output') or {}).get('degraded'))
                for item in (published_snapshot.get('tasks') or [])
            )
            final_status = (
                ResearchTaskStatus.COMPLETED_PARTIAL
                if partial else ResearchTaskStatus.COMPLETED
            )
            _save_task_state(
                task,
                status=final_status,
                message=(
                    '报告已获 Vue 人工批准并以降级说明版本发布'
                    if partial else '报告已获 Vue 人工批准并发布'
                ),
                progress=100,
                team_stage='published',
                report_ready=True,
                extra={
                    'approval_decision': 'approved',
                    'approval_artifact_id': artifact_id,
                    'latest_report_run_id': team['run_id'],
                },
            )
        except Exception as error:
            try:
                _mark_publication_retry(task, artifact_id, error)
            except Exception:
                pass
            raise RuntimeError('report publication retry required') from error

        return {
            'approval': decision_result['approval'],
            'snapshot': published_snapshot,
            'replayed': bool(decision_result.get('replayed')),
            'report_ready': True,
        }


def coordinate_rollback(
    team_id: str,
    target_artifact_id: str,
    *,
    expected_version: int,
    idempotency_key: str,
    source: str,
    actor: str,
    reason: str,
) -> Dict[str, Any]:
    """Switch the durable pointer, then repair report and graph latest aliases."""

    if source != APPROVAL_AUTHORITY:
        raise PermissionError('只有 Vue 人工入口可以回滚发布版本')
    with task_run_lock(AgentTeamStore.get_team(team_id)['team']['task_id']):
        snapshot, task = _live_context(team_id)
        team = snapshot['team']
        rollback_retry = (
            (task.progress_detail or {}).get('team_stage')
            == 'rollback_retry_pending'
        )
        if team.get('status') != 'published' or (
            task.status not in {
                ResearchTaskStatus.COMPLETED,
                ResearchTaskStatus.COMPLETED_PARTIAL,
            }
            and not rollback_retry
        ):
            raise TeamInvariantError('仅已完成发布的当前 Team 可以回滚')
        target = AgentTeamStore.resolve_published_artifact_by_id(
            str(team['task_id']), target_artifact_id,
        )
        # Reject a missing/corrupt immutable target before changing the
        # durable latest pointer.
        _load_published_run(str(team['task_id']), str(target['run_id']))
        with traced_span(
            'agentteams.human_approval.rollback',
            trace_id=str(team.get('trace_id') or '') or None,
            attributes={
                'task_id': str(team['task_id']),
                'run_id': str(team['run_id']),
                'team_id': team_id,
                'target_run_id': str(target['run_id']),
                'authority': source,
            },
        ):
            result = AgentTeamStore.rollback_artifact(
                team_id,
                target_artifact_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                authority=source,
                actor=actor,
                reason=reason,
            )
        _mirror_human_event(
            team_id,
            event_name='human.rollback',
            operation_id=f'rollback-{idempotency_key}',
            task_id=str(team['task_id']),
            run_id=str(team['run_id']),
            target_run_id=str(target['run_id']),
        )
        requested_target_run_id = str(target['run_id'])
        target_run_id = requested_target_run_id
        try:
            # On a delayed idempotent replay, another rollback may already
            # have moved the pointer again.  Mirror the current durable
            # pointer, never the stale request's target.
            durable_artifact_id = str(
                result['snapshot']['team'].get('latest_artifact_id') or ''
            )
            if not durable_artifact_id:
                raise TeamInvariantError('回滚后缺少 durable latest 指针')
            durable_target = AgentTeamStore.resolve_published_artifact_by_id(
                str(team['task_id']), durable_artifact_id,
            )
            target_run_id = str(durable_target['run_id'])
            _mirror_published_run(str(team['task_id']), target_run_id)
            _save_task_state(
                task,
                status=ResearchTaskStatus.COMPLETED,
                message=f'latest 报告已回滚至 {target_run_id}',
                progress=100,
                team_stage='rolled_back',
                report_ready=True,
                extra={
                    'rollback_from_run_id': team['run_id'],
                    'latest_report_run_id': target_run_id,
                    'rollback_target_run_id': target_run_id,
                },
            )
        except Exception as error:
            try:
                _mark_rollback_retry(task, target_run_id, error)
            except Exception:
                pass
            raise RuntimeError('report rollback retry required') from error
        return {
            **result,
            'target_run_id': target_run_id,
            'requested_target_run_id': requested_target_run_id,
            'report_ready': True,
        }
