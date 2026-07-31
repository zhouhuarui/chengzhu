"""任务蓝图（06§1.1）。"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from ..config import Config
from ..models.research_task import (
    ResearchTask,
    ResearchTaskStatus,
    resolve_task_run_id,
    task_artifact_folder,
)
from ..models.task_card import TaskCard
from ..services.agent_logger import AgentLogger
from ..services.pipeline import run_full_pipeline
from ..services.planner import PlannerService
from ..utils import db as dbutil
from ..utils.report_commit import report_bundle_is_committed
from ..utils.run_admission import compensate_failed_run_admission
from ..utils.task_run_lock import task_run_lock

task_bp = Blueprint('task', __name__)

ACTIVE_RUN_STATUSES = {
    ResearchTaskStatus.COLLECTING,
    ResearchTaskStatus.INGESTING,
    ResearchTaskStatus.NORMALIZING,
    ResearchTaskStatus.DEBATING,
    ResearchTaskStatus.ADJUDICATING,
    ResearchTaskStatus.ANALYZING,
    ResearchTaskStatus.REVIEWING,
    ResearchTaskStatus.ASSEMBLING,
}

_SAFE_ARTIFACT_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


@task_bp.route('/create', methods=['POST'])
def create_task():
    requirement = request.form.get('requirement') or (request.json or {}).get('requirement')
    if not requirement or not str(requirement).strip():
        return jsonify({'success': False, 'error': 'requirement 必填'}), 400

    task = ResearchTask(requirement=str(requirement).strip())
    task.ensure_folder()
    task.set_status(ResearchTaskStatus.PARSING, '解析需求中', progress=2)

    # 保存上传文件（可选）
    files = request.files.getlist('files') if request.files else []
    for f in files:
        if f and f.filename:
            dest = os_path_join_files(task, f.filename)
            f.save(dest)

    from ..services.memory_service import get_prefill
    from ..services.playbook import get_rules, render_rules_for_prompt

    pref = get_prefill()
    mem_ctx = (
        f"watch_symbols={pref.get('watch_symbols')}; "
        f"default_time_window={pref.get('default_time_window')}; "
        f"report_length={pref.get('report_length')}; "
        f"deliverable_style={pref.get('deliverable_style')}"
    )
    rules_txt = render_rules_for_prompt(get_rules('planner'))
    planner = PlannerService(budget_run_id=task.task_id)
    card = planner.plan(
        task.requirement,
        user_memory_context=mem_ctx,
        playbook_rules=rules_txt,
        run_id=task.task_id,
    )
    # 启发式：无标的时用偏好预填
    if card.symbols and all(not s.code for s in card.symbols) and pref.get('watch_symbols'):
        from ..models.task_card import SymbolRef
        card.symbols = [SymbolRef(code=c, name='') for c in pref['watch_symbols'][:2]]
        card.clarifications = [c for c in card.clarifications if '未识别' not in c]
        card.clarifications.append('已按你的常用标的预填，请确认代码')
    task.set_task_card(card)
    task.set_status(ResearchTaskStatus.AWAITING_CONFIRM, '请确认任务卡', progress=5)

    return jsonify({
        'success': True,
        'data': {
            'task_id': task.task_id,
            'task_card': card.to_dict(),
            'clarifications': card.clarifications,
            'status': task.status.value,
        },
    })


def os_path_join_files(task: ResearchTask, filename: str) -> str:
    import os
    safe_name = os.path.basename(str(filename or '').replace('\x00', ''))
    if safe_name in {'', '.', '..'}:
        raise ValueError('非法文件名')
    return os.path.join(task.folder, 'files', safe_name)


@task_bp.route('/<task_id>/confirm', methods=['POST'])
def confirm_task(task_id: str):
    # Reload after acquiring the shared task lock. Without this critical
    # section two concurrent confirms can both observe a terminal status and
    # create pipelines which race on task.json/latest aliases.
    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        if task.status in ACTIVE_RUN_STATUSES:
            return jsonify({'success': False, 'error': '当前已有运行进行中，请等待完成后再创建 A/B Run'}), 409

        body = request.get_json(silent=True) or {}
        if isinstance(body.get('task_card'), dict):
            card_data = body['task_card']
        else:
            # 允许前端只修改 analysis_mode，同时保持旧的整卡请求。
            card_data = dict(task.task_card or {})
            card_data.update(body)
        card = TaskCard.from_dict(card_data)
        errors = card.validate()
        # 确认时必须有有效代码
        if any(not s.code for s in card.symbols):
            errors.append('请先确认全部标的的股票代码')
        if errors:
            return jsonify({'success': False, 'error': '; '.join(errors)}), 400

        run_started_at = dbutil.now_iso()
        run_deadline = time.time() + float(Config.PIPELINE_TIMEOUT_SECONDS)
        # Keep the confirmed card in memory until ``begin_run`` atomically
        # publishes it together with the clean runtime state.
        task.task_card = card.to_dict()
        run_id: Optional[str] = None
        try:
            run_id = task.create_run(
                task.task_card,
                deadline_epoch=run_deadline,
                publish_current=False,
            )
            dbutil.insert_task_run(
                run_id=run_id,
                task_id=task_id,
                task_card=card.to_dict(),
                status='collecting',
                user_id=task.user_id,
                started_at=run_started_at,
            )
            dbutil.assign_pending_llm_logs(task_id, run_id)
            if card.analysis_mode == 'evidence_debate':
                dbutil.insert_debate_run(run_id, task_id, status='pending')
            task.begin_run(
                run_id,
                ResearchTaskStatus.COLLECTING,
                '开始采集',
                5,
                analysis_mode=card.analysis_mode,
            )
        except Exception as error:
            safe_error = compensate_failed_run_admission(
                task_id,
                run_id,
                error,
                message='运行准入失败',
            )
            status_code = 504 if error.__class__.__name__ == 'RunDeadlineExceeded' else 500
            return jsonify({
                'success': False,
                'error': (
                    'run_deadline_exceeded'
                    if status_code == 504 else f'run_admission_failed: {safe_error}'
                ),
            }), status_code

    # 后台线程跑采集
    try:
        t = threading.Thread(target=_bg_collect, args=(task_id, run_id), daemon=True)
        t.start()
    except Exception as error:
        with task_run_lock(task_id):
            safe_error = compensate_failed_run_admission(
                task_id,
                run_id,
                error,
                message='后台运行启动失败',
            )
        return jsonify({
            'success': False,
            'error': f'run_worker_start_failed: {safe_error}',
        }), 500

    return jsonify({
        'success': True,
        'data': {'task_id': task_id, 'run_id': run_id, 'status': 'collecting'},
    })


def _bg_collect(task_id: str, run_id: Optional[str] = None) -> None:
    try:
        # 兼容尚未升级的管线；新管线通过显式 run_id 写入隔离产物。
        signature = inspect.signature(run_full_pipeline)
        accepts_run = 'run_id' in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if run_id and accepts_run:
            run_full_pipeline(task_id, run_id=run_id)
        else:
            run_full_pipeline(task_id)
    except Exception as e:
        from ..utils.llm_audit import safe_error_summary
        safe_error = safe_error_summary(e)
        # The pipeline itself normally terminalizes under this same barrier.
        # This outer guard is only for failures before that layer starts and
        # must never revive a task which DELETE already removed.
        with task_run_lock(task_id):
            row = dbutil.get_task_run(run_id) if run_id else None
            if row and row.get('status') in {
                ResearchTaskStatus.COMPLETED.value,
                ResearchTaskStatus.COMPLETED_PARTIAL.value,
                ResearchTaskStatus.FAILED.value,
            }:
                return
            task = ResearchTask.load(task_id)
            if not task:
                return
            run_failure_status = ResearchTaskStatus.FAILED.value
            if not run_id or task.current_run_id == run_id:
                task.error = safe_error
                artifact_folder = task_artifact_folder(task_id, run_id)
                has_report = report_bundle_is_committed(
                    artifact_folder,
                    task_id=task_id,
                    run_id=(run_id if run_id and run_id != task_id else None),
                )
                task.progress_detail = {
                    **(task.progress_detail or {}),
                    'stage': (
                        ResearchTaskStatus.COMPLETED_PARTIAL.value
                        if has_report else ResearchTaskStatus.FAILED.value
                    ),
                    'run_id': run_id,
                    'report_ready': has_report,
                }
                # 只有真正生成报告才允许 completed_partial。
                if has_report:
                    task.set_status(
                        ResearchTaskStatus.COMPLETED_PARTIAL,
                        f'分析异常（已保留部分报告）: {safe_error}',
                        progress=min(max(task.progress, 70), 99),
                    )
                else:
                    task.set_status(
                        ResearchTaskStatus.FAILED,
                        f'管线异常: {safe_error}',
                        progress=100,
                    )
                run_failure_status = task.status.value
            if run_id:
                if row:
                    dbutil.finish_task_run(run_id, run_failure_status)
                debate = dbutil.get_debate_run(run_id)
                if debate and debate.get('status') not in ('completed', 'failed'):
                    dbutil.finish_debate_run(run_id, 'failed', error=safe_error)


def _debate_progress(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        'status': row.get('status'),
        'current_round': row.get('current_round') or 0,
        'current_role': row.get('current_role'),
        'claim_count': row.get('claim_count') or 0,
        'challenge_count': row.get('challenge_count') or 0,
        'withdrawn_count': row.get('withdrawn_count') or 0,
        'audit_failure_count': row.get('audit_failure_count') or 0,
    }


def _read_json_object(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _decode_json_field(value: Any, fallback: Any) -> Any:
    if value in (None, ''):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return decoded


def _historical_run_status(task: ResearchTask, run_id: str) -> Dict[str, Any]:
    """Build immutable status from one run's rows and published artefacts.

    Task-level fields describe the newest execution and must never be reused
    for an older run.  ``task_run`` intentionally has no mutable UI progress,
    so historical progress is a coarse, deterministic stage percentage.
    """

    row = dbutil.get_task_run(run_id) or {}
    debate_row = dbutil.get_debate_run(run_id)
    folder = task_artifact_folder(task.task_id, run_id)
    report_path = os.path.join(folder, 'report.json')
    report_ready = report_bundle_is_committed(
        folder,
        task_id=task.task_id,
        run_id=(run_id if run_id != task.task_id else None),
    )
    report = _read_json_object(report_path) if report_ready else {}
    run_meta = _read_json_object(os.path.join(folder, 'run.json'))

    status = str(row.get('status') or '').strip().lower()
    if not status:
        status = 'completed' if report_ready else 'unknown'
    # A published run report is a terminal commit marker.  It takes precedence
    # over a stale non-terminal DB value left by a process interruption after
    # report publication, but never masks an explicitly recorded failure.
    if report_ready and status not in {'completed', 'completed_partial', 'failed'}:
        status = 'completed'

    progress_by_status = {
        'pending': 0,
        'created': 0,
        'parsing': 2,
        'awaiting_confirm': 5,
        'collecting': 20,
        'ingesting': 45,
        'normalizing': 60,
        'debating': 70,
        'adjudicating': 80,
        'analyzing': 85,
        'reviewing': 90,
        'assembling': 95,
        'completed': 100,
        'completed_partial': 100,
        'failed': 100,
        'unknown': 0,
    }
    progress = progress_by_status.get(status, 0)
    artifacts = {
        'report': report_ready,
        'evidence_snapshot': os.path.isfile(os.path.join(folder, 'evidence_index.json')),
        'normalized_facts': os.path.isfile(os.path.join(folder, 'normalized_facts.jsonl')),
        'debate_verdict': os.path.isfile(os.path.join(folder, 'debate', 'verdict.json')),
        'graph': os.path.isfile(os.path.join(folder, 'graph.json')),
    }
    progress_detail: Dict[str, Any] = {
        'stage': status,
        'historical': True,
        'artifacts': artifacts,
    }
    stage_timings = _decode_json_field(row.get('stage_timings_json'), {})
    if isinstance(stage_timings, dict) and stage_timings:
        progress_detail['stage_timings'] = stage_timings
    if report.get('title'):
        progress_detail['report_title'] = report['title']
    if report.get('debate_status'):
        progress_detail['debate_status'] = report['debate_status']
    debate = _debate_progress(debate_row)
    if debate is not None:
        progress_detail['debate'] = debate

    collect_failures = _decode_json_field(row.get('collect_failures_json'), [])
    if not isinstance(collect_failures, list):
        collect_failures = []
    error = None
    if debate_row and debate_row.get('error'):
        error = str(debate_row['error'])
    elif run_meta.get('error'):
        error = str(run_meta['error'])

    if status == 'completed':
        message = '历史运行已完成'
    elif status == 'completed_partial':
        message = '历史运行部分完成'
    elif status == 'failed':
        message = '历史运行失败'
    else:
        message = f'历史运行状态：{status}'
    if report.get('title') and status in {'completed', 'completed_partial'}:
        message += f'：{report["title"]}'

    return {
        'status': status,
        'progress': progress,
        'progress_detail': progress_detail,
        'message': message,
        'error': error,
        'collect_failures': collect_failures,
        'run_id': run_id,
        'is_current': False,
    }


@task_bp.route('/<task_id>/status', methods=['GET'])
def task_status(task_id: str):
    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    requested_run = request.args.get('run_id')
    resolved_run: Optional[str] = None
    if requested_run:
        resolved_run = resolve_task_run_id(task_id, requested_run)
        if not resolved_run:
            return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
        current_identity = task.current_run_id or task.task_id
        if resolved_run != current_identity:
            return jsonify({
                'success': True,
                'data': _historical_run_status(task, resolved_run),
            })

    progress_detail = dict(task.progress_detail or {})
    if task.current_run_id:
        debate = dbutil.get_debate_run(task.current_run_id)
        debate_progress = _debate_progress(debate)
        if debate_progress is not None:
            progress_detail['debate'] = debate_progress
    return jsonify({
        'success': True,
        'data': {
            'status': task.status.value,
            'progress': task.progress,
            'progress_detail': progress_detail,
            'message': task.message,
            'error': task.error,
            'collect_failures': task.collect_failures,
            'run_id': resolved_run or task.current_run_id,
            'is_current': True,
        },
    })


@task_bp.route('/<task_id>/agent-log', methods=['GET'])
def agent_log(task_id: str):
    from_line = int(request.args.get('from_line', 0))
    requested_run = request.args.get('run_id')
    run_id = resolve_task_run_id(task_id, requested_run)
    if requested_run and not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    data = AgentLogger.read_from_line(
        task_id,
        from_line,
        run_id=run_id if run_id and run_id != task_id else None,
    )
    return jsonify({'success': True, 'data': data})


@task_bp.route('/<task_id>/evidence', methods=['GET'])
def evidence(task_id: str):
    from ..services.evidence_store import EvidenceStore

    requested_run = request.args.get('run_id')
    resolved_run = resolve_task_run_id(task_id, requested_run)
    if requested_run and not resolved_run:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    store = EvidenceStore(
        task_id,
        run_id=resolved_run,
    )
    if resolved_run and resolved_run != task_id and not store.is_frozen:
        return jsonify({
            'success': True,
            'data': {
                'items': [],
                'total': 0,
                'run_id': resolved_run,
                'status': 'staging',
                'published': False,
            },
        }), 202
    source_type = request.args.get('source_type')
    cards = store.cards
    if source_type:
        cards = [card for card in cards if card.source_type == source_type]
    items = []
    for card in cards:
        item = card.to_dict()
        item['display_id'] = store.display_id(card)
        items.append(item)
    return jsonify({
        'success': True,
        'data': {
            'items': items,
            'total': len(items),
            'run_id': store.run_id or task_id,
            'status': 'frozen' if store.is_frozen else 'legacy',
            'published': store.is_frozen or not resolved_run or resolved_run == task_id,
        },
    })


def _decode_run_row(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row)
    for source, target, fallback in (
        ('task_card_json', 'task_card', {}),
        ('stage_timings_json', 'stage_timings', {}),
        ('collect_failures_json', 'collect_failures', []),
    ):
        raw = result.pop(source, None)
        try:
            result[target] = json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            result[target] = fallback
    if isinstance(result.get('task_card'), dict):
        result['task_card'].setdefault('analysis_mode', 'direct')
        result['analysis_mode'] = result['task_card'].get('analysis_mode', 'direct')
    result['created_at'] = result.get('started_at')
    return result


@task_bp.route('/<task_id>/runs', methods=['GET'])
def task_runs(task_id: str):
    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'limit 必须为整数'}), 400
    rows = {_row['run_id']: _decode_run_row(_row) for _row in dbutil.list_task_runs(task_id, limit)}
    legacy_report = os.path.join(task.folder, 'report.json')
    if report_bundle_is_committed(task.folder, task_id=task_id) and task_id not in rows:
        try:
            with open(legacy_report, 'r', encoding='utf-8') as f:
                legacy_meta = json.load(f)
        except (OSError, ValueError):
            legacy_meta = {}
        aliased_run = legacy_meta.get('run_id')
        # 新报告会原子更新根目录 latest 副本，已在真实 run 列表中时不重复展示。
        if not aliased_run or aliased_run not in rows:
            legacy_mode = legacy_meta.get('analysis_mode') or 'direct'
            rows[task_id] = {
                'run_id': task_id,
                'task_id': task_id,
                'task_card': {**(task.task_card or {}), 'analysis_mode': legacy_mode},
                'analysis_mode': legacy_mode,
                'status': 'completed',
                'started_at': legacy_meta.get('created_at') or task.created_at,
                'created_at': legacy_meta.get('created_at') or task.created_at,
                'legacy': True,
            }
    if os.path.isdir(task.runs_folder):
        for run_id in sorted(os.listdir(task.runs_folder), reverse=True):
            folder = os.path.join(task.runs_folder, run_id)
            if not os.path.isdir(folder) or run_id in rows:
                continue
            meta: Dict[str, Any] = {}
            meta_path = os.path.join(folder, 'run.json')
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    meta = {}
            rows[run_id] = {
                'run_id': run_id,
                'task_id': task_id,
                'task_card': meta.get('task_card') or {},
                'analysis_mode': (meta.get('task_card') or {}).get('analysis_mode', 'direct'),
                'status': 'unknown',
                'started_at': meta.get('created_at'),
                'created_at': meta.get('created_at'),
            }
    data = sorted(
        rows.values(),
        key=lambda item: (item.get('started_at') or '', item.get('run_id') or ''),
        reverse=True,
    )[:limit]
    published_run_id = resolve_task_run_id(task_id)
    for item in data:
        item['is_current'] = item.get('run_id') == task.current_run_id
        item_run_id = item.get('run_id')
        report_folder = task_artifact_folder(task_id, item_run_id)
        item['report_ready'] = report_bundle_is_committed(
            report_folder,
            task_id=task_id,
            run_id=(item_run_id if item_run_id != task_id else None),
        )
        item['is_latest_published'] = bool(
            item['report_ready'] and item_run_id == published_run_id
        )
    return jsonify({'success': True, 'data': data})


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not os.path.isfile(path):
        return items
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                value = json.loads(line)
                if isinstance(value, dict):
                    items.append(value)
    return items


def _decorate_evidence_refs(value: Any, display_map: Dict[str, str]) -> Any:
    """API 显示层把永久 UID 转为 run 内 E 角标，不修改落盘契约。"""
    if isinstance(value, list):
        return [_decorate_evidence_refs(item, display_map) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _decorate_evidence_refs(item, display_map)
        for key, item in value.items()
    }
    refs = result.get('evidence_uids') or result.get('evidence_ids')
    if refs and not result.get('evidence_refs'):
        refs = refs if isinstance(refs, list) else [refs]
        result['evidence_refs'] = [display_map.get(str(ref), str(ref)) for ref in refs]
    return result


def _enrich_api_verdict(
    verdict: Optional[Dict[str, Any]],
    claims: List[Dict[str, Any]],
    challenges: List[Dict[str, Any]],
    challenge_audit: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Expose clickable accepted-claim provenance without changing verdict.json."""

    if not isinstance(verdict, dict):
        return verdict
    result = dict(verdict)
    by_id = {str(item.get('claim_id')): item for item in claims}
    accepted = [
        by_id[claim_id]
        for claim_id in (result.get('accepted_claim_ids') or [])
        if claim_id in by_id
    ]

    def item(claim: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'claim_id': claim.get('claim_id'),
            'statement': claim.get('assertion') or claim.get('statement'),
            'evidence_uids': claim.get('evidence_uids') or [],
            'evidence_refs': claim.get('evidence_refs') or [],
            'fact_uids': claim.get('fact_uids') or [],
            'assumptions': claim.get('assumptions') or [],
            'status': claim.get('status'),
        }

    result['consensus_facts'] = [
        item(claim) for claim in accepted
        if claim.get('fact_uids') and not claim.get('assumptions')
    ]
    result['supported_interpretations'] = [
        item(claim) for claim in accepted
        if claim.get('assumptions') or not claim.get('fact_uids')
    ]
    valid_challenges = {
        audit.get('challenge_id') for audit in challenge_audit if audit.get('hard_pass')
    }
    result['major_challenges'] = [
        {
            'challenge_id': challenge.get('challenge_id'),
            'target_claim_id': challenge.get('target_claim_id'),
            'statement': challenge.get('argument'),
            'evidence_uids': challenge.get('evidence_uids') or [],
            'evidence_refs': challenge.get('evidence_refs') or [],
            'resolution_status': challenge.get('resolution_status'),
        }
        for challenge in challenges
        if challenge.get('challenge_id') in valid_challenges
    ]
    return result


@task_bp.route('/<task_id>/debate', methods=['GET'])
def task_debate(task_id: str):
    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    requested_run = request.args.get('run_id')
    run_id = resolve_task_run_id(task_id, requested_run)
    if not run_id:
        return jsonify({'success': False, 'error': 'run 不存在'}), 404
    folder = task_artifact_folder(task_id, run_id)
    debate_folder = os.path.join(folder, 'debate')
    metadata = dbutil.get_debate_run(run_id)
    verdict: Optional[Dict[str, Any]] = None
    verdict_path = os.path.join(debate_folder, 'verdict.json')
    if os.path.isfile(verdict_path):
        with open(verdict_path, 'r', encoding='utf-8') as f:
            verdict = json.load(f)
    elif metadata and metadata.get('verdict_json'):
        try:
            verdict = json.loads(metadata['verdict_json'])
        except (TypeError, ValueError):
            verdict = None
    progress = {
        'status': metadata.get('status') if metadata else None,
        'current_round': metadata.get('current_round') if metadata else 0,
        'current_role': metadata.get('current_role') if metadata else None,
        'claim_count': metadata.get('claim_count') if metadata else 0,
        'challenge_count': metadata.get('challenge_count') if metadata else 0,
        'withdrawn_count': metadata.get('withdrawn_count') if metadata else 0,
        'audit_failure_count': metadata.get('audit_failure_count') if metadata else 0,
    }
    from ..services.evidence_store import EvidenceStore
    evidence_store = EvidenceStore(task_id, run_id=run_id)
    display_map = {
        str(card.evidence_uid): evidence_store.display_id(card)
        for card in evidence_store.cards if card.evidence_uid
    }
    claims = _decorate_evidence_refs(
        _read_jsonl(os.path.join(debate_folder, 'claims.jsonl')),
        display_map,
    )
    challenges = _decorate_evidence_refs(
        _read_jsonl(os.path.join(debate_folder, 'challenges.jsonl')),
        display_map,
    )
    verdict = _decorate_evidence_refs(verdict, display_map)
    challenge_audit = _read_jsonl(os.path.join(debate_folder, 'challenge_audit.jsonl'))
    verdict = _enrich_api_verdict(verdict, claims, challenges, challenge_audit)
    return jsonify({
        'success': True,
        'data': {
            'run_id': run_id,
            'status': progress['status'],
            'current_round': progress['current_round'],
            'current_role': progress['current_role'],
            'progress': progress,
            'metadata': metadata,
            'evidence_display_map': display_map,
            'claims': claims,
            'challenges': challenges,
            'audit': _read_jsonl(os.path.join(debate_folder, 'audit.jsonl')),
            'challenge_audit': challenge_audit,
            'verdict': verdict,
        },
    })


@task_bp.route('/<task_id>', methods=['GET'])
def get_task(task_id: str):
    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    return jsonify({'success': True, 'data': task.to_dict()})


@task_bp.route('/list', methods=['GET'])
def list_tasks():
    limit = int(request.args.get('limit', 20))
    tasks = ResearchTask.list_tasks(limit=limit)
    return jsonify({
        'success': True,
        'data': [t.to_dict() for t in tasks],
    })


@task_bp.route('/<task_id>/graph', methods=['GET'])
def task_graph(task_id: str):
    from ..utils.graph_client import get_graph_client, project_group_id

    requested_run = request.args.get('run_id')
    run_id = resolve_task_run_id(task_id, requested_run)
    if requested_run and not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    folder = task_artifact_folder(task_id, run_id) if run_id else os.path.join(
        Config.UPLOAD_FOLDER, 'tasks', task_id,
    )
    path = os.path.join(folder, 'graph.json')
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({'success': True, 'data': data})
    if run_id and run_id != task_id:
        # Never fall back to the cumulative task graph for a run-scoped read.
        return jsonify({
            'success': True,
            'data': {
                'run_id': run_id,
                'nodes': [],
                'edges': [],
                'statistics': {'nodes': 0, 'edges': 0, 'backend': 'run_snapshot_pending'},
            },
        })
    client = get_graph_client(project_group_id(task_id))
    return jsonify({'success': True, 'data': client.visualization_payload()})


def _owned_artifact_directory(
    parent: str,
    identifier: str,
    *,
    expected_prefix: str,
) -> str:
    """Resolve one direct, non-symlink-owned child without path traversal."""

    value = str(identifier or '')
    if (
        not value.startswith(expected_prefix)
        or not _SAFE_ARTIFACT_ID_RE.fullmatch(value)
    ):
        raise ValueError('unsafe_task_artifact_identifier')
    safe_parent = os.path.realpath(parent)
    candidate = os.path.join(safe_parent, value)
    if os.path.dirname(candidate) != safe_parent:
        raise ValueError('unsafe_task_artifact_path')
    if os.path.islink(candidate):
        raise ValueError('task_artifact_symlink_rejected')
    return candidate


def _remove_owned_directory(path: str) -> None:
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        raise OSError('owned_artifact_is_not_a_directory')
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise OSError('owned_artifact_delete_incomplete')


def _stage_owned_directory(path: str, transaction_id: str) -> Optional[str]:
    """Atomically move one owned directory out of the live namespace."""

    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isdir(path):
        raise OSError('owned_artifact_is_not_a_directory')
    parent = os.path.dirname(path)
    staged = os.path.join(
        parent,
        f'.{os.path.basename(path)}.deleting-{transaction_id}',
    )
    if os.path.lexists(staged):
        raise OSError('delete_staging_path_exists')
    os.replace(path, staged)
    return staged


def _restore_staged_directories(staged: Dict[str, str]) -> None:
    """Restore live paths after a DB transaction rolls back."""

    errors: List[Exception] = []
    for original, temporary in reversed(list(staged.items())):
        if not os.path.lexists(temporary):
            continue
        if os.path.lexists(original):
            errors.append(OSError('delete_restore_target_exists'))
            continue
        try:
            os.replace(temporary, original)
        except OSError as error:
            errors.append(error)
    if errors:
        raise OSError('delete_staging_restore_failed') from errors[0]


def _delete_filesystem_preflight(parent: str) -> None:
    """Fail before the DB transaction when directory removal is unavailable."""

    os.makedirs(parent, exist_ok=True)
    probe = tempfile.mkdtemp(prefix='.task-delete-probe-', dir=parent)
    try:
        _remove_owned_directory(probe)
    finally:
        # A test-injected or platform-specific helper failure must not leave
        # the probe behind.  The probe is known empty and narrowly scoped.
        if os.path.isdir(probe) and not os.path.islink(probe):
            try:
                os.rmdir(probe)
            except OSError:
                pass


@task_bp.route('/<task_id>', methods=['DELETE'])
def delete_task(task_id: str):
    from ..utils.graph_client import get_graph_client, project_group_id

    try:
        task_folder = _owned_artifact_directory(
            os.path.join(Config.UPLOAD_FOLDER, 'tasks'),
            task_id,
            expected_prefix='task_',
        )
    except ValueError:
        return jsonify({'success': False, 'error': '非法 task_id'}), 400

    # Serialize deletion with confirm/rerun admission.  Re-read under the lock
    # so a run cannot become active between the status check and cleanup.
    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        active_values = {status.value for status in ACTIVE_RUN_STATUSES}
        # Compatibility with pre-state-machine rows.
        active_values.update({'pending', 'running'})
        if (
            task.status in ACTIVE_RUN_STATUSES
            or dbutil.has_task_run_with_status(task_id, active_values)
        ):
            return jsonify({'success': False, 'error': '运行进行中，暂不能删除任务'}), 409

        manifest = dbutil.get_task_cleanup_manifest(task_id)
        try:
            scenario_paths = [
                _owned_artifact_directory(
                    os.path.join(Config.UPLOAD_FOLDER, 'scenarios'),
                    scenario_id,
                    expected_prefix='scen_',
                )
                for scenario_id in manifest['scenario_ids']
            ]
            brief_paths = [
                _owned_artifact_directory(
                    os.path.join(Config.UPLOAD_FOLDER, 'briefs'),
                    sub_id,
                    expected_prefix='sub_',
                )
                for sub_id in manifest['tracking_sub_ids']
            ]
        except ValueError:
            return jsonify({
                'success': False,
                'error': '任务关联产物标识不安全，已拒绝删除',
            }), 409

        try:
            _delete_filesystem_preflight(Config.UPLOAD_FOLDER)
        except OSError:
            return jsonify({
                'success': False,
                'error': '任务产物删除失败，数据库记录未清理',
            }), 500

        transaction_id = uuid.uuid4().hex
        staged: Dict[str, str] = {}

        def stage_filesystem() -> None:
            # Renames are same-filesystem and atomic.  They execute while the
            # SQLite IMMEDIATE transaction holds the manifest write barrier.
            for original in [*scenario_paths, *brief_paths, task_folder]:
                temporary = _stage_owned_directory(original, transaction_id)
                if temporary:
                    staged[original] = temporary

        try:
            dbutil.delete_task_runs(
                task_id,
                expected_manifest=manifest,
                stage_filesystem=stage_filesystem,
            )
        except Exception as error:
            try:
                _restore_staged_directories(staged)
            except OSError:
                return jsonify({
                    'success': False,
                    'error': '任务删除回滚失败，需要人工恢复暂存产物',
                }), 500
            status = 409 if str(error) == 'task_related_state_changed_during_delete' else 500
            return jsonify({
                'success': False,
                'error': (
                    '删除期间任务关联状态发生变化，请重试'
                    if status == 409 else '任务数据库清理失败'
                ),
            }), status

        # DB ownership is gone and canonical paths are no longer reachable.
        # Physical purge is best-effort but explicitly reported if a platform
        # leaves a hidden staged directory for later administrative cleanup.
        cleanup_pending = 0
        for temporary in staged.values():
            try:
                _remove_owned_directory(temporary)
            except OSError:
                cleanup_pending += 1

        try:
            get_graph_client(project_group_id(task_id)).delete_group()
        except Exception:
            # The local/remote graph is a derived cache.  Its failure does not
            # claim that owned run files survived, and a later cache cleanup can
            # safely retry by group id.
            pass
        response_data = {
            'deleted_scenarios': len(manifest['scenario_ids']),
            'deleted_tracking_subscriptions': len(manifest['tracking_sub_ids']),
        }
        if cleanup_pending:
            response_data['cleanup_pending'] = cleanup_pending
        return jsonify({'success': True, 'data': response_data}), (
            202 if cleanup_pending else 200
        )
