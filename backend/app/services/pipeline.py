"""Run-isolated research pipeline with optional evidence debate."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models.debate import ClaimStatus, VerdictStatus
from ..models.research_task import (
    ResearchTask,
    ResearchTaskStatus,
    task_artifact_folder,
    task_card_for_run,
)
from ..models.task_card import TaskCard
from ..utils import db as dbutil
from ..utils.llm_audit import run_cost_cny, run_cost_summary, run_token_usage
from ..utils.report_commit import report_bundle_is_committed
from ..utils.task_run_lock import task_run_lock
from .agent_logger import AgentLogger
from .analyst import Analyst
from .evidence_store import EvidenceStore
from .financial_normalizer import FinancialNormalizer, write_facts_jsonl
from .graph_ingest import ingest_task_evidence, publish_latest_graph
from .memory_service import remember_task_episode
from .report_assembler import assemble_report, publish_report
from .reviewer import Reviewer


def _set_stage(
    task: ResearchTask,
    card: TaskCard,
    run_id: Optional[str],
    status: ResearchTaskStatus,
    message: str,
    progress: int,
    **detail: Any,
) -> None:
    task.progress_detail = {
        **(task.progress_detail or {}),
        'stage': status.value,
        'analysis_mode': card.analysis_mode,
        'run_id': run_id,
        **detail,
    }
    task.set_status(status, message, progress=progress)
    if run_id and dbutil.get_task_run(run_id):
        dbutil.update_task_run(run_id, status=status.value)


def _load_evidence_index(path: str) -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError('冻结证据索引格式无效')
    return value


def _freeze_evidence(task: ResearchTask, run_id: Optional[str]) -> tuple[EvidenceStore, Dict[str, Any]]:
    """Publish the current run staging cards once and then reopen by index."""
    if not run_id:
        store = EvidenceStore(task.task_id)
        return store, {
            'schema_version': 1,
            'task_id': task.task_id,
            'run_id': None,
            'items': [
                {
                    'evidence_uid': card.evidence_uid,
                    'display_id': store.display_id(card),
                    'card': card.to_dict(),
                }
                for card in store.cards
            ],
        }

    run_folder = task.run_folder(run_id)
    index_path = os.path.join(run_folder, 'evidence_index.json')
    if not os.path.isfile(index_path):
        staging = EvidenceStore(task.task_id, run_id=run_id, allow_staging=True)
        if not staging.cards:
            raise ValueError('本次运行没有可冻结的证据，拒绝读取历史残留')
        staging.freeze_to_run(run_id)
    frozen = EvidenceStore(task.task_id, run_id=run_id)
    return frozen, _load_evidence_index(index_path)


def _dimensions(card: TaskCard) -> List[str]:
    values = card.compare_dimensions or card.focus_points or [
        '盈利质量', '现金流与偿债', '增长驱动', '经营变化',
    ]
    result: List[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= Config.DEBATE_MAX_DIMENSIONS:
            break
    return result or ['盈利质量', '现金流与偿债', '增长驱动', '经营变化']


def _claim_payload(claim: Any) -> Dict[str, Any]:
    return {
        'claim_id': claim.claim_id,
        'statement': claim.assertion,
        'evidence_uids': list(claim.evidence_uids),
        'fact_uids': list(claim.fact_uids),
        'assumptions': list(claim.assumptions),
        'status': claim.status.value,
    }


def _enrich_verdict(verdict: Dict[str, Any], orchestrator: Any) -> Dict[str, Any]:
    """Keep permanent evidence refs for report rendering without mutating verdict.json."""
    out = dict(verdict)
    claims = list(orchestrator.claims)
    by_id = {claim.claim_id: claim for claim in claims}
    accepted = [
        by_id[claim_id]
        for claim_id in verdict.get('accepted_claim_ids') or []
        if claim_id in by_id
    ]
    out['consensus_facts'] = [
        _claim_payload(claim)
        for claim in accepted if claim.fact_uids and not claim.assumptions
    ]
    out['supported_interpretations'] = [
        _claim_payload(claim)
        for claim in accepted if claim.assumptions or not claim.fact_uids
    ]
    disputed_text = set(verdict.get('unresolved_disputes') or [])
    out['unresolved_disputes'] = [
        _claim_payload(claim)
        for claim in claims
        if claim.status in {ClaimStatus.DISPUTED, ClaimStatus.REJECTED}
        or claim.assertion in disputed_text
    ] or list(disputed_text)
    out['withdrawn_claims'] = [
        _claim_payload(claim) for claim in claims if claim.status == ClaimStatus.WITHDRAWN
    ]
    out['major_challenges'] = [
        {
            'statement': challenge.argument,
            'evidence_uids': list(challenge.evidence_uids),
            'target_claim_id': challenge.target_claim_id,
            'resolution_status': challenge.resolution_status.value,
        }
        for challenge in (
            getattr(orchestrator, 'valid_challenges', None)
            or []
        )
    ]
    return out


def _budget_allows_llm(run_id: Optional[str], *, reserve_cny: float = 0.2) -> bool:
    if not run_id or not Config.TEXT_LLM_API_KEY:
        return bool(Config.TEXT_LLM_API_KEY)
    try:
        return run_cost_cny(run_id) < max(0.0, Config.LLM_COST_BUDGET_CNY - reserve_cny)
    except Exception:
        # Cost-ledger unavailability must fail closed: admitting another paid
        # call without knowing committed spend would bypass the RMB 2 redline.
        return False


def _finalize_success(
    *,
    task_id: str,
    run_id: Optional[str],
    analysis_mode: str,
    debate_status: Optional[str],
    report: Dict[str, Any],
    timings: Dict[str, float],
    total_tokens: int,
    total_cost: float,
    cost_summary: Dict[str, float],
    elapsed_seconds: float,
    logger: AgentLogger,
) -> ResearchTask:
    """Publish terminal state while holding the same barrier as DELETE.

    A task becomes deletable only after every terminal DB/memory/log write has
    completed.  DELETE therefore cannot remove the directory between
    ``set_status`` and a trailing logger write which would otherwise recreate
    the task or leave orphaned metadata.
    """

    with task_run_lock(task_id):
        live = ResearchTask.load(task_id)
        if not live:
            raise RuntimeError('task_removed_before_pipeline_finalize')
        if run_id and live.current_run_id != run_id:
            raise RuntimeError('run_is_no_longer_current')
        final_status = (
            ResearchTaskStatus.COMPLETED_PARTIAL
            if live.collect_failures or debate_status == 'fallback_direct'
            else ResearchTaskStatus.COMPLETED
        )
        live.progress_detail = {
            **(live.progress_detail or {}),
            'stage': 'completed',
            'analysis_mode': analysis_mode,
            'run_id': run_id,
            'report_ready': True,
            'report_title': report.get('title'),
            'sections': len(report.get('sections') or []),
            'mode': report.get('mode'),
            'debate_status': debate_status,
            'stage_timings': timings,
            'llm_tokens': total_tokens,
            'llm_cost_cny': total_cost,
            'llm_cost_settled_cny': cost_summary['settled_cny'],
            'llm_cost_reserved_cny': cost_summary['reserved_cny'],
            'elapsed_seconds': round(elapsed_seconds, 3),
        }
        live.error = None
        live.set_status(
            final_status,
            '报告已生成' if final_status == ResearchTaskStatus.COMPLETED else (
                '报告已生成（辩论未完成，已降级直接分析）'
                if debate_status == 'fallback_direct'
                else '报告已生成（部分采集源不可用）'
            ),
            progress=100,
        )
        db_run_id = run_id or task_id
        if dbutil.get_task_run(db_run_id):
            dbutil.update_task_run(
                db_run_id,
                status=final_status.value,
                llm_calls=len(dbutil.list_llm_call_logs(run_id)),
                llm_tokens=total_tokens,
                stage_timings_json=json.dumps(timings, ensure_ascii=False),
                collect_failures_json=json.dumps(live.collect_failures, ensure_ascii=False),
            )
            dbutil.finish_task_run(db_run_id, final_status.value)
        try:
            remember_task_episode(task_id)
        except Exception:
            pass
        logger.log('pipeline', 'completed', {
            'run_id': run_id,
            'title': report.get('title'),
            'cited': len(report.get('cited_ids') or []),
            'llm_cost_cny': total_cost,
        })
        return live


def _finalize_failure(
    *,
    task_id: str,
    run_id: Optional[str],
    run_folder: str,
    safe_error: str,
    logger: AgentLogger,
) -> Optional[ResearchTask]:
    """Finalize one failed pipeline without reviving a concurrently deleted task."""

    with task_run_lock(task_id):
        live = ResearchTask.load(task_id)
        if not live:
            return None
        if run_id and live.current_run_id != run_id:
            return live
        live.error = safe_error
        if report_bundle_is_committed(
            run_folder,
            task_id=task_id,
            run_id=run_id,
        ):
            live.progress_detail = {
                **(live.progress_detail or {}),
                'stage': 'completed_partial',
                'report_ready': True,
                'run_id': run_id,
            }
            live.set_status(
                ResearchTaskStatus.COMPLETED_PARTIAL,
                f'分析异常（已保留报告）: {safe_error}',
                progress=min(max(live.progress, 70), 99),
            )
        else:
            live.progress_detail = {
                **(live.progress_detail or {}),
                'stage': 'failed',
                'report_ready': False,
                'run_id': run_id,
            }
            live.set_status(
                ResearchTaskStatus.FAILED,
                f'分析管线失败: {safe_error}',
                progress=100,
            )
        db_run_id = run_id or task_id
        if dbutil.get_task_run(db_run_id):
            dbutil.finish_task_run(db_run_id, live.status.value)
        debate = dbutil.get_debate_run(db_run_id)
        if debate and debate.get('status') not in {'completed', 'failed'}:
            dbutil.finish_debate_run(db_run_id, 'failed', error=safe_error)
        logger.log('pipeline', 'failed', {'run_id': run_id, 'error': safe_error})
        return live


def run_analysis_pipeline(
    task_id: str,
    run_id: Optional[str] = None,
    *,
    deadline_epoch: Optional[float] = None,
) -> ResearchTask:
    task = ResearchTask.load(task_id)
    if not task or not task.task_card:
        raise ValueError('task or task_card missing')
    run_id = run_id or task.current_run_id
    if run_id and not task.has_run(run_id):
        raise ValueError('run 不存在或不属于该任务')

    logger = AgentLogger(task_id, agent='pipeline', run_id=run_id)
    card = TaskCard.from_dict(task_card_for_run(task, run_id))
    # Defensive compatibility: tracking can never enter the full debate path.
    analysis_mode = (
        'evidence_debate'
        if card.analysis_mode == 'evidence_debate' and card.deliverable in {'summary', 'compare'}
        else 'direct'
    )
    card.analysis_mode = analysis_mode
    run_folder = task_artifact_folder(task_id, run_id)
    if not run_id and not dbutil.get_task_run(task_id):
        # Legacy callers historically used task_id as run_id; keep their DB
        # record readable while all newly confirmed tasks use a real run_id.
        dbutil.insert_task_run(
            run_id=task_id,
            task_id=task_id,
            task_card=task.task_card or {},
            status='ingesting',
            user_id=task.user_id,
        )
    from ..utils.run_limits import (
        bounded_timeout,
        deadline_epoch_for_run,
        ensure_time_remaining,
        remaining_seconds,
    )

    started = time.monotonic()
    deadline_epoch = deadline_epoch or deadline_epoch_for_run(run_id or task_id)
    initial_elapsed = max(
        0.0,
        float(Config.PIPELINE_TIMEOUT_SECONDS) - remaining_seconds(deadline_epoch),
    )
    stage_started = started
    timings: Dict[str, float] = {}

    def finish_timing(stage: str) -> None:
        nonlocal stage_started
        now = time.monotonic()
        timings[stage] = round(now - stage_started, 3)
        stage_started = now

    def remaining_time() -> float:
        return remaining_seconds(deadline_epoch)

    try:
        ensure_time_remaining(deadline_epoch, stage='ingesting')
        _set_stage(task, card, run_id, ResearchTaskStatus.INGESTING, '图谱摄入中', 56)
        from ..utils.run_limits import call_with_deadline
        ingest_stats = call_with_deadline(
            lambda: ingest_task_evidence(
                task_id,
                logger=logger,
                run_id=run_id,
                deadline_epoch=deadline_epoch,
            ),
            deadline_epoch,
            reserve_seconds=5,
            stage='graph_ingest',
        )
        finish_timing('ingesting')
        task.progress_detail = {
            **(task.progress_detail or {}),
            'ingest': ingest_stats,
        }
        task.save()

        ensure_time_remaining(deadline_epoch, stage='evidence_freeze')
        frozen_store, evidence_index = _freeze_evidence(task, run_id)
        logger.log('pipeline', 'evidence_frozen', {
            'run_id': run_id,
            'cards': len(frozen_store.cards),
        })

        ensure_time_remaining(deadline_epoch, stage='normalizing')
        _set_stage(task, card, run_id, ResearchTaskStatus.NORMALIZING, '财务事实标准化中', 63)
        normalizer = FinancialNormalizer(card.time_window)
        facts = call_with_deadline(
            lambda: normalizer.normalize(evidence_index.get('items') or []),
            deadline_epoch,
            reserve_seconds=5,
            stage='financial_normalization',
        )
        ensure_time_remaining(
            deadline_epoch,
            reserve_seconds=3,
            stage='financial_facts_publish',
        )
        write_facts_jsonl(os.path.join(run_folder, 'normalized_facts.jsonl'), facts)
        finish_timing('normalizing')
        task.progress_detail = {
            **(task.progress_detail or {}),
            'normalization': {
                'fact_count': len(facts),
                'rejected_or_flagged': sum(bool(fact.quality_flags) for fact in facts),
            },
        }
        task.save()

        debate_verdict: Optional[Dict[str, Any]] = None
        debate_status: Optional[str] = None
        debate_fallback_reason: Optional[str] = None

        if analysis_mode == 'evidence_debate':
            from ..utils.llm_client import LLMClient
            from .debate_orchestrator import DebateOrchestrator

            if not dbutil.get_debate_run(run_id or ''):
                dbutil.insert_debate_run(run_id or task_id, task_id, status='pending')

            holder: Dict[str, Any] = {}

            def debate_progress(stage: str, detail: Dict[str, Any]) -> None:
                orchestrator = holder.get('orchestrator')
                claims = list(getattr(orchestrator, 'claims', []) or [])
                challenges = list(getattr(orchestrator, 'challenges', []) or [])
                audits = list(getattr(orchestrator, 'audit_scores', []) or [])
                challenge_audits = list(getattr(orchestrator, 'challenge_audits', []) or [])
                role = detail.get('role')
                round_number = int(detail.get('round') or 0)
                if stage in {'completed', 'degraded'}:
                    dbutil.update_debate_run(
                        run_id or task_id,
                        status=stage,
                        claim_count=len(claims),
                        challenge_count=len(challenges),
                        withdrawn_count=sum(c.status == ClaimStatus.WITHDRAWN for c in claims),
                        audit_failure_count=(
                            sum(not score.hard_pass for score in audits)
                            + sum(not item.get('hard_pass') for item in challenge_audits)
                        ),
                    )
                    return
                status = (
                    ResearchTaskStatus.ADJUDICATING
                    if stage == 'adjudicating' else ResearchTaskStatus.DEBATING
                )
                progress = 78 if status == ResearchTaskStatus.ADJUDICATING else (68 + round_number * 4)
                debate_detail = {
                    'status': stage,
                    'current_round': round_number,
                    'current_role': role,
                    'claim_count': len(claims),
                    'challenge_count': len(challenges),
                    'withdrawn_count': sum(c.status == ClaimStatus.WITHDRAWN for c in claims),
                    'audit_failure_count': (
                        sum(not score.hard_pass for score in audits)
                        + sum(not item.get('hard_pass') for item in challenge_audits)
                    ),
                }
                _set_stage(
                    task, card, run_id, status,
                    '证据裁决中' if status == ResearchTaskStatus.ADJUDICATING else '多视角证据辩论中',
                    progress,
                    debate=debate_detail,
                )
                dbutil.update_debate_run(
                    run_id or task_id,
                    status=stage,
                    current_round=round_number,
                    current_role=role,
                    claim_count=len(claims),
                    challenge_count=len(challenges),
                    withdrawn_count=debate_detail['withdrawn_count'],
                    audit_failure_count=debate_detail['audit_failure_count'],
                )
                logger.log('debate_progress', stage, debate_detail, agent=str(role or 'debate'))

            debate_llm = None
            if Config.TEXT_LLM_API_KEY and remaining_time() > 100:
                # Five logical calls plus at most two correction calls and one
                # transport retry each must fit before deterministic report
                # assembly gets its reserve.
                per_request_timeout = min(
                    25.0,
                    max(2.0, (remaining_time() - 75.0) / 14.0),
                )
                debate_llm = LLMClient(
                    api_key=Config.TEXT_LLM_API_KEY,
                    base_url=Config.TEXT_LLM_BASE_URL,
                    model=Config.TEXT_LLM_REASONING_MODEL,
                    provider=Config.TEXT_LLM_PROVIDER,
                    connect_timeout=min(Config.LLM_CONNECT_TIMEOUT_SECONDS, 5),
                    read_timeout=bounded_timeout(
                        deadline_epoch,
                        min(Config.LLM_READ_TIMEOUT_SECONDS, per_request_timeout),
                        reserve_seconds=70,
                        stage='debate_client',
                    ),
                    max_retries=Config.LLM_MAX_RETRIES,
                    deadline_epoch=deadline_epoch,
                    deadline_reserve_seconds=70,
                    budget_run_id=run_id or task_id,
                )
            orchestrator = DebateOrchestrator(
                run_folder,
                evidence_index,
                facts=facts,
                dimensions=_dimensions(card),
                time_window=card.time_window,
                llm=debate_llm,
                auto_create_llm=debate_llm is not None,
                progress_callback=debate_progress,
                max_corrections=Config.DEBATE_MAX_CORRECTIONS,
                degrade_on_failure=True,
                run_id=run_id or task_id,
                deadline_epoch=deadline_epoch,
                unavailable_reason=(
                    '剩余运行时间不足，辩论未执行'
                    if Config.TEXT_LLM_API_KEY and debate_llm is None else None
                ),
            )
            holder['orchestrator'] = orchestrator
            verdict = orchestrator.run()
            finish_timing('debate')
            verdict_payload = verdict.to_dict()
            counts = {
                'claim_count': len(orchestrator.claims),
                'challenge_count': len(orchestrator.challenges),
                'withdrawn_count': sum(
                    claim.status == ClaimStatus.WITHDRAWN for claim in orchestrator.claims
                ),
                'audit_failure_count': sum(
                    not score.hard_pass for score in orchestrator.audit_scores
                ) + sum(
                    not item.get('hard_pass') for item in orchestrator.challenge_audits
                ),
            }
            if verdict.status == VerdictStatus.DEGRADED:
                debate_status = 'fallback_direct'
                debate_fallback_reason = verdict.degradation_reason or '未形成有效裁决'
                dbutil.finish_debate_run(
                    run_id or task_id,
                    'failed',
                    verdict=verdict_payload,
                    error=debate_fallback_reason,
                )
            else:
                debate_status = 'completed'
                debate_verdict = _enrich_verdict(verdict_payload, orchestrator)
                dbutil.update_debate_run(run_id or task_id, **counts)
                dbutil.finish_debate_run(run_id or task_id, 'completed', verdict=verdict_payload)

        ensure_time_remaining(deadline_epoch, reserve_seconds=5, stage='analysis')

        _set_stage(task, card, run_id, ResearchTaskStatus.ANALYZING, '分析表达中', 82)
        logger.log('pipeline', 'analyzing', {'analysis_mode': analysis_mode})
        analyst = Analyst(
            task_id,
            card,
            logger=logger,
            run_id=run_id,
            deadline_epoch=deadline_epoch,
            allow_llm=(
                debate_status != 'fallback_direct'
                and _budget_allows_llm(run_id, reserve_cny=0.35)
                and remaining_time() > 75
            ),
        )
        draft = analyst.run(
            debate_verdict=debate_verdict,
            debate_status=debate_status,
            debate_fallback_reason=debate_fallback_reason,
        )
        finish_timing('analyzing')

        _set_stage(task, card, run_id, ResearchTaskStatus.REVIEWING, '审校中', 91)
        logger.log('pipeline', 'reviewing', {})
        reviewer = Reviewer(
            task_id,
            logger=logger,
            run_id=run_id,
            deadline_epoch=deadline_epoch,
            allow_llm=(
                debate_status != 'fallback_direct'
                and _budget_allows_llm(run_id, reserve_cny=0.1)
                and remaining_time() > 40
            ),
        )
        reviewed = reviewer.run(draft)
        finish_timing('reviewing')

        ensure_time_remaining(deadline_epoch, reserve_seconds=2, stage='report_assembly')
        cost_summary = (
            run_cost_summary(run_id)
            if run_id else {
                'settled_cny': 0.0,
                'reserved_cny': 0.0,
                'committed_cny': 0.0,
            }
        )
        # Publication is gated by committed cost, not only logs already
        # settled.  A crash-left or concurrently active request therefore
        # cannot disappear from the RMB 2 final redline.
        total_cost = cost_summary['committed_cny']
        total_tokens = run_token_usage(run_id) if run_id else 0
        if total_cost > Config.LLM_COST_BUDGET_CNY:
            raise RuntimeError('llm_cost_budget_exceeded')
        _set_stage(task, card, run_id, ResearchTaskStatus.ASSEMBLING, '装配报告', 97)
        # Build first without touching public artifacts.  A dependency that
        # returns after the absolute deadline can therefore never publish a
        # late report or overwrite the legacy latest alias.
        report = assemble_report(
            task_id,
            reviewed,
            run_id=run_id,
            publish=False,
        )
        ensure_time_remaining(
            deadline_epoch,
            reserve_seconds=1,
            stage='report_publish',
        )
        publish_report(
            task_id,
            report,
            run_id=run_id,
            deadline_epoch=deadline_epoch,
        )
        if run_id:
            publish_latest_graph(task_id, run_id)
        finish_timing('assembling')

        return _finalize_success(
            task_id=task_id,
            run_id=run_id,
            analysis_mode=analysis_mode,
            debate_status=debate_status,
            report=report,
            timings=timings,
            total_tokens=total_tokens,
            total_cost=total_cost,
            cost_summary=cost_summary,
            elapsed_seconds=initial_elapsed + time.monotonic() - started,
            logger=logger,
        )
    except Exception as error:
        from ..utils.llm_audit import LLMBudgetExceeded, safe_error_summary
        from ..utils.run_limits import RunDeadlineExceeded

        if isinstance(error, (RunDeadlineExceeded, TimeoutError)):
            safe_error = 'run_deadline_exceeded'
        elif isinstance(error, LLMBudgetExceeded) or 'llm_cost_budget_exceeded' in str(error):
            safe_error = 'llm_cost_budget_exceeded'
        else:
            safe_error = safe_error_summary(error)
        _finalize_failure(
            task_id=task_id,
            run_id=run_id,
            run_folder=run_folder,
            safe_error=safe_error,
            logger=logger,
        )
        raise


def run_full_pipeline(task_id: str, run_id: Optional[str] = None) -> ResearchTask:
    """Collect into this run's staging area, then freeze and analyse it."""
    from .collect_orchestrator import run_collection

    from ..utils.run_limits import deadline_epoch_for_run

    deadline_epoch = deadline_epoch_for_run(run_id or task_id)
    task = run_collection(task_id, run_id=run_id, deadline_epoch=deadline_epoch)
    if task.status == ResearchTaskStatus.FAILED:
        return task
    return run_analysis_pipeline(
        task_id,
        run_id=run_id,
        deadline_epoch=deadline_epoch,
    )
