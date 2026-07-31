"""情景推演蓝图。"""

from __future__ import annotations

import json
import os
import threading

from flask import Blueprint, jsonify, request

from ..config import Config
from ..models.research_task import ResearchTask, resolve_task_run_id
from ..services.scenario import create_scenario, interview_agents, load_scenario, run_scenario

scenario_bp = Blueprint('scenario', __name__)


@scenario_bp.route('/create', methods=['POST'])
def create():
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    hypothesis = (body.get('hypothesis') or '').strip()
    if not task_id or not hypothesis:
        return jsonify({'success': False, 'error': 'task_id 与 hypothesis 必填'}), 400
    task = ResearchTask.load(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    requested_run_id = body.get('run_id')
    resolved_run_id = resolve_task_run_id(task_id, requested_run_id)
    if requested_run_id and not resolved_run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    try:
        meta = create_scenario(
            task_id,
            hypothesis,
            body.get('from_evidence_id'),
            run_id=resolved_run_id,
        )
    except ValueError as error:
        return jsonify({'success': False, 'error': str(error)}), 409
    return jsonify({
        'success': True,
        'data': {
            'scenario_id': meta['scenario_id'],
            'run_id': meta['run_id'],
            'scenario_config': meta['config'],
            'status': meta['status'],
        },
    })


@scenario_bp.route('/<scenario_id>/start', methods=['POST'])
def start(scenario_id: str):
    body = request.get_json(silent=True) or {}
    config = body.get('scenario_config') or body.get('config') or body

    def _bg():
        try:
            run_scenario(scenario_id, config if config else None)
        except Exception:
            meta = load_scenario(scenario_id)
            if meta:
                meta['status'] = 'failed'
                from ..services.scenario.runner import save_scenario
                save_scenario(meta)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'success': True, 'data': {'scenario_id': scenario_id, 'status': 'running'}})


@scenario_bp.route('/<scenario_id>/status', methods=['GET'])
@scenario_bp.route('/<scenario_id>/run-status', methods=['GET'])
def status(scenario_id: str):
    meta = load_scenario(scenario_id)
    if not meta:
        return jsonify({'success': False, 'error': '不存在'}), 404
    # 附带最近动作
    actions = []
    path = os.path.join(Config.UPLOAD_FOLDER, 'scenarios', scenario_id, 'actions.jsonl')
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()[-30:]
        actions = [json.loads(x) for x in lines if x.strip()]
    return jsonify({
        'success': True,
        'data': {
            'status': meta.get('status'),
            'progress': meta.get('progress', 0),
            'message': meta.get('message'),
            'recent_actions': actions,
            'config': meta.get('config'),
        },
    })


@scenario_bp.route('/<scenario_id>/agent-log', methods=['GET'])
def agent_log(scenario_id: str):
    path = os.path.join(Config.UPLOAD_FOLDER, 'scenarios', scenario_id, 'actions.jsonl')
    from_line = int(request.args.get('from_line', 0))
    if not os.path.isfile(path):
        return jsonify({'success': True, 'data': {'lines': [], 'next_line': 0, 'finished': False}})
    with open(path, 'r', encoding='utf-8') as f:
        all_lines = f.readlines()
    slice_ = all_lines[from_line:]
    parsed = []
    for line in slice_:
        line = line.strip()
        if line:
            parsed.append(json.loads(line))
    meta = load_scenario(scenario_id)
    finished = (meta or {}).get('status') in ('completed', 'failed')
    return jsonify({
        'success': True,
        'data': {
            'lines': parsed,
            'next_line': from_line + len(slice_),
            'finished': finished,
        },
    })


@scenario_bp.route('/<scenario_id>/interview', methods=['POST'])
def interview(scenario_id: str):
    body = request.get_json(silent=True) or {}
    topic = body.get('topic') or '当前情景看法'
    max_agents = int(body.get('max_agents') or 3)
    answers = interview_agents(scenario_id, topic, max_agents)
    return jsonify({'success': True, 'data': {'answers': answers}})


@scenario_bp.route('/<scenario_id>/report', methods=['GET'])
def report(scenario_id: str):
    path = os.path.join(Config.UPLOAD_FOLDER, 'scenarios', scenario_id, 'report.json')
    if not os.path.isfile(path):
        return jsonify({'success': False, 'error': '报告未生成'}), 404
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify({'success': True, 'data': data})
