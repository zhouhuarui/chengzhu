"""反馈蓝图。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..models.research_task import resolve_task_run_id
from ..utils import db as dbutil
from ..services.reflection import reflect_async
from ..services.memory_service import remember_task_episode

feedback_bp = Blueprint('feedback', __name__)


def _feedback_run_id(task_id: str, requested: str | None = None) -> str | None:
    if requested:
        return resolve_task_run_id(task_id, requested)
    return resolve_task_run_id(task_id) or task_id


@feedback_bp.route('/section', methods=['POST'])
def section_feedback():
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    if not task_id or body.get('vote') not in ('up', 'down'):
        return jsonify({'success': False, 'error': 'task_id 与 vote(up|down) 必填'}), 400
    run_id = _feedback_run_id(task_id, body.get('run_id'))
    if not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    fid = dbutil.insert_feedback(
        run_id=run_id,
        kind='section_vote',
        section_index=body.get('section_index'),
        vote=body.get('vote'),
        comment=body.get('comment'),
    )
    reflect_async(run_id)
    remember_task_episode(task_id)
    return jsonify({'success': True, 'data': {'id': fid, 'run_id': run_id}})


@feedback_bp.route('/report', methods=['POST'])
def report_feedback():
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    stars = body.get('stars')
    if not task_id or not stars:
        return jsonify({'success': False, 'error': 'task_id 与 stars 必填'}), 400
    run_id = _feedback_run_id(task_id, body.get('run_id'))
    if not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    fid = dbutil.insert_feedback(
        run_id=run_id,
        kind='report_stars',
        stars=int(stars),
        comment=body.get('comment'),
    )
    reflect_async(run_id)
    remember_task_episode(task_id)
    return jsonify({'success': True, 'data': {'id': fid, 'run_id': run_id}})


@feedback_bp.route('/<task_id>', methods=['GET'])
def get_feedback(task_id: str):
    requested = request.args.get('run_id')
    run_id = _feedback_run_id(task_id, requested)
    if requested and not run_id:
        return jsonify({'success': False, 'error': 'run 不存在或不属于该任务'}), 404
    # 保持旧接口 data 为数组的响应形状。
    return jsonify({'success': True, 'data': dbutil.list_feedback(run_id or task_id)})
