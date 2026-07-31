"""报告与对话 API（06 蓝图）。"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from ..config import Config
from ..models.research_task import (
    ResearchTask,
    ResearchTaskStatus,
    resolve_task_run_id,
    task_artifact_folder,
    task_card_for_run,
)
from ..services.chat_agent import ChatAgent
from ..services.pipeline import run_analysis_pipeline
from ..services.report_assembler import load_report
from ..utils import db as dbutil
from ..utils.report_commit import report_bundle_is_committed
from ..utils.run_admission import compensate_failed_run_admission
from ..utils.task_run_lock import task_run_lock

report_bp = Blueprint('report', __name__)

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


def _resolve_report_run(task_id: str) -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    requested = request.args.get('run_id')
    run_id = resolve_task_run_id(task_id, requested)
    if requested and not run_id:
        return None, (jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404)
    return run_id, None


def _load_report(task_id: str, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """调用新 assembler 的 run_id 接口，并兼容升级前的实现。"""
    if not run_id or run_id == task_id:
        return load_report(task_id)
    signature = inspect.signature(load_report)
    if 'run_id' in signature.parameters:
        return load_report(task_id, run_id=run_id)
    path = os.path.join(task_artifact_folder(task_id, run_id), 'report.json')
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


@report_bp.route('/<task_id>', methods=['GET'])
def get_report(task_id: str):
    from ..services.evidence_store import EvidenceStore

    run_id, error_response = _resolve_report_run(task_id)
    if error_response:
        return error_response
    report = _load_report(task_id, run_id)
    if not report:
        return jsonify({'success': False, 'error': '报告尚未生成'}), 404
    fmt = request.args.get('format', 'json')
    if fmt == 'markdown':
        return report.get('markdown', ''), 200, {'Content-Type': 'text/markdown; charset=utf-8'}
    store = EvidenceStore(task_id, run_id=run_id)
    report = dict(report)
    report['run_id'] = run_id or report.get('run_id') or task_id
    report['sources'] = store.sources_index()
    report['outline'] = {
        'title': report.get('title'),
        'sections': [{'title': s.get('title')} for s in report.get('sections') or []],
    }
    return jsonify({'success': True, 'data': report})


@report_bp.route('/<task_id>/markdown', methods=['GET'])
def get_markdown(task_id: str):
    from ..services.pdf_visuals import chart_to_markdown_table, extract_chart_blocks
    import re

    run_id, error_response = _resolve_report_run(task_id)
    if error_response:
        return error_response
    report = _load_report(task_id, run_id)
    if not report:
        return jsonify({'success': False, 'error': '报告尚未生成'}), 404
    md = report.get('markdown') or ''
    # 导出时 chart 块降级为表格
    for chart in extract_chart_blocks(md):
        table = chart_to_markdown_table(chart)
        md = re.sub(r'```chart\s*\{.*?\}\s*```', table, md, count=1, flags=re.DOTALL)
    return md, 200, {'Content-Type': 'text/markdown; charset=utf-8'}


@report_bp.route('/<task_id>/review-log', methods=['GET'])
def review_log(task_id: str):
    run_id, error_response = _resolve_report_run(task_id)
    if error_response:
        return error_response
    path = os.path.join(task_artifact_folder(task_id, run_id), 'review_log.jsonl')
    lines = []
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
    return jsonify({'success': True, 'data': lines})


@report_bp.route('/chat', methods=['POST'])
def chat_alt():
    """兼容 06 文档：POST /api/report/chat {task_id, message}。"""
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    question = (body.get('message') or body.get('question') or '').strip()
    if not task_id or not question:
        return jsonify({'success': False, 'error': 'task_id 与 message 必填'}), 400
    requested_run_id = body.get('run_id')
    run_id = resolve_task_run_id(task_id, requested_run_id)
    if requested_run_id and not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    agent = ChatAgent(task_id, run_id=run_id)
    result = agent.ask(question, history=body.get('chat_history') or body.get('history') or [])
    return jsonify({
        'success': True,
        'data': {
            'response': result.get('answer'),
            'tool_calls': [],
            'correction_detected': result.get('correction_flag'),
            **result,
        },
    })


@report_bp.route('/<task_id>/rerun-analysis', methods=['POST'])
def rerun_analysis(task_id: str):
    # Confirm and rerun share one admission lock. Re-read task state under the
    # lock so a concurrent request cannot create a second active pipeline.
    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        if task.status in ACTIVE_RUN_STATUSES:
            return jsonify({'success': False, 'error': '当前已有运行进行中'}), 409
        if task.status == ResearchTaskStatus.FAILED and not task.task_card:
            return jsonify({'success': False, 'error': '无法重跑'}), 400

        # 重跑分析只能复用 latest 已原子发布且非空的冻结证据；staging
        # cards 不可见，也绝不能被静默转换成空快照。
        from ..services.evidence_store import EvidenceStore
        source_run_id = resolve_task_run_id(task_id)
        source_store = EvidenceStore(
            task_id,
            run_id=source_run_id,
        )
        if not source_store.is_frozen or not source_store.cards:
            return jsonify({
                'success': False,
                'error': '当前没有可重跑的已冻结非空证据快照',
            }), 409

        run_started_at = dbutil.now_iso()
        run_deadline = time.time() + float(Config.PIPELINE_TIMEOUT_SECONDS)
        card = dict(task_card_for_run(task, source_run_id) or {})
        # Publish the source card and reset runtime fields in the same
        # ``begin_run`` task.json write after admission succeeds.
        task.task_card = card
        run_id: Optional[str] = None
        try:
            run_id = task.create_run(
                card,
                deadline_epoch=run_deadline,
                publish_current=False,
            )
            source_store.freeze_to_run(run_id)
            dbutil.insert_task_run(
                run_id=run_id,
                task_id=task_id,
                task_card=card,
                status='analyzing',
                user_id=task.user_id,
                started_at=run_started_at,
            )
            if card.get('analysis_mode') == 'evidence_debate':
                dbutil.insert_debate_run(run_id, task_id, status='pending')
            task.begin_run(
                run_id,
                ResearchTaskStatus.ANALYZING,
                '重新分析中',
                65,
                analysis_mode=str(card.get('analysis_mode') or 'direct'),
            )
        except Exception as error:
            safe_error = compensate_failed_run_admission(
                task_id,
                run_id,
                error,
                message='重跑准入失败',
            )
            status_code = 504 if error.__class__.__name__ == 'RunDeadlineExceeded' else 500
            return jsonify({
                'success': False,
                'error': (
                    'run_deadline_exceeded'
                    if status_code == 504 else f'run_admission_failed: {safe_error}'
                ),
            }), status_code

    def _bg():
        try:
            signature = inspect.signature(run_analysis_pipeline)
            if 'run_id' in signature.parameters:
                run_analysis_pipeline(task_id, run_id=run_id)
            else:
                run_analysis_pipeline(task_id)
        except Exception as exc:
            from ..utils.llm_audit import safe_error_summary
            with task_run_lock(task_id):
                row = dbutil.get_task_run(run_id) or {}
                if row.get('status') in {
                    ResearchTaskStatus.COMPLETED.value,
                    ResearchTaskStatus.COMPLETED_PARTIAL.value,
                    ResearchTaskStatus.FAILED.value,
                }:
                    return
                failed = ResearchTask.load(task_id)
                if not failed:
                    return
                safe_error = (
                    str(failed.error)
                    if failed.error else safe_error_summary(exc)
                )
                folder = task_artifact_folder(task_id, run_id)
                has_report = report_bundle_is_committed(
                    folder,
                    task_id=task_id,
                    run_id=run_id,
                )
                run_failure_status = ResearchTaskStatus.FAILED.value
                if failed.current_run_id == run_id:
                    failed.error = safe_error
                    if has_report:
                        failed.progress_detail = {
                            **(failed.progress_detail or {}),
                            'stage': ResearchTaskStatus.COMPLETED_PARTIAL.value,
                            'run_id': run_id,
                            'report_ready': True,
                        }
                        failed.set_status(
                            ResearchTaskStatus.COMPLETED_PARTIAL,
                            f'重跑异常（已保留报告）: {safe_error}',
                            progress=min(max(failed.progress, 70), 99),
                        )
                    else:
                        failed.progress_detail = {
                            **(failed.progress_detail or {}),
                            'stage': ResearchTaskStatus.FAILED.value,
                            'run_id': run_id,
                            'report_ready': False,
                        }
                        failed.set_status(
                            ResearchTaskStatus.FAILED,
                            f'重跑失败: {safe_error}',
                            progress=100,
                        )
                    run_failure_status = failed.status.value
                if row:
                    dbutil.finish_task_run(run_id, run_failure_status)
                debate = dbutil.get_debate_run(run_id)
                if debate and debate.get('status') not in {'completed', 'failed'}:
                    dbutil.finish_debate_run(run_id, 'failed', error=safe_error)

    try:
        threading.Thread(target=_bg, daemon=True).start()
    except Exception as error:
        with task_run_lock(task_id):
            safe_error = compensate_failed_run_admission(
                task_id,
                run_id,
                error,
                message='重跑后台启动失败',
            )
        return jsonify({
            'success': False,
            'error': f'run_worker_start_failed: {safe_error}',
        }), 500
    return jsonify({
        'success': True,
        'data': {'task_id': task_id, 'run_id': run_id, 'status': 'analyzing'},
    })


@report_bp.route('/<task_id>/chat', methods=['POST'])
def chat(task_id: str):
    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    if not question:
        return jsonify({'success': False, 'error': 'question 必填'}), 400
    history = body.get('history') or []
    requested_run_id = body.get('run_id')
    run_id = resolve_task_run_id(task_id, requested_run_id)
    if requested_run_id and not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    agent = ChatAgent(task_id, run_id=run_id)
    result = agent.ask(question, history=history)

    if result.get('correction_flag'):
        try:
            run_id = run_id or task_id
            dbutil.insert_feedback(
                run_id=run_id,
                kind='correction',
                comment=question,
            )
        except Exception:
            pass

    return jsonify({'success': True, 'data': result})
