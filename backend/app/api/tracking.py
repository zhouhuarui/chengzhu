"""追踪订阅蓝图。"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from ..utils import db as dbutil
from ..utils.task_run_lock import task_run_lock
from ..services import tracking_service as ts

tracking_bp = Blueprint('tracking', __name__)


@tracking_bp.route('/subscribe', methods=['POST'])
def subscribe():
    body = request.get_json(silent=True) or {}
    task_id = body.get('task_id')
    if not task_id:
        return jsonify({'success': False, 'error': 'task_id 必填'}), 400
    try:
        sub = ts.subscribe(task_id, body.get('cron', 'weekly'), int(body.get('hour', 8)))
        return jsonify({'success': True, 'data': sub})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@tracking_bp.route('/list', methods=['GET'])
def list_subs():
    subs = dbutil.list_tracking_subs()
    for s in subs:
        briefs = dbutil.list_briefs(s['sub_id'])
        s['latest_brief'] = briefs[0] if briefs else None
        s['brief_count'] = len(briefs)
    return jsonify({'success': True, 'data': subs})


@tracking_bp.route('/<sub_id>/pause', methods=['POST'])
def pause(sub_id: str):
    dbutil.update_tracking_sub(sub_id, status='paused')
    return jsonify({'success': True, 'data': dbutil.get_tracking_sub(sub_id)})


@tracking_bp.route('/<sub_id>/resume', methods=['POST'])
def resume(sub_id: str):
    dbutil.update_tracking_sub(sub_id, status='active')
    return jsonify({'success': True, 'data': dbutil.get_tracking_sub(sub_id)})


@tracking_bp.route('/<sub_id>', methods=['DELETE'])
def delete_sub(sub_id: str):
    sub = dbutil.get_tracking_sub(sub_id)
    if not sub:
        return jsonify({'success': False, 'error': '订阅不存在'}), 404
    with task_run_lock(str(sub['task_id'])):
        dbutil.delete_tracking_sub(sub_id)
    return jsonify({'success': True})


@tracking_bp.route('/<sub_id>/briefs', methods=['GET'])
def briefs(sub_id: str):
    items = dbutil.list_briefs(sub_id)
    for b in items:
        path = b.get('markdown_path')
        if path and os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                b['markdown'] = f.read()
        b['title'] = f"简报 {b.get('date')}"
    return jsonify({'success': True, 'data': items})


@tracking_bp.route('/<sub_id>/run-now', methods=['POST'])
def run_now(sub_id: str):
    try:
        data = ts.run_subscription_now(sub_id)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@tracking_bp.route('/notifications', methods=['GET'])
def notifications():
    unread = dbutil.list_unread_briefs()
    return jsonify({'success': True, 'data': {'count': len(unread), 'items': unread}})
