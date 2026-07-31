"""记忆 / playbook / 健康度蓝图。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.source_health import get_all_source_health, get_source_health
from ..services import memory_service, playbook
from ..utils import db as dbutil

memory_bp = Blueprint('memory', __name__)


@memory_bp.route('/source-health', methods=['GET'])
def source_health():
    window = int(request.args.get('window_days', 7))
    tool = request.args.get('tool')
    if tool:
        data = get_source_health(tool, window)
    else:
        data = get_all_source_health(window)
    return jsonify({'success': True, 'data': data})


@memory_bp.route('/prefill', methods=['GET'])
def prefill():
    return jsonify({'success': True, 'data': memory_service.get_prefill()})


@memory_bp.route('/preferences', methods=['GET'])
def preferences():
    prefs = memory_service.list_user_preferences() if hasattr(memory_service, 'list_user_preferences') else dbutil.list_user_preferences()
    return jsonify({'success': True, 'data': {'preferences': prefs}})


@memory_bp.route('/preferences/<key>', methods=['DELETE'])
def delete_preference(key: str):
    dbutil.tombstone_user_preference(key)
    return jsonify({'success': True, 'data': {'key': key, 'tombstone_days': 7}})


@memory_bp.route('/user', methods=['DELETE'])
def clear_user():
    memory_service.clear_user_memory()
    return jsonify({'success': True})


@memory_bp.route('/playbook', methods=['GET'])
def list_playbook():
    status = request.args.get('status')
    rules = dbutil.list_playbook_rules(status=status, user_id='default')
    return jsonify({'success': True, 'data': rules})


@memory_bp.route('/playbook/<int:rule_id>/confirm', methods=['POST'])
def confirm_playbook(rule_id: int):
    rule = playbook.confirm_rule(rule_id)
    if not rule:
        return jsonify({'success': False, 'error': '规则不存在'}), 404
    return jsonify({'success': True, 'data': rule})


@memory_bp.route('/playbook/<int:rule_id>', methods=['DELETE'])
def retire_playbook(rule_id: int):
    rule = playbook.retire_rule(rule_id)
    return jsonify({'success': True, 'data': rule})


@memory_bp.route('/playbook/stats', methods=['GET'])
def playbook_stats():
    return jsonify({'success': True, 'data': dbutil.playbook_stats()})
