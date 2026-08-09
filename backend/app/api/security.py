"""Private, bounded security-master lookup for the task symbol picker."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.security_master import (
    SecurityMasterUnavailableError,
    get_security_master,
)


security_bp = Blueprint('security', __name__)


@security_bp.route('/search', methods=['GET'])
def search_securities():
    query = str(request.args.get('q') or '').strip()
    if not query:
        return jsonify({
            'success': False,
            'code': 'security_query_required',
            'error': 'q 必填',
        }), 400
    raw_limit = request.args.get('limit', '10')
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({
            'success': False,
            'code': 'invalid_security_limit',
            'error': 'limit 必须是整数',
        }), 400
    if limit < 1:
        return jsonify({
            'success': False,
            'code': 'invalid_security_limit',
            'error': 'limit 必须大于 0',
        }), 400

    master = get_security_master()
    effective_limit = min(limit, master.max_results)
    try:
        items = master.search(query, limit=effective_limit)
        as_of = master.as_of
    except ValueError as exc:
        return jsonify({
            'success': False,
            'code': 'invalid_security_query',
            'error': str(exc),
        }), 400
    except SecurityMasterUnavailableError:
        return jsonify({
            'success': False,
            'code': 'security_master_unavailable',
            'error': '本地证券主数据暂不可用',
        }), 503

    response = jsonify({
        'success': True,
        'data': {
            'items': items,
            'count': len(items),
            'query': query,
            'limit': effective_limit,
            'as_of': as_of,
            'using_cached_snapshot': bool(master.last_refresh_error),
        },
    })
    response.headers['Cache-Control'] = 'private, max-age=30'
    return response
