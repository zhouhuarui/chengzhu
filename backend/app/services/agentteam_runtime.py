"""Deterministic Chengzhu execution plane exposed to AgentTeams Workers.

AgentTeams owns identity, planning and collaboration.  This module owns the
durable side effects: source collection, evidence freezing, hard audit, report
validation and the human publication gate.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..config import Config
from ..models.debate import Challenge, ClaimCard, ClaimStatus, JudgeScore
from ..models.research_task import (
    ResearchTask,
    ResearchTaskStatus,
    task_artifact_folder,
    task_card_for_run,
)
from ..models.task_card import TaskCard
from ..observability import traced_span
from ..team import (
    AgentTeamStore,
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    TeamNotFoundError,
    build_task_contract,
)
from ..utils import db as dbutil
from ..utils.task_run_lock import task_run_lock
from .agent_logger import AgentLogger
from .artifact_store import ArtifactRef, file_sha256, publish_artifacts
from .evidence_store import EvidenceStore
from .evidence_freezer import freeze_evidence
from .financial_normalizer import FinancialNormalizer, load_facts_jsonl, write_facts_jsonl
from .graph_ingest import ingest_task_evidence
from .report_assembler import assemble_report
from .reviewer import Reviewer


ROLE_TASK_KEY = {
    'research-lead': 'research-plan',
    'disclosure-researcher': 'disclosure-research',
    'market-context-researcher': 'market-context-research',
    'quality-analyst': 'quality-analysis',
    'growth-analyst': 'growth-analysis',
    'evidence-judge': 'evidence-judgement',
    'report-writer': 'report-draft',
    'compliance-reviewer': 'compliance-review',
}

GROUP_COLLECTORS = {
    'disclosure': ('announcement', 'financial'),
    'market_context': ('news', 'research', 'industry'),
}

SAFE_STRUCTURED_FIELDS = {
    'statement', 'REPORT_DATE', 'report_period', 'period_type',
    'accumulation_basis', 'consolidation_scope', 'merged_flag', 'currency',
    'publish_date', 'visual_status', 'visual_parse_incomplete',
    'candidate_pages', 'page_count', 'file_name', 'file_sha256',
}

_SAFE_FILE_RE = re.compile(r'^[A-Za-z0-9_.-]{1,160}$')


def _atomic_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f'{path}.tmp-{uuid.uuid4().hex}'
    try:
        with open(temporary, 'x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_jsonl(path: str, rows: Iterable[Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f'{path}.tmp-{uuid.uuid4().hex}'
    try:
        with open(temporary, 'x', encoding='utf-8') as handle:
            for item in rows:
                payload = item.to_dict() if hasattr(item, 'to_dict') else item
                handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: str, fallback: Any) -> Any:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, TypeError, ValueError):
        return fallback


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def _accepted_report_claims(
    run_folder: str,
) -> tuple[Dict[str, Any], List[ClaimCard]]:
    """Load the durable Judge contract and fail closed on any mismatch.

    ``verdict.json`` chooses the publishable Claim IDs, while ``audit.jsonl``
    independently proves that every selected Claim passed all five hard
    checks.  Recomputing ``hard_pass`` from the component booleans avoids
    trusting a persisted convenience field.  ``claims.jsonl`` is the only
    source of report prose; no Writer-supplied sentence is returned here.
    """

    debate_dir = os.path.join(run_folder, 'debate')
    verdict_path = os.path.join(debate_dir, 'verdict.json')
    claims_path = os.path.join(debate_dir, 'claims.jsonl')
    audit_path = os.path.join(debate_dir, 'audit.jsonl')
    if not all(os.path.isfile(path) for path in (
        verdict_path, claims_path, audit_path,
    )):
        raise TeamInvariantError('缺少完整的 Judge 裁决、Claim 或审计产物')

    verdict = _read_json(verdict_path, None)
    if not isinstance(verdict, dict):
        raise TeamInvariantError('Judge 裁决产物格式无效')
    raw_accepted = verdict.get('accepted_claim_ids')
    if not isinstance(raw_accepted, list):
        raise TeamInvariantError('Judge 裁决缺少 accepted_claim_ids')
    accepted_ids = [str(item).strip() for item in raw_accepted]
    if (
        any(not item for item in accepted_ids)
        or len(accepted_ids) != len(set(accepted_ids))
        or len(accepted_ids) > 48
    ):
        raise TeamInvariantError('Judge accepted_claim_ids 无效或重复')

    claims: Dict[str, ClaimCard] = {}
    for raw in _read_jsonl(claims_path):
        claim = ClaimCard.from_dict(raw)
        if not claim.claim_id or claim.claim_id in claims:
            raise TeamInvariantError('Claim 产物包含空 ID 或重复 ID')
        claims[claim.claim_id] = claim

    scores: Dict[str, JudgeScore] = {}
    for raw in _read_jsonl(audit_path):
        score = JudgeScore.from_dict(raw)
        if not score.claim_id or score.claim_id in scores:
            raise TeamInvariantError('审计产物包含空 Claim ID 或重复 Claim ID')
        scores[score.claim_id] = score

    accepted: List[ClaimCard] = []
    for claim_id in accepted_ids:
        claim = claims.get(claim_id)
        score = scores.get(claim_id)
        if claim is None:
            raise TeamInvariantError('Judge 接受了不存在的 Claim')
        if score is None or not score.hard_pass:
            raise TeamInvariantError('Judge 接受的 Claim 未通过确定性硬校验')
        if claim.status != ClaimStatus.ACCEPTED:
            raise TeamInvariantError('Claim 状态与 Judge 裁决不一致')
        if not claim.assertion.strip() or len(claim.assertion) > 1600:
            raise TeamInvariantError('已接受 Claim 的 assertion 无效')
        if len(claim.assumptions) > 12 or any(
            len(str(item)) > 500 for item in claim.assumptions
        ):
            raise TeamInvariantError('已接受 Claim 的 assumptions 超出安全边界')
        accepted.append(claim)
    return verdict, accepted


def _render_accepted_claim(
    claim: ClaimCard,
    evidence_store: EvidenceStore,
) -> tuple[str, List[str]]:
    """Render one audited Claim with citations from the frozen snapshot."""

    display_ids: List[str] = []
    evidence_uids: List[str] = []
    for evidence_uid in claim.evidence_uids:
        card = evidence_store.get(evidence_uid)
        if card is None:
            raise TeamInvariantError('已接受 Claim 引用了冻结快照外的 EvidenceCard')
        canonical_uid = str(card.evidence_uid or evidence_uid)
        if canonical_uid not in evidence_uids:
            evidence_uids.append(canonical_uid)
        display_id = evidence_store.display_id(card)
        if display_id not in display_ids:
            display_ids.append(display_id)
    if not display_ids:
        raise TeamInvariantError('已接受 Claim 缺少冻结 EvidenceCard 引用')
    citations = ''.join(f'[{item}]' for item in display_ids)
    lines = [f'- {claim.assertion.strip()} {citations}']
    lines.extend(
        f'  - 前提：{str(assumption).strip()} {citations}'
        for assumption in claim.assumptions
        if str(assumption).strip()
    )
    return '\n'.join(lines), evidence_uids


def _claim_gated_report_sections(
    raw_sections: Sequence[Any],
    accepted_claims: Sequence[ClaimCard],
    evidence_store: EvidenceStore,
) -> List[Dict[str, Any]]:
    """Use Worker input only as Claim grouping; rebuild every byte of prose."""

    accepted_by_id = {claim.claim_id: claim for claim in accepted_claims}
    accepted_order = [claim.claim_id for claim in accepted_claims]
    requested_groups: List[List[str]] = []
    requested_ids: List[str] = []
    for raw in list(raw_sections)[:16]:
        if not isinstance(raw, dict):
            raise ValueError('report section must be object')
        values = raw.get('claim_ids') or []
        if not isinstance(values, list):
            raise ValueError('evidence_debate section claim_ids must be an array')
        group: List[str] = []
        for value in values:
            claim_id = str(value).strip()
            if not claim_id:
                raise ValueError('claim_ids cannot contain empty values')
            if claim_id not in accepted_by_id:
                raise TeamInvariantError('Writer 只能选择 Judge 接受且 hard-pass 的 Claim')
            if claim_id not in requested_ids:
                requested_ids.append(claim_id)
                group.append(claim_id)
        if group:
            requested_groups.append(group)

    uncovered = [
        claim_id for claim_id in accepted_order if claim_id not in requested_ids
    ]
    if uncovered:
        requested_groups.append(uncovered)

    if not accepted_claims:
        return [{
            'title': '证据不足',
            'goal': '披露确定性 Claim 门禁结果',
            'content': (
                '本次裁决没有可进入正式报告的已接受且通过硬校验结论；'
                '本版本不形成事实性结论。'
            ),
            'claim_ids': [],
            'evidence_uids': [],
            'verdict': 'warning',
            'audited_debate': True,
            'system': True,
        }]

    # Render bounded sections. A Worker may suggest grouping, but cannot make
    # an accepted Claim disappear; oversized groups are split deterministically.
    sections: List[Dict[str, Any]] = []
    for group in requested_groups:
        current_ids: List[str] = []
        current_evidence: List[str] = []
        current_lines: List[str] = []
        for claim_id in group:
            line, evidence_uids = _render_accepted_claim(
                accepted_by_id[claim_id], evidence_store
            )
            candidate = '\n'.join([*current_lines, line])
            if current_lines and len(candidate) > 20_000:
                sections.append({
                    'claim_ids': current_ids,
                    'evidence_uids': current_evidence,
                    'content': '\n'.join(current_lines),
                })
                current_ids = []
                current_evidence = []
                current_lines = []
            if len(line) > 20_000:
                raise TeamInvariantError('已接受 Claim 无法放入有界报告章节')
            current_ids.append(claim_id)
            current_lines.append(line)
            current_evidence.extend(
                item for item in evidence_uids if item not in current_evidence
            )
        if current_lines:
            sections.append({
                'claim_ids': current_ids,
                'evidence_uids': current_evidence,
                'content': '\n'.join(current_lines),
            })
    if len(sections) > 16:
        raise TeamInvariantError('已接受 Claim 超出报告章节安全上限')
    for index, section in enumerate(sections, start=1):
        section.update({
            'title': f'已审计结论（第 {index} 部分）',
            'goal': '呈现 Judge 接受且通过确定性硬校验的 Claim',
            'verdict': 'pending',
            'audited_debate': True,
        })
    return sections


def _request_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _visual_upload_is_authorized(
    run_folder: str,
    artifact_name: str,
    file_sha256_value: str,
) -> bool:
    """Verify immutable, upload-time consent; Worker input is never authority."""

    manifest = _read_json(
        os.path.join(run_folder, 'files', '.visual_authorization.json'), {}
    )
    if int(manifest.get('schema_version') or 0) != 1:
        return False
    for record in manifest.get('records') or []:
        if not isinstance(record, dict):
            continue
        if (
            record.get('authorized') is True
            and record.get('purpose')
            == 'alibaba-cloud-bailian-visual-understanding'
            and str(record.get('file_name') or '') == artifact_name
            and str(record.get('sha256') or '').lower()
            == str(file_sha256_value or '').lower()
            and str(record.get('source') or '') == 'vue-upload-consent'
        ):
            return True
    return False


def _operation_replay(
    team_id: str,
    *,
    event_type: str,
    idempotency_key: str,
    request_digest: str,
) -> Optional[Dict[str, Any]]:
    """Replay a non-terminal MCP write from its redacted durable event."""

    with dbutil.db_cursor() as cur:
        cur.execute(
            'SELECT event_type, payload_json FROM team_event '
            'WHERE team_id = ? AND idempotency_key = ?',
            (team_id, idempotency_key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = _read_json_value(row['payload_json'])
    if (
        row['event_type'] != event_type
        or payload.get('request_digest') != request_digest
        or not isinstance(payload.get('result'), dict)
    ):
        raise TeamIdempotencyError('operation idempotency_key 已用于不同请求')
    return dict(payload['result'])


def _read_json_value(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


def team_id_for_run(run_id: str) -> str:
    with dbutil.db_cursor() as cur:
        cur.execute('SELECT team_id FROM agent_team_run WHERE run_id = ?', (run_id,))
        row = cur.fetchone()
    if not row:
        raise TeamNotFoundError('run 尚未建立 Agent Team')
    return str(row['team_id'])


def _snapshot_for_run(run_id: str) -> Dict[str, Any]:
    return AgentTeamStore.get_team(team_id_for_run(run_id))


def _team_task(snapshot: Dict[str, Any], task_key: str) -> Dict[str, Any]:
    for item in snapshot.get('tasks') or []:
        if item.get('task_key') == task_key:
            return item
    raise TeamNotFoundError(f'Team task missing: {task_key}')


def _task_output_replay(task: Dict[str, Any], idempotency_key: str) -> Optional[Dict[str, Any]]:
    output = task.get('output')
    if (
        task.get('status') == 'completed'
        and isinstance(output, dict)
        and output.get('_idempotency_key') == idempotency_key
    ):
        return output
    return None


def _assert_expected(snapshot: Dict[str, Any], expected_version: int) -> None:
    current = int(snapshot['team']['state_version'])
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        raise TeamInvariantError('expected_version 必须为整数') from None
    if current != expected:
        raise TeamConflictError(
            'state_version 已变化，请刷新后重试', current_version=current
        )


def _set_task_progress(
    task_id: str,
    run_id: str,
    status: ResearchTaskStatus,
    message: str,
    progress: int,
    *,
    team_stage: str,
) -> None:
    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task or task.current_run_id != run_id:
            return
        task.progress_detail = {
            **(task.progress_detail or {}),
            'stage': status.value,
            'team_stage': team_stage,
            'run_id': run_id,
            'execution_mode': 'agentteams',
        }
        task.set_status(status, message, progress=progress)
        if dbutil.get_task_run(run_id):
            dbutil.update_task_run(run_id, status=status.value)


def _mark_run_degraded(task_id: str, run_id: str, reason: str) -> None:
    """Persist a public, bounded degradation marker for final status selection."""

    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task or task.current_run_id != run_id:
            return
        reasons = list((task.progress_detail or {}).get('degradation_reasons') or [])
        safe_reason = str(reason or 'unspecified')[:160]
        if safe_reason not in reasons:
            reasons.append(safe_reason)
        task.progress_detail = {
            **(task.progress_detail or {}),
            'degraded': True,
            'degradation_reasons': reasons,
        }
        task.save()
    AgentTeamStore.mark_degraded(team_id_for_run(run_id), safe_reason)


class AgentTeamRuntime:
    def __init__(self, task_id: str, run_id: str, role: str):
        self.task_id = str(task_id)
        self.run_id = str(run_id)
        self.role = str(role)
        self.team_id = team_id_for_run(self.run_id)
        self.run_folder = task_artifact_folder(self.task_id, self.run_id)
        self.logger = AgentLogger(
            self.task_id,
            agent=self.role,
            run_id=self.run_id,
        )
        task = ResearchTask.load(self.task_id)
        if not task or not task.has_run(self.run_id):
            raise ValueError('run 不存在或不属于任务')
        self.task = task
        self.card = TaskCard.from_dict(task_card_for_run(task, self.run_id))
        if self.card.execution_mode != 'agentteams':
            raise PermissionError('replay run is read-only')

    def snapshot(self) -> Dict[str, Any]:
        return AgentTeamStore.get_team(self.team_id)

    def _enforce_run_limits(self, task_key: str) -> None:
        """Reject new execution after the durable wall-clock/cost ceiling."""

        from ..utils.run_limits import deadline_epoch_for_run, ensure_time_remaining

        ensure_time_remaining(
            deadline_epoch_for_run(self.run_id),
            reserve_seconds=1.0,
            stage=f'agentteams:{task_key}',
        )
        snapshot = self.snapshot()
        hard_budget = float(
            snapshot['team'].get('budget_cny') or Config.LLM_COST_BUDGET_CNY
        )
        committed = float(
            dbutil.llm_budget_totals(self.run_id).get('committed_cny') or 0
        )
        if committed >= hard_budget:
            raise TeamInvariantError('run llm budget exhausted')

    def _begin(
        self,
        task_key: str,
        *,
        expected_version: int,
        idempotency_key: str,
        allowed_roles: Sequence[str],
    ) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        if self.role not in set(allowed_roles):
            raise PermissionError(f'role {self.role} cannot execute {task_key}')
        snapshot = self.snapshot()
        task = _team_task(snapshot, task_key)
        replay = _task_output_replay(task, idempotency_key)
        if replay is not None:
            return task, replay
        self._enforce_run_limits(task_key)
        snapshot = self.snapshot()
        task = _team_task(snapshot, task_key)
        # A duplicated write must replay even when the caller still carries
        # the pre-commit team version.  New writes remain guarded below.
        _assert_expected(snapshot, expected_version)
        if task.get('status') != 'running':
            snapshot = AgentTeamStore.transition_task(
                self.team_id,
                task['team_task_id'],
                'running',
                expected_version=int(task['state_version']),
                idempotency_key=f'{idempotency_key}:start',
                actor=self.role,
            )
            task = _team_task(snapshot, task_key)
        return task, None

    def _complete(
        self,
        task_key: str,
        task: Dict[str, Any],
        output: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        payload = {**output, '_idempotency_key': idempotency_key}
        snapshot = AgentTeamStore.transition_task(
            self.team_id,
            task['team_task_id'],
            'completed',
            expected_version=int(task['state_version']),
            idempotency_key=f'{idempotency_key}:complete',
            actor=self.role,
            output=payload,
        )
        source = _team_task(snapshot, task_key)
        from ..utils.run_limits import deadline_epoch_for_run

        for target in snapshot.get('tasks') or []:
            if source['team_task_id'] not in (target.get('depends_on') or []):
                continue
            inherited = target.get('input') or {}
            artifact_refs = [
                item for item in (output.get('artifact_refs') or [])
                if isinstance(item, dict)
            ]
            task_contract = build_task_contract(
                goal=str(target.get('title') or target['task_key']),
                inputs=[{
                    'type': 'completed_team_task',
                    'team_task_id': source['team_task_id'],
                    'task_key': source['task_key'],
                }],
                expected_outputs=(
                    inherited.get('expected_outputs')
                    or [{
                        'task_key': target['task_key'],
                        'result': 'durable_result_with_artifact_refs',
                    }]
                ),
                acceptance_criteria=(
                    inherited.get('acceptance_criteria')
                    or [
                        'respect the assigned role and MCP allowlist',
                        'persist side effects with CAS and an idempotency key',
                    ]
                ),
                deadline={
                    'epoch_seconds': deadline_epoch_for_run(self.run_id),
                    'timeout_seconds': Config.PIPELINE_TIMEOUT_SECONDS,
                },
                budget={
                    'currency': 'CNY',
                    'limit_cny': float(target.get('budget_cny') or 0),
                },
                artifact_refs=artifact_refs,
                trace_id=str(
                    snapshot['team'].get('trace_id')
                    or inherited.get('trace_id')
                    or f'pending-dispatch:{self.run_id}'
                ),
            )
            AgentTeamStore.create_handoff(
                self.team_id,
                source_task_id=source['team_task_id'],
                target_task_id=target['team_task_id'],
                from_agent=self.role,
                to_agent=str(target.get('assigned_agent') or 'chengzhu-backend'),
                payload={'task_contract': task_contract},
                idempotency_key=f'{idempotency_key}:handoff:{target["task_key"]}',
            )
        self.logger.log(
            'team_task_completed',
            task_key,
            {'team_task_id': source['team_task_id']},
            team_task_id=source['team_task_id'],
        )
        return {'ok': True, **output, 'team': self.snapshot()}

    def _event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        task_key: Optional[str] = None,
    ) -> None:
        team_task_id = None
        if task_key:
            team_task_id = _team_task(self.snapshot(), task_key)['team_task_id']
        AgentTeamStore.append_event(
            self.team_id,
            event_type,
            actor=self.role,
            payload=payload,
            team_task_id=team_task_id,
            idempotency_key=idempotency_key,
        )

    def _publish_refs(
        self,
        paths: Sequence[str],
        *,
        artifact_type: str,
        producer: Optional[str] = None,
    ) -> tuple[List[ArtifactRef], bool]:
        refs, degraded = publish_artifacts(
            self.task_id,
            self.run_id,
            paths,
            artifact_type=artifact_type,
            producer=producer or self.role,
        )
        return refs, degraded

    def _set_team_stage(self, stage: str, *, idempotency_key: str) -> Dict[str, Any]:
        snapshot = self.snapshot()
        team = snapshot['team']
        if team.get('current_stage') == stage:
            return snapshot
        if team.get('status') not in {'pending', 'running', 'changes_requested'}:
            return snapshot
        return AgentTeamStore.transition_team(
            self.team_id,
            'running',
            expected_version=int(team['state_version']),
            idempotency_key=idempotency_key,
            actor=self.role,
            current_stage=stage,
        )

    def start_team_run(self, *, expected_version: int, idempotency_key: str) -> Dict[str, Any]:
        if self.role != 'research-lead':
            raise PermissionError('only research-lead can start a team run')
        snapshot = self.snapshot()
        task = _team_task(snapshot, 'research-plan')
        replay = _task_output_replay(task, idempotency_key)
        if replay is not None:
            return {'ok': True, **replay, 'team': snapshot}
        self._enforce_run_limits('research-plan')
        snapshot = self.snapshot()
        task = _team_task(snapshot, 'research-plan')
        _assert_expected(snapshot, expected_version)
        if snapshot['team']['status'] == 'pending':
            snapshot = AgentTeamStore.transition_team(
                self.team_id,
                'running',
                expected_version=int(expected_version),
                idempotency_key=f'{idempotency_key}:team',
                actor=self.role,
                current_stage='research-plan',
            )
        task = _team_task(snapshot, 'research-plan')
        if task['status'] != 'running':
            snapshot = AgentTeamStore.transition_task(
                self.team_id,
                task['team_task_id'],
                'running',
                expected_version=int(task['state_version']),
                idempotency_key=f'{idempotency_key}:start',
                actor=self.role,
            )
            task = _team_task(snapshot, 'research-plan')
        from ..utils.run_limits import deadline_epoch_for_run

        result = self._complete(
            'research-plan',
            task,
            {
                'task_contract': build_task_contract(
                    goal='produce an evidence-bound financial information report',
                    inputs=['confirmed_task_card', 'task_id', 'run_id'],
                    expected_outputs=[
                        'frozen evidence',
                        'audited verdict',
                        'reviewed report',
                    ],
                    acceptance_criteria=[
                        'all factual claims resolve to frozen EvidenceCards',
                        'zero audit-failed claims enter the report',
                        'publication stops at the Vue approval gate',
                    ],
                    deadline={
                        'epoch_seconds': deadline_epoch_for_run(self.run_id),
                        'timeout_seconds': Config.PIPELINE_TIMEOUT_SECONDS,
                    },
                    budget={
                        'currency': 'CNY',
                        'limit_cny': Config.LLM_COST_BUDGET_CNY,
                    },
                    artifact_refs=[],
                    trace_id=str(
                        self.snapshot()['team'].get('trace_id')
                        or f'pending-dispatch:{self.run_id}'
                    ),
                ),
            },
            idempotency_key=idempotency_key,
        )
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.COLLECTING,
            'AgentTeams 已分派双路采集', 8, team_stage='collecting',
        )
        result['team'] = self._set_team_stage(
            'collecting', idempotency_key=f'{idempotency_key}:stage:collecting'
        )
        return result

    def bailian_visual_proxy(
        self,
        artifact_name: str,
        *,
        user_authorized: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Run the pinned Bailian visual capability without exposing its key.

        The uploaded file stays inside the immutable run snapshot.  Workers
        receive only a compact status and an ArtifactRef; page images, Base64
        payloads and extracted source text never travel through Matrix/MCP.
        """

        if self.role != 'disclosure-researcher':
            raise PermissionError('only disclosure-researcher can use visual proxy')
        if not user_authorized:
            raise PermissionError('visual processing requires explicit upload authorization')
        operation_digest = _request_digest({
            'artifact_name': artifact_name,
            'user_authorized': bool(user_authorized),
        })
        replay = _operation_replay(
            self.team_id,
            event_type='official_skill_invoked',
            idempotency_key=f'{idempotency_key}:event',
            request_digest=operation_digest,
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        self._enforce_run_limits('disclosure-research')
        snapshot = self.snapshot()
        _assert_expected(snapshot, expected_version)
        if not _SAFE_FILE_RE.fullmatch(str(artifact_name or '')):
            raise ValueError('invalid artifact_name')
        source = os.path.join(self.run_folder, 'files', artifact_name)
        files_root = os.path.realpath(os.path.join(self.run_folder, 'files'))
        resolved = os.path.realpath(source)
        if (
            not resolved.startswith(files_root + os.sep)
            or os.path.islink(source)
            or not os.path.isfile(source)
        ):
            raise FileNotFoundError('authorized run artifact not found')
        suffix = Path(source).suffix.lower()
        if suffix not in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}:
            raise ValueError('visual proxy supports PDF and image files only')
        source_hash = file_sha256(source)
        if not _visual_upload_is_authorized(
            self.run_folder,
            artifact_name,
            source_hash,
        ):
            raise PermissionError('durable Vue upload consent not found')

        from ..utils.file_parser import FileParser
        from ..utils.llm_audit import safe_error_summary
        from ..utils.run_limits import deadline_epoch_for_run
        from .bailian_official_skill import (
            OFFICIAL_FILE_SHA256,
            OfficialBailianSkillClient,
            OfficialSkillInvocationError,
        )
        from .pdf_visuals import (
            parse_image_visual,
            parse_local_visual_fallback,
            parse_pdf_visuals,
        )

        deadline = deadline_epoch_for_run(self.run_id)
        traditional_text = FileParser.extract_text(source) if suffix == '.pdf' else ''
        official_status = 'completed'
        fallback_reason = None
        with traced_span(
            'agentteams.bailian_visual_proxy',
            attributes={
                'run_id': self.run_id,
                'skill': 'alibabacloud-bailian-image-creator',
            },
        ):
            try:
                if (
                    Config.AGENTTEAMS_DEMO_VISUAL_FAILURE_ONCE
                    and AgentTeamStore.claim_demo_visual_failure(self.team_id)
                ):
                    raise OfficialSkillInvocationError(
                        'demo_visual_failure_injected_once'
                    )
                official_client = OfficialBailianSkillClient(
                    run_id=self.run_id,
                    deadline_epoch=deadline,
                )
                if suffix == '.pdf':
                    visual = parse_pdf_visuals(
                        source,
                        max_pages=Config.VISION_MAX_PAGES,
                        vision_client=official_client,
                        run_id=self.run_id,
                        deadline_epoch=deadline,
                    )
                else:
                    visual = parse_image_visual(
                        source,
                        vision_client=official_client,
                        run_id=self.run_id,
                        deadline_epoch=deadline,
                    )
                official_visual_status = str(visual.get('visual_status') or '')
                if visual.get('visual_incomplete') or official_visual_status in {
                    'not_configured', 'failed', 'partial', 'rate_limited', 'timeout',
                }:
                    raise OfficialSkillInvocationError(
                        'official_skill_incomplete'
                    )
            except Exception as error:
                # The fallback is the existing Chengzhu parser.  It may still
                # recover deterministic PDF text/tables, but the run remains
                # explicitly degraded even when that compatibility parser can
                # also obtain visual output.
                official_status = 'degraded'
                fallback_reason = safe_error_summary(error)
                visual = parse_local_visual_fallback(
                    source,
                    max_pages=Config.VISION_MAX_PAGES,
                    run_id=self.run_id,
                    deadline_epoch=deadline,
                )

        status = str(visual.get('visual_status') or '')
        degraded = official_status != 'completed' or bool(
            visual.get('visual_incomplete')
            or not visual.get('ok', True)
            or status in {'not_configured', 'failed', 'partial', 'rate_limited', 'timeout'}
        )
        result_name = f'bailian_visual_{source_hash[:20]}.json'
        result_path = os.path.join(self.run_folder, result_name)
        stored = {
            'schema_version': 1,
            'skill': 'alibabacloud-bailian-image-creator',
            'execution': 'bailian-visual-proxy',
            'official_skill_commit': Config.AGENTTEAMS_BAILIAN_SKILL_COMMIT,
            'official_script_sha256': OFFICIAL_FILE_SHA256[
                'scripts/image_understanding.py'
            ],
            'official_model': Config.AGENTTEAMS_BAILIAN_SKILL_MODEL,
            'official_status': official_status,
            'file_name': artifact_name,
            'file_sha256': source_hash,
            'visual_skill': 'degraded' if degraded else 'completed',
            'fallback': 'existing-chengzhu-parser' if official_status == 'degraded' else None,
            'fallback_reason': fallback_reason,
            'traditional_text': traditional_text,
            'visual_result': visual,
        }
        if os.path.isfile(result_path):
            prior = _read_json(result_path, {})
            if prior != stored:
                raise FileExistsError('immutable_visual_result_conflict')
        else:
            _atomic_json(result_path, stored)
        refs, store_degraded = self._publish_refs(
            [result_path],
            artifact_type='bailian-visual-result',
            producer='alibabacloud-bailian-image-creator',
        )
        if len(refs) != 1:
            raise RuntimeError('visual result artifact missing')
        ref = refs[0]
        artifact = AgentTeamStore.register_artifact(
            self.team_id,
            artifact_type='visual-analysis',
            uri=ref.uri,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            sha256=ref.sha256,
            metadata={
                'task_id': self.task_id,
                'run_id': self.run_id,
                'file_name': artifact_name,
                'visual_skill': 'degraded' if degraded else 'completed',
                'official_skill_commit': Config.AGENTTEAMS_BAILIAN_SKILL_COMMIT,
                'official_status': official_status,
                'artifact_store_degraded': store_degraded,
            },
            requires_approval=False,
            producer='alibabacloud-bailian-image-creator',
            schema_version=1,
        )
        output = {
            'skill': 'alibabacloud-bailian-image-creator',
            'official_skill_commit': Config.AGENTTEAMS_BAILIAN_SKILL_COMMIT,
            'official_status': official_status,
            'visual_skill': 'degraded' if degraded else 'completed',
            'visual_status': status or ('degraded' if degraded else 'completed'),
            'page_count': int(visual.get('page_count') or 1),
            'candidate_pages': list(visual.get('candidate_pages') or []),
            'artifact': artifact,
            'artifact_ref': ref.to_dict(),
            'degraded': degraded or store_degraded,
        }
        if degraded or store_degraded:
            _mark_run_degraded(
                self.task_id,
                self.run_id,
                'visual_skill_degraded' if degraded else 'artifact_store_degraded',
            )
        self._event(
            'official_skill_invoked',
            {
                'request_digest': operation_digest,
                'result': output,
            },
            idempotency_key=f'{idempotency_key}:event',
            task_key='disclosure-research',
        )
        return {'ok': True, **output, 'team': self.snapshot()}

    def collect_evidence(
        self,
        source_group: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        role_for_group = {
            'disclosure': 'disclosure-researcher',
            'market_context': 'market-context-researcher',
        }
        expected_role = role_for_group.get(source_group)
        if expected_role != self.role:
            raise PermissionError('collector role/source_group mismatch')
        task_key = ROLE_TASK_KEY[self.role]
        task, replay = self._begin(
            task_key,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=(expected_role,),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}

        from .collect_orchestrator import collect_uploaded_files, run_collector
        from ..utils.run_limits import deadline_epoch_for_run

        deadline = deadline_epoch_for_run(self.run_id)
        results: List[Dict[str, Any]] = []
        with traced_span(
            'agentteams.collect_evidence',
            attributes={'run_id': self.run_id, 'source_group': source_group},
        ):
            if source_group == 'disclosure':
                uploaded = collect_uploaded_files(
                    self.task, self.run_id, self.card.to_dict(), self.logger,
                    deadline_epoch=deadline,
                )
                if uploaded is not None:
                    results.append(uploaded)
            for collector in GROUP_COLLECTORS[source_group]:
                latest: Dict[str, Any] = {}
                for attempt in range(2):
                    latest = run_collector(
                        self.task,
                        collector,
                        self.card.to_dict(),
                        self.logger,
                        self.run_id,
                        deadline,
                    )
                    if latest.get('ok'):
                        break
                    self._event(
                        'collector_retry',
                        {'collector': collector, 'attempt': attempt + 1},
                        idempotency_key=f'{idempotency_key}:retry:{collector}:{attempt + 1}',
                        task_key=task_key,
                    )
                results.append(latest)

        paths = [
            os.path.join(self.run_folder, 'evidence', f'{item["agent"]}.jsonl')
            for item in results if item.get('agent')
        ]
        refs, store_degraded = self._publish_refs(
            paths,
            artifact_type='evidence-staging',
        )
        output = {
            'source_group': source_group,
            'results': results,
            'cards': sum(int(item.get('cards') or 0) for item in results),
            'degraded': store_degraded or not any(item.get('ok') for item in results),
            'artifact_refs': [item.to_dict() for item in refs],
        }
        if output['degraded']:
            _mark_run_degraded(
                self.task_id, self.run_id, f'collector:{source_group}'
            )
            self._event(
                'member_degraded',
                {'source_group': source_group, 'artifact_store_degraded': store_degraded},
                idempotency_key=f'{idempotency_key}:degraded',
                task_key=task_key,
            )
        return self._complete(task_key, task, output, idempotency_key=idempotency_key)

    def freeze_evidence(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        task, replay = self._begin(
            'evidence-freeze',
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=('research-lead',),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        collector_tasks = [
            _team_task(self.snapshot(), 'disclosure-research'),
            _team_task(self.snapshot(), 'market-context-research'),
        ]
        successful_groups = 0
        for collector_task in collector_tasks:
            output = collector_task.get('output') or {}
            results = output.get('results') or []
            if any(bool(item.get('ok')) for item in results if isinstance(item, dict)):
                successful_groups += 1
        if successful_groups == 0:
            failed_snapshot = AgentTeamStore.transition_task(
                self.team_id,
                task['team_task_id'],
                'failed',
                expected_version=int(task['state_version']),
                idempotency_key=f'{idempotency_key}:no-evidence',
                actor='chengzhu-backend',
                error_code='all_collectors_failed',
            )
            AgentTeamStore.transition_team(
                self.team_id,
                'failed',
                expected_version=int(failed_snapshot['team']['state_version']),
                idempotency_key=f'{idempotency_key}:team-failed',
                actor='chengzhu-backend',
                current_stage='failed',
                terminal_reason='all_collectors_failed',
            )
            _set_task_progress(
                self.task_id, self.run_id, ResearchTaskStatus.FAILED,
                '双路证据采集均失败，运行已安全终止', 100, team_stage='failed',
            )
            dbutil.finish_task_run(
                self.run_id, ResearchTaskStatus.FAILED.value,
            )
            raise TeamInvariantError('双路证据采集均失败，禁止冻结空证据')
        partial_collection = successful_groups < len(collector_tasks)
        if partial_collection:
            _mark_run_degraded(self.task_id, self.run_id, 'single_collector_group_failed')
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.INGESTING,
            '冻结证据与图谱摄入中', 52, team_stage='freezing',
        )
        with traced_span('agentteams.freeze_evidence', attributes={'run_id': self.run_id}):
            ingest_stats = ingest_task_evidence(
                self.task_id,
                logger=self.logger,
                run_id=self.run_id,
            )
            frozen, index = freeze_evidence(self.task, self.run_id)
            normalizer = FinancialNormalizer(self.card.time_window)
            facts = normalizer.normalize(index.get('items') or [])
            facts_path = os.path.join(self.run_folder, 'normalized_facts.jsonl')
            write_facts_jsonl(facts_path, facts)
        paths = [
            os.path.join(self.run_folder, 'evidence_index.json'),
            facts_path,
            os.path.join(self.run_folder, 'graph.json'),
        ]
        refs, store_degraded = self._publish_refs(
            paths,
            artifact_type='frozen-context',
            producer='chengzhu-backend',
        )
        output = {
            'cards': len(frozen.cards),
            'facts': len(facts),
            'ingest': ingest_stats,
            'degraded': store_degraded or partial_collection,
            'completed_partial': partial_collection,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        result = self._complete(
            'evidence-freeze', task, output, idempotency_key=idempotency_key
        )
        if self.card.analysis_mode == 'direct':
            for analyst_key in ('quality-analysis', 'growth-analysis'):
                snapshot = self.snapshot()
                analyst_task = _team_task(snapshot, analyst_key)
                if analyst_task['status'] not in {'completed', 'skipped'}:
                    AgentTeamStore.transition_task(
                        self.team_id,
                        analyst_task['team_task_id'],
                        'skipped',
                        expected_version=int(analyst_task['state_version']),
                        idempotency_key=f'{idempotency_key}:direct:{analyst_key}',
                        actor='research-lead',
                        output={
                            'analysis_mode': 'direct',
                            'reason': 'dual analyst debate disabled by TaskCard',
                        },
                    )
            next_stage = 'adjudicating'
            next_status = ResearchTaskStatus.ADJUDICATING
            next_message = 'direct 模式已跳过双分析师，等待确定性 Judge'
        else:
            next_stage = 'analyzing'
            next_status = ResearchTaskStatus.NORMALIZING
            next_message = '冻结证据就绪，等待双分析师'
        _set_task_progress(
            self.task_id, self.run_id, next_status,
            next_message, 63, team_stage=next_stage,
        )
        result['team'] = self._set_team_stage(
            next_stage, idempotency_key=f'{idempotency_key}:stage:{next_stage}'
        )
        return result

    def get_frozen_context(
        self,
        *,
        cursor: int = 0,
        limit: int = 30,
        view: str = 'evidence',
    ) -> Dict[str, Any]:
        if self.role not in {
            'quality-analyst', 'growth-analyst', 'evidence-judge',
            'report-writer', 'compliance-reviewer', 'research-lead',
        }:
            raise PermissionError('role cannot read frozen context')
        offset = max(0, int(cursor))
        bounded = max(1, min(int(limit), 50))
        if view == 'facts':
            values = [item.to_dict() for item in load_facts_jsonl(
                os.path.join(self.run_folder, 'normalized_facts.jsonl')
            )]
        elif view == 'verdict':
            values = [_read_json(os.path.join(self.run_folder, 'debate', 'verdict.json'), {})]
        else:
            index = _read_json(os.path.join(self.run_folder, 'evidence_index.json'), {})
            values = []
            for item in index.get('items') or []:
                card = item.get('card') or {}
                provenance = card.get('provenance') or {}
                private = provenance.get('license_scope') == 'private_derived_only'
                structured = card.get('structured') or {}
                values.append({
                    'evidence_uid': item.get('evidence_uid'),
                    'display_id': item.get('display_id'),
                    'source_type': card.get('source_type'),
                    'title': card.get('title'),
                    'publish_time': card.get('publish_time'),
                    'symbol': card.get('symbol'),
                    'excerpt': '' if private else str(card.get('excerpt') or '')[:1200],
                    'structured': {
                        key: structured.get(key) for key in SAFE_STRUCTURED_FIELDS
                        if structured.get(key) not in (None, '')
                    },
                    'private_derived_only': private,
                })
        page = values[offset:offset + bounded]
        return {
            'ok': True,
            'view': view,
            'items': page,
            'next_cursor': offset + len(page),
            'has_more': offset + len(page) < len(values),
            'frozen': os.path.isfile(os.path.join(self.run_folder, 'evidence_index.json')),
        }

    def submit_claims(
        self,
        claims: Sequence[Dict[str, Any]],
        *,
        round_number: int,
        finalize: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if self.role not in {'quality-analyst', 'growth-analyst'}:
            raise PermissionError('only analysts submit claims')
        task_key = ROLE_TASK_KEY[self.role]
        claims = list(claims)
        operation_digest = _request_digest({
            'claims': claims,
            'round_number': round_number,
            'finalize': bool(finalize),
        })
        if not finalize:
            replay = _operation_replay(
                self.team_id,
                event_type='claims_submitted',
                idempotency_key=f'{idempotency_key}:event',
                request_digest=operation_digest,
            )
            if replay is not None:
                return {'ok': True, **replay, 'team': self.snapshot()}
        task, replay = self._begin(
            task_key,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=(self.role,),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        path = os.path.join(self.run_folder, 'debate', 'claims.jsonl')
        existing = [ClaimCard.from_dict(item) for item in _read_jsonl(path)]
        by_id = {item.claim_id: item for item in existing}
        created: List[str] = []
        role_label = (
            '稳健与质量视角' if self.role == 'quality-analyst' else '成长与变化视角'
        )
        for index, raw in enumerate(claims[:24]):
            payload = dict(raw or {})
            payload['role'] = role_label
            payload['round'] = max(1, min(2, int(round_number)))
            payload['claim_id'] = str(
                payload.get('claim_id')
                or 'clm_{}_{}'.format(
                    self.role.replace('-', '_'),
                    hashlib.sha256(
                        f'{idempotency_key}:{index}'.encode('utf-8')
                    ).hexdigest()[:10],
                )
            )[:80]
            claim = ClaimCard.from_dict(payload)
            if not claim.assertion.strip() or len(claim.assertion) > 1600:
                raise ValueError(f'invalid claim at index {index}')
            prior = by_id.get(claim.claim_id)
            if prior and prior.to_dict() != claim.to_dict():
                raise ValueError('claim_id conflict')
            if not prior:
                existing.append(claim)
                by_id[claim.claim_id] = claim
                created.append(claim.claim_id)
        _atomic_jsonl(path, existing)
        refs, degraded = self._publish_refs([path], artifact_type='debate-claims')
        output = {
            'created_claim_ids': created,
            'claim_count': len(existing),
            'degraded': degraded,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        if finalize:
            _set_task_progress(
                self.task_id, self.run_id, ResearchTaskStatus.DEBATING,
                '双分析师提交观点与反证', 74, team_stage='analyzing',
            )
            return self._complete(task_key, task, output, idempotency_key=idempotency_key)
        self._event(
            'claims_submitted', {
                'request_digest': operation_digest,
                'result': output,
            },
            idempotency_key=f'{idempotency_key}:event', task_key=task_key,
        )
        return {'ok': True, **output, 'team': self.snapshot()}

    def submit_challenges(
        self,
        challenges: Sequence[Dict[str, Any]],
        *,
        round_number: int,
        finalize: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if self.role not in {'quality-analyst', 'growth-analyst'}:
            raise PermissionError('only analysts submit challenges')
        task_key = ROLE_TASK_KEY[self.role]
        challenges = list(challenges)
        operation_digest = _request_digest({
            'challenges': challenges,
            'round_number': round_number,
            'finalize': bool(finalize),
        })
        if not finalize:
            replay = _operation_replay(
                self.team_id,
                event_type='challenges_submitted',
                idempotency_key=f'{idempotency_key}:event',
                request_digest=operation_digest,
            )
            if replay is not None:
                return {'ok': True, **replay, 'team': self.snapshot()}
        task, replay = self._begin(
            task_key,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=(self.role,),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        claims = {item.get('claim_id') for item in _read_jsonl(
            os.path.join(self.run_folder, 'debate', 'claims.jsonl')
        )}
        path = os.path.join(self.run_folder, 'debate', 'challenges.jsonl')
        existing = [Challenge.from_dict(item) for item in _read_jsonl(path)]
        by_id = {item.challenge_id: item for item in existing}
        created: List[str] = []
        for index, raw in enumerate(challenges[:24]):
            payload = dict(raw or {})
            payload['role'] = self.role
            payload['round'] = max(1, min(2, int(round_number)))
            payload['challenge_id'] = str(
                payload.get('challenge_id')
                or 'chg_{}_{}'.format(
                    self.role.replace('-', '_'),
                    hashlib.sha256(
                        f'{idempotency_key}:{index}'.encode('utf-8')
                    ).hexdigest()[:10],
                )
            )[:80]
            challenge = Challenge.from_dict(payload)
            if challenge.target_claim_id not in claims:
                raise ValueError(f'unknown target_claim_id at index {index}')
            if not challenge.argument.strip() or len(challenge.argument) > 1600:
                raise ValueError(f'invalid challenge at index {index}')
            prior = by_id.get(challenge.challenge_id)
            if prior and prior.to_dict() != challenge.to_dict():
                raise ValueError('challenge_id conflict')
            if not prior:
                existing.append(challenge)
                by_id[challenge.challenge_id] = challenge
                created.append(challenge.challenge_id)
        _atomic_jsonl(path, existing)
        refs, degraded = self._publish_refs([path], artifact_type='debate-challenges')
        output = {
            'created_challenge_ids': created,
            'challenge_count': len(existing),
            'degraded': degraded,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        if finalize:
            return self._complete(task_key, task, output, idempotency_key=idempotency_key)
        self._event(
            'challenges_submitted', {
                'request_digest': operation_digest,
                'result': output,
            },
            idempotency_key=f'{idempotency_key}:event', task_key=task_key,
        )
        return {'ok': True, **output, 'team': self.snapshot()}

    def audit_debate(
        self,
        accepted_claim_ids: Sequence[str],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        task, replay = self._begin(
            'evidence-judgement',
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=('evidence-judge',),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        from .debate_orchestrator import EvidenceAuditor, JudgeSynthesizer

        evidence_index = _read_json(os.path.join(self.run_folder, 'evidence_index.json'), {})
        facts = load_facts_jsonl(os.path.join(self.run_folder, 'normalized_facts.jsonl'))
        claims = [ClaimCard.from_dict(item) for item in _read_jsonl(
            os.path.join(self.run_folder, 'debate', 'claims.jsonl')
        )]
        challenges = [Challenge.from_dict(item) for item in _read_jsonl(
            os.path.join(self.run_folder, 'debate', 'challenges.jsonl')
        )]
        auditor = EvidenceAuditor(
            evidence_index,
            facts,
            time_window=self.card.time_window,
        )
        scores = auditor.audit_all(claims, challenges)
        challenge_audits = list(auditor.challenge_audits)
        valid_challenge_ids = {
            item['challenge_id'] for item in challenge_audits if item.get('hard_pass')
        }
        valid_challenges = [
            item for item in challenges if item.challenge_id in valid_challenge_ids
        ]
        hard_pass = {item.claim_id for item in scores if item.hard_pass}
        requested = list(dict.fromkeys(str(item) for item in accepted_claim_ids))
        if any(item not in hard_pass for item in requested):
            raise TeamInvariantError('Judge 不能接受 hard_pass=false 的 Claim')
        verdict = JudgeSynthesizer().from_judge_payload(
            {'accepted_claim_ids': requested},
            claims,
            scores,
            valid_challenges,
        )
        debate_dir = os.path.join(self.run_folder, 'debate')
        audit_path = os.path.join(debate_dir, 'audit.jsonl')
        challenge_audit_path = os.path.join(debate_dir, 'challenge_audit.jsonl')
        verdict_path = os.path.join(debate_dir, 'verdict.json')
        claims_path = os.path.join(debate_dir, 'claims.jsonl')
        _atomic_jsonl(claims_path, claims)
        _atomic_jsonl(audit_path, scores)
        _atomic_jsonl(challenge_audit_path, challenge_audits)
        _atomic_json(verdict_path, verdict.to_dict())
        refs, degraded = self._publish_refs(
            [claims_path, audit_path, challenge_audit_path, verdict_path],
            artifact_type='audited-verdict',
        )
        output = {
            'accepted_claim_ids': list(verdict.accepted_claim_ids),
            'hard_pass_claim_ids': sorted(hard_pass),
            'audit_failures': sum(not item.hard_pass for item in scores),
            'degraded': degraded,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        if dbutil.get_debate_run(self.run_id):
            dbutil.update_debate_run(
                self.run_id,
                claim_count=len(claims),
                challenge_count=len(challenges),
                audit_failure_count=output['audit_failures'],
            )
            dbutil.finish_debate_run(
                self.run_id, 'completed', verdict=verdict.to_dict()
            )
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.ADJUDICATING,
            '确定性审计与 Judge 裁决完成', 81, team_stage='adjudicating',
        )
        result = self._complete(
            'evidence-judgement', task, output, idempotency_key=idempotency_key
        )
        result['team'] = self._set_team_stage(
            'writing', idempotency_key=f'{idempotency_key}:stage:writing'
        )
        return result

    def store_report_draft(
        self,
        draft: Dict[str, Any],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        task, replay = self._begin(
            'report-draft',
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=('report-writer',),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        sections = list((draft or {}).get('sections') or [])
        if len(sections) > 16:
            raise ValueError('report draft must contain at most 16 sections')
        claim_gate_enforced = self.card.analysis_mode == 'evidence_debate'
        accepted_claim_ids: List[str] = []
        if claim_gate_enforced:
            verdict, accepted_claims = _accepted_report_claims(self.run_folder)
            evidence_store = EvidenceStore(self.task_id, run_id=self.run_id)
            if not evidence_store.is_frozen:
                raise TeamInvariantError('报告只能引用已冻结 EvidenceCard 快照')
            safe_sections = _claim_gated_report_sections(
                sections, accepted_claims, evidence_store
            )
            accepted_claim_ids = [claim.claim_id for claim in accepted_claims]
            title = '成竹证据审计报告'
            summary = (
                '本报告仅汇总 Evidence Judge 接受且通过确定性硬校验的结论；'
                '未通过门禁的内容不会进入正文。'
                if accepted_claims else
                '本次裁决未形成可进入正式报告的已接受硬校验结论。'
            )
            # The full verdict can contain rejected/disputed prose. Retain only
            # non-narrative provenance plus accepted Claim IDs; the candidate
            # body above is the authoritative, deterministic rendering.
            report_verdict = {
                'status': verdict.get('status'),
                'generated_by': verdict.get('generated_by'),
                'degradation_reason': verdict.get('degradation_reason'),
                'accepted_claim_ids': accepted_claim_ids,
            }
        else:
            if not sections:
                raise ValueError('report draft must contain 1..16 sections')
            safe_sections = []
            for item in sections:
                if not isinstance(item, dict):
                    raise ValueError('report section must be object')
                content = str(item.get('content') or '')
                if not content or len(content) > 20_000:
                    raise ValueError('report section content size invalid')
                safe_sections.append({
                    'title': str(item.get('title') or '')[:160],
                    'goal': str(item.get('goal') or '')[:500],
                    'content': content,
                    'verdict': 'pending',
                })
            verdict = _read_json(
                os.path.join(self.run_folder, 'debate', 'verdict.json'), {}
            )
            report_verdict = verdict or None
            title = str((draft or {}).get('title') or '投研信息整理报告')[:240]
            summary = str((draft or {}).get('summary') or '')[:4000]
        version = len(list(Path(self.run_folder).glob('report_draft_v*.json'))) + 1
        payload = {
            'title': title,
            'summary': summary,
            'sections': safe_sections,
            'mode': 'agentteams',
            'analysis_mode': self.card.analysis_mode,
            'debate_status': 'completed' if claim_gate_enforced else (
                'completed' if verdict else None
            ),
            'debate_verdict': report_verdict,
            'claim_gate_enforced': claim_gate_enforced,
            'accepted_claim_ids': accepted_claim_ids,
            'writer_version': version,
        }
        path = os.path.join(self.run_folder, f'report_draft_v{version}.json')
        _atomic_json(path, payload)
        refs, degraded = self._publish_refs([path], artifact_type='report-draft')
        output = {
            'writer_version': version,
            'claim_gate_enforced': claim_gate_enforced,
            'accepted_claim_ids': accepted_claim_ids,
            'degraded': degraded,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.ANALYZING,
            'AgentTeams Writer 已提交报告草稿', 87, team_stage='writing',
        )
        result = self._complete(
            'report-draft', task, output, idempotency_key=idempotency_key
        )
        result['team'] = self._set_team_stage(
            'reviewing', idempotency_key=f'{idempotency_key}:stage:reviewing'
        )
        return result

    def _latest_draft(self) -> Path:
        paths = sorted(
            Path(self.run_folder).glob('report_draft_v*.json'),
            key=lambda item: int(re.search(r'v(\d+)', item.name).group(1)),
        )
        if not paths:
            raise FileNotFoundError('report draft missing')
        return paths[-1]

    def validate_report(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        operation_digest = _request_digest({'operation': 'validate_report'})
        cached = _operation_replay(
            self.team_id,
            event_type='report_validated',
            idempotency_key=f'{idempotency_key}:validated',
            request_digest=operation_digest,
        )
        if cached is not None:
            return {'ok': True, **cached, 'team': self.snapshot()}
        task, replay = self._begin(
            'compliance-review',
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=('compliance-reviewer',),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        draft_path = self._latest_draft()
        draft = _read_json(str(draft_path), {})
        reviewer = Reviewer(
            self.task_id,
            logger=self.logger,
            run_id=self.run_id,
            allow_llm=False,
        )
        with traced_span(
            'agentteams.review.validate',
            attributes={
                'task_id': self.task_id,
                'run_id': self.run_id,
                'team_id': self.team_id,
                'reviewer': self.role,
            },
        ):
            reviewed = reviewer.run(draft)
        issues = [
            issue
            for section in reviewed.get('sections') or []
            for issue in (section.get('issues') or [])
        ]
        review_version = len(list(Path(self.run_folder).glob('reviewed_report_v*.json'))) + 1
        reviewed_path = os.path.join(
            self.run_folder, f'reviewed_report_v{review_version}.json'
        )
        _atomic_json(reviewed_path, reviewed)
        candidate_path: Optional[str] = None
        assembly_error: Optional[str] = None
        try:
            candidate = assemble_report(
                self.task_id, reviewed, run_id=self.run_id, publish=False
            )
            candidate_path = os.path.join(
                self.run_folder, f'report_candidate_v{review_version}.json'
            )
            _atomic_json(candidate_path, candidate)
        except Exception as error:
            from ..utils.llm_audit import safe_error_summary
            assembly_error = safe_error_summary(error)
        paths = [reviewed_path] + ([candidate_path] if candidate_path else [])
        refs, degraded = self._publish_refs(paths, artifact_type='report-review')
        output = {
            'review_version': review_version,
            'valid': candidate_path is not None and not issues,
            'issues': issues[:100],
            'assembly_error': assembly_error,
            'candidate_path': os.path.basename(candidate_path) if candidate_path else None,
            'degraded': degraded,
            'artifact_refs': [item.to_dict() for item in refs],
        }
        self._event(
            'report_validated',
            {'request_digest': operation_digest, 'result': output},
            idempotency_key=f'{idempotency_key}:validated',
            task_key='compliance-review',
        )
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.REVIEWING,
            '合规与引用确定性校验完成', 93, team_stage='reviewing',
        )
        return {'ok': True, **output, 'team': self.snapshot()}

    def submit_review(
        self,
        decision: str,
        issues: Sequence[Dict[str, Any]],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if decision not in {'pass', 'revise'}:
            raise ValueError('review decision must be pass or revise')
        task, replay = self._begin(
            'compliance-review',
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            allowed_roles=('compliance-reviewer',),
        )
        if replay is not None:
            return {'ok': True, **replay, 'team': self.snapshot()}
        candidates = sorted(
            Path(self.run_folder).glob('report_candidate_v*.json'),
            key=lambda item: int(re.search(r'v(\d+)', item.name).group(1)),
        )
        if decision == 'pass' and not candidates:
            raise TeamInvariantError('deterministic validation did not produce a candidate')
        reviews_path = os.path.join(self.run_folder, 'review_decisions.jsonl')
        prior = _read_jsonl(reviews_path)
        requested_decision = decision
        max_revision_requests = int(Config.REVIEWER_MAX_ROUNDS)
        prior_revision_requests = sum(
            1 for item in prior
            if (
                item.get('requested_decision')
                or (
                    'revise'
                    if item.get('decision') in {'revise', 'safe_fallback'}
                    else item.get('decision')
                )
            ) == 'revise'
        )
        revision_request_count = (
            prior_revision_requests + 1
            if requested_decision == 'revise'
            else prior_revision_requests
        )
        human_rejection_count = int(
            self.snapshot()['team'].get('rejection_count') or 0
        )
        review = {
            'decision': decision,
            'requested_decision': requested_decision,
            'issues': list(issues)[:100],
            'reviewer': self.role,
            'round': len(prior) + 1,
            'revision_request_count': revision_request_count,
            'human_rejection_count': human_rejection_count,
            'candidate_path': candidates[-1].name if candidates else None,
        }
        safe_fallback = False
        safe_fallback_reason: Optional[str] = None
        if decision == 'revise' and (
            prior_revision_requests >= max_revision_requests
            or human_rejection_count > 0
        ):
            reviewed_paths = sorted(
                Path(self.run_folder).glob('reviewed_report_v*.json'),
                key=lambda item: int(re.search(r'v(\d+)', item.name).group(1)),
            )
            if not reviewed_paths:
                raise TeamInvariantError('reviewed report missing for safe fallback')
            reviewed = _read_json(str(reviewed_paths[-1]), {})
            sections = list(reviewed.get('sections') or [])
            issue_types = sorted({
                str(item.get('type') or item.get('code') or '未分类问题')[:80]
                for item in review['issues'] if isinstance(item, dict)
            })
            safe_fallback_reason = (
                'post_human_rejection_cycle_limit'
                if human_rejection_count > 0
                else 'reviewer_revision_limit'
            )
            sections.append({
                'title': '审校未决事项披露',
                'goal': '达到审校退回上限后的安全降级披露',
                'content': (
                    f'本报告已达到 {max_revision_requests} 次审校退回上限，仍有 '
                    f'{len(review["issues"])} 项待关注问题。未决类别：'
                    f'{"、".join(issue_types) if issue_types else "未分类"}。'
                    '该版本仅整理已通过确定性证据门禁的内容，并保留上述限制说明。'
                ),
                'verdict': 'warning',
                'system': True,
            })
            reviewed['sections'] = sections
            candidate = assemble_report(
                self.task_id, reviewed, run_id=self.run_id, publish=False
            )
            safe_path = os.path.join(
                self.run_folder, f'report_candidate_safe_v{review["round"]}.json'
            )
            _atomic_json(safe_path, candidate)
            review['decision'] = 'safe_fallback'
            review['candidate_path'] = os.path.basename(safe_path)
            review['safe_fallback_reason'] = safe_fallback_reason
            safe_fallback = True
        _atomic_jsonl(reviews_path, [*prior, review])
        output = {
            'decision': review['decision'],
            'requested_decision': requested_decision,
            'review_round': review['round'],
            'revision_request_count': revision_request_count,
            'human_rejection_count': human_rejection_count,
            'candidate_path': review['candidate_path'],
            'issues': review['issues'],
            'safe_fallback': safe_fallback,
            'safe_fallback_reason': safe_fallback_reason,
        }
        if requested_decision == 'revise' and not safe_fallback:
            self._event(
                'report_revision_requested', output,
                idempotency_key=f'{idempotency_key}:revision',
                task_key='compliance-review',
            )
        with traced_span(
            'agentteams.review.submit',
            attributes={
                'task_id': self.task_id,
                'run_id': self.run_id,
                'team_id': self.team_id,
                'decision': review['decision'],
                'review_round': review['round'],
            },
        ):
            result = self._complete(
                'compliance-review', task, output,
                idempotency_key=idempotency_key,
            )
        if requested_decision == 'revise' and not safe_fallback:
            result['team'] = self._set_team_stage(
                'revision', idempotency_key=f'{idempotency_key}:stage:revision'
            )
        return result

    def request_publish_approval(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if self.role != 'research-lead':
            raise PermissionError('only research-lead requests publication')
        decisions = _read_jsonl(os.path.join(self.run_folder, 'review_decisions.jsonl'))
        if not decisions or decisions[-1].get('decision') not in {'pass', 'safe_fallback'}:
            raise TeamInvariantError('latest compliance review has not passed')
        candidate_name = str(decisions[-1].get('candidate_path') or '')
        if not _SAFE_FILE_RE.fullmatch(candidate_name):
            raise TeamInvariantError('invalid candidate artifact')
        candidate_path = os.path.join(self.run_folder, candidate_name)
        with traced_span(
            'agentteams.approval.request',
            attributes={
                'task_id': self.task_id,
                'run_id': self.run_id,
                'team_id': self.team_id,
                'authority': 'vue',
            },
        ):
            candidate_sha = file_sha256(candidate_path)
            artifact = AgentTeamStore.replay_artifact_registration(
                self.team_id, idempotency_key,
                artifact_type='report',
                sha256=candidate_sha,
                producer='compliance-reviewer',
                schema_version=1,
                task_id=self.task_id,
                run_id=self.run_id,
                candidate_path=candidate_name,
            )
            if artifact is not None:
                metadata = artifact.get('metadata') or {}
                degraded = bool(metadata.get('artifact_store_degraded'))
                ref = ArtifactRef(
                    artifact_type='final-report-candidate',
                    uri=str(artifact['uri']),
                    sha256=candidate_sha,
                    size_bytes=os.path.getsize(candidate_path),
                    producer='compliance-reviewer',
                    schema_version=1,
                )
            else:
                # Fail stale/new requests before the external MinIO write.
                # Exact retries have already returned above; the store still
                # repeats this CAS atomically during registration to close the
                # interval between the upload and SQLite commit.
                _assert_expected(self.snapshot(), expected_version)
                refs, degraded = self._publish_refs(
                    [candidate_path], artifact_type='final-report-candidate',
                    producer='compliance-reviewer',
                )
                if len(refs) != 1:
                    raise RuntimeError('final report candidate missing')
                ref = refs[0]
                artifact = AgentTeamStore.register_artifact(
                    self.team_id,
                    artifact_type='report',
                    uri=ref.uri,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    sha256=ref.sha256,
                    metadata={
                        'task_id': self.task_id,
                        'run_id': self.run_id,
                        'candidate_path': candidate_name,
                        'artifact_store_degraded': degraded,
                    },
                    requires_approval=True,
                    producer='compliance-reviewer',
                    schema_version=1,
                )
        self._event(
            'publish_approval_requested',
            {'artifact_id': artifact['artifact_id'], 'authority': 'vue'},
            idempotency_key=f'{idempotency_key}:event',
        )
        _set_task_progress(
            self.task_id, self.run_id, ResearchTaskStatus.REVIEWING,
            '报告已通过审校，等待 Vue 人工批准', 98,
            team_stage='awaiting_publish_approval',
        )
        return {
            'ok': True,
            'artifact': artifact,
            'artifact_ref': ref.to_dict(),
            'degraded': degraded,
            'team': self.snapshot(),
        }
