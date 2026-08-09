"""报告与对话 API（06 蓝图）。"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from ..models.research_task import (
    ResearchTask,
    resolve_task_run_id,
    task_artifact_folder,
    task_card_for_run,
)
from ..services.artifact_store import LocalReplayArtifactStore
from ..services.chat_agent import ChatAgent
from ..services.report_assembler import load_report
from ..utils import db as dbutil
from ..utils.report_commit import report_bundle_is_committed

report_bp = Blueprint('report', __name__)


def _resolve_report_run(task_id: str) -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    requested = request.args.get('run_id')
    run_id = resolve_task_run_id(task_id, requested)
    if requested and not run_id:
        return None, (jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404)
    return run_id, None


def _execution_mode_for_run(task_id: str, run_id: Optional[str]) -> str:
    task = ResearchTask.load(task_id)
    if not task:
        return 'agentteams'
    card = task_card_for_run(task, run_id) if run_id else task.task_card
    return str((card or {}).get('execution_mode') or 'agentteams')


def _replay_artifact_path(
    task_id: str,
    run_id: Optional[str],
    relative_path: str,
) -> Optional[str]:
    """Resolve a demo artifact exclusively through the read-only adapter."""

    if not run_id or run_id == task_id:
        return None
    if _execution_mode_for_run(task_id, run_id) != 'replay':
        return None
    try:
        return str(LocalReplayArtifactStore(task_id, run_id).resolve(relative_path))
    except FileNotFoundError:
        return None


def _load_report(task_id: str, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """调用新 assembler 的 run_id 接口，并兼容升级前的实现。"""
    if not run_id or run_id == task_id:
        return load_report(task_id)
    if _execution_mode_for_run(task_id, run_id) == 'replay':
        folder = task_artifact_folder(task_id, run_id)
        if not report_bundle_is_committed(
            folder,
            task_id=task_id,
            run_id=run_id,
        ):
            return None
        path = _replay_artifact_path(task_id, run_id, 'report.json')
        if not path:
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
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
    is_replay = _execution_mode_for_run(task_id, run_id) == 'replay'
    path = _replay_artifact_path(task_id, run_id, 'review_log.jsonl')
    if not is_replay:
        path = os.path.join(task_artifact_folder(task_id, run_id), 'review_log.jsonl')
    lines = []
    if path and os.path.isfile(path):
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
    """Compatibility tombstone: no second realtime orchestrator is allowed.

    A fresh realtime execution must be admitted by the task confirmation API,
    which creates an AgentTeams run and dispatches the Manager.  Replay runs are
    immutable and cannot be continued at all.
    """

    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    run_id = resolve_task_run_id(task_id)
    if _execution_mode_for_run(task_id, run_id) == 'replay':
        return jsonify({
            'success': False,
            'code': 'replay_read_only',
            'error': '回放任务为只读，不能重新执行分析',
        }), 409
    return jsonify({
        'success': False,
        'code': 'agentteams_rerun_requires_confirmation',
        'error': '实时分析重跑已迁移至 AgentTeams；请重新确认任务以创建新的 run',
    }), 409


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
