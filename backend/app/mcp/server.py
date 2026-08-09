"""Minimal MCP 2024-11-05 Streamable HTTP endpoint for AgentTeams."""

from __future__ import annotations

import hmac
import json
import re
import time
from typing import Any, Callable, Dict, Optional

from flask import Flask, Response, jsonify, request

from ..config import Config
from ..services.agentteam_runtime import AgentTeamRuntime
from ..observability import configure_telemetry, traced_span
from ..team import (
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    TeamNotFoundError,
)
from ..utils.db import init_db


ROLE_TOOLS = {
    'research-lead': {'start_team_run', 'freeze_evidence', 'get_frozen_context', 'request_publish_approval'},
    'disclosure-researcher': {'collect_evidence', 'bailian_visual_proxy'},
    'market-context-researcher': {'collect_evidence'},
    'quality-analyst': {'get_frozen_context', 'submit_claims', 'submit_challenges'},
    'growth-analyst': {'get_frozen_context', 'submit_claims', 'submit_challenges'},
    'evidence-judge': {'get_frozen_context', 'audit_debate'},
    'report-writer': {'get_frozen_context', 'store_report_draft'},
    'compliance-reviewer': {'get_frozen_context', 'validate_report', 'submit_review'},
}

COMMON_WRITE = {
    'task_id': {'type': 'string'},
    'run_id': {'type': 'string'},
    'expected_version': {'type': 'integer', 'minimum': 0},
    'idempotency_key': {
        'type': 'string',
        'pattern': '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$',
    },
}

_IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')


def _schema(description: str, properties: Dict[str, Any], required: list[str]) -> Dict[str, Any]:
    return {
        'description': description,
        'inputSchema': {
            'type': 'object',
            'properties': properties,
            'required': required,
            'additionalProperties': False,
        },
    }


TOOL_SCHEMAS = {
    'start_team_run': _schema(
        'Research Lead accepts the durable TaskContract and opens the fixed DAG.',
        dict(COMMON_WRITE), list(COMMON_WRITE),
    ),
    'collect_evidence': _schema(
        'Collect one role-scoped source group. The server retries each collector once.',
        {**COMMON_WRITE, 'source_group': {'type': 'string', 'enum': ['disclosure', 'market_context']}},
        [*COMMON_WRITE, 'source_group'],
    ),
    'freeze_evidence': _schema(
        'Freeze the current run evidence exactly once and normalize FinancialFacts.',
        dict(COMMON_WRITE), list(COMMON_WRITE),
    ),
    'get_frozen_context': _schema(
        'Read a bounded page of frozen evidence, facts, or the audited verdict.',
        {
            'task_id': {'type': 'string'}, 'run_id': {'type': 'string'},
            'view': {'type': 'string', 'enum': ['evidence', 'facts', 'verdict']},
            'cursor': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
        },
        ['task_id', 'run_id'],
    ),
    'submit_claims': _schema(
        'Submit structured claims against frozen evidence; no retrieval is performed.',
        {
            **COMMON_WRITE,
            'claims': {'type': 'array', 'maxItems': 24, 'items': {'type': 'object'}},
            'round_number': {'type': 'integer', 'minimum': 1, 'maximum': 2},
            'finalize': {'type': 'boolean'},
        },
        [*COMMON_WRITE, 'claims', 'round_number', 'finalize'],
    ),
    'submit_challenges': _schema(
        'Submit cross-role counterevidence and optionally complete the analyst task.',
        {
            **COMMON_WRITE,
            'challenges': {'type': 'array', 'maxItems': 24, 'items': {'type': 'object'}},
            'round_number': {'type': 'integer', 'minimum': 1, 'maximum': 2},
            'finalize': {'type': 'boolean'},
        },
        [*COMMON_WRITE, 'challenges', 'round_number', 'finalize'],
    ),
    'audit_debate': _schema(
        'Run the authoritative deterministic auditor. Accepted IDs must all hard-pass.',
        {
            **COMMON_WRITE,
            'accepted_claim_ids': {
                'type': 'array', 'maxItems': 48, 'items': {'type': 'string'},
            },
        },
        [*COMMON_WRITE, 'accepted_claim_ids'],
    ),
    'store_report_draft': _schema(
        (
            'Store a bounded report plan. In evidence_debate, sections.claim_ids '
            'is the only narrative input: the backend rejects non-accepted IDs, '
            'rebuilds prose from hard-pass ClaimCards and ignores free-form metadata/content. '
            'In direct mode, title/goal/content retain their existing deterministic '
            'frozen-evidence relevance gate.'
        ),
        {
            **COMMON_WRITE,
            'draft': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'maxLength': 240},
                    'summary': {'type': 'string', 'maxLength': 4000},
                    'sections': {
                        'type': 'array',
                        'minItems': 0,
                        'maxItems': 16,
                        'items': {
                            'type': 'object',
                            'properties': {
                                'claim_ids': {
                                    'type': 'array',
                                    'maxItems': 48,
                                    'items': {
                                        'type': 'string',
                                        'minLength': 1,
                                        'maxLength': 80,
                                    },
                                },
                                'title': {'type': 'string', 'maxLength': 160},
                                'goal': {'type': 'string', 'maxLength': 500},
                                'content': {'type': 'string', 'maxLength': 20000},
                            },
                            'additionalProperties': False,
                        },
                    },
                },
                'required': ['sections'],
                'additionalProperties': False,
            },
        },
        [*COMMON_WRITE, 'draft'],
    ),
    'validate_report': _schema(
        'Run deterministic citation, financial, chart and compliance gates.',
        dict(COMMON_WRITE), list(COMMON_WRITE),
    ),
    'submit_review': _schema(
        'Submit pass/revise after deterministic validation; this never publishes.',
        {
            **COMMON_WRITE,
            'decision': {'type': 'string', 'enum': ['pass', 'revise']},
            'issues': {'type': 'array', 'maxItems': 100, 'items': {'type': 'object'}},
        },
        [*COMMON_WRITE, 'decision', 'issues'],
    ),
    'request_publish_approval': _schema(
        'Register the latest reviewed candidate and stop at the Vue human gate.',
        dict(COMMON_WRITE), list(COMMON_WRITE),
    ),
    'bailian_visual_proxy': _schema(
        'Analyze an explicitly authorized uploaded PDF/image; falls back locally and records degraded state.',
        {
            **COMMON_WRITE,
            'artifact_name': {'type': 'string', 'pattern': '^[A-Za-z0-9_.-]{1,160}$'},
            'user_authorized': {'type': 'boolean'},
        },
        [*COMMON_WRITE, 'artifact_name', 'user_authorized'],
    ),
}


def _bearer_token() -> str:
    value = request.headers.get('Authorization', '')
    if not value.lower().startswith('bearer '):
        return ''
    return value[7:].strip()


def _authenticated_role(app: Flask, route_role: Optional[str] = None) -> Optional[str]:
    expected = str(Config.AGENTTEAMS_MCP_GATEWAY_TOKEN or '')
    supplied = _bearer_token()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        # Unit tests may omit a secret but never get this bypass in a live app.
        if not app.testing:
            return None
    header_role = str(
        request.headers.get('X-AgentTeams-Worker')
        or request.headers.get('X-Chengzhu-Role')
        or ''
    ).strip()
    if route_role:
        # Higress exposes one consumer-scoped route per Worker and calls a
        # role-specific upstream path with a server-only bearer token.  A
        # supplied header may confirm that identity but may never override it.
        role = str(route_role).strip()
        if header_role and header_role != role:
            return None
    else:
        role = header_role
    return role if role in ROLE_TOOLS else None


def _tool_result(payload: Dict[str, Any], *, is_error: bool = False) -> Dict[str, Any]:
    return {
        'content': [{
            'type': 'text',
            'text': json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
        }],
        'structuredContent': payload,
        'isError': bool(is_error),
    }


def _required(arguments: Dict[str, Any], name: str) -> Any:
    if name not in arguments:
        raise ValueError(f'missing argument: {name}')
    return arguments[name]


def _safe_idempotency(arguments: Dict[str, Any]) -> str:
    value = str(_required(arguments, 'idempotency_key'))
    if not _IDEMPOTENCY_RE.fullmatch(value):
        raise ValueError('idempotency_key must be an opaque identifier')
    return value


def _record_tool_call(
    role: str,
    name: str,
    arguments: Dict[str, Any],
    *,
    ok: bool,
    latency_ms: int,
    result: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
) -> None:
    """Best-effort metadata logging; never persist arguments or model text."""

    run_id = str(arguments.get('run_id') or '')[:128] or None
    try:
        from ..utils.db import insert_tool_call_log

        insert_tool_call_log(
            name,
            ok,
            run_id=run_id,
            agent=role,
            degraded=bool((result or {}).get('degraded')),
            latency_ms=max(0, int(latency_ms)),
            error=(str(error_code)[:160] if error_code else None),
        )
    except Exception:
        pass
    idem = str(arguments.get('idempotency_key') or '')
    if not run_id or not _IDEMPOTENCY_RE.fullmatch(idem):
        return
    try:
        from ..services.agentteam_runtime import team_id_for_run
        from ..team import AgentTeamStore

        AgentTeamStore.append_event(
            team_id_for_run(run_id),
            'mcp_tool_completed' if ok else 'mcp_tool_failed',
            actor=role,
            payload={
                'tool': name,
                'ok': bool(ok),
                'degraded': bool((result or {}).get('degraded')),
                'latency_ms': max(0, int(latency_ms)),
                'error_code': str(error_code)[:160] if error_code else None,
            },
            idempotency_key=(
                f'mcp:{name}:{idem}:ok'
                if ok else f'mcp:{name}:{idem}:failed:{str(error_code)[:40]}'
            ),
        )
    except Exception:
        pass


def _call_tool(role: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name not in ROLE_TOOLS.get(role, set()):
        raise PermissionError(f'role {role} cannot call {name}')
    runtime = AgentTeamRuntime(
        str(_required(arguments, 'task_id')),
        str(_required(arguments, 'run_id')),
        role,
    )
    write_kwargs = lambda: {
        'expected_version': int(_required(arguments, 'expected_version')),
        'idempotency_key': _safe_idempotency(arguments),
    }
    if name == 'start_team_run':
        return runtime.start_team_run(**write_kwargs())
    if name == 'collect_evidence':
        return runtime.collect_evidence(
            str(_required(arguments, 'source_group')), **write_kwargs()
        )
    if name == 'freeze_evidence':
        return runtime.freeze_evidence(**write_kwargs())
    if name == 'get_frozen_context':
        return runtime.get_frozen_context(
            view=str(arguments.get('view') or 'evidence'),
            cursor=int(arguments.get('cursor') or 0),
            limit=int(arguments.get('limit') or 30),
        )
    if name == 'submit_claims':
        return runtime.submit_claims(
            list(_required(arguments, 'claims')),
            round_number=int(_required(arguments, 'round_number')),
            finalize=bool(_required(arguments, 'finalize')),
            **write_kwargs(),
        )
    if name == 'submit_challenges':
        return runtime.submit_challenges(
            list(_required(arguments, 'challenges')),
            round_number=int(_required(arguments, 'round_number')),
            finalize=bool(_required(arguments, 'finalize')),
            **write_kwargs(),
        )
    if name == 'audit_debate':
        return runtime.audit_debate(
            list(_required(arguments, 'accepted_claim_ids')), **write_kwargs()
        )
    if name == 'store_report_draft':
        return runtime.store_report_draft(
            dict(_required(arguments, 'draft')), **write_kwargs()
        )
    if name == 'validate_report':
        return runtime.validate_report(**write_kwargs())
    if name == 'submit_review':
        return runtime.submit_review(
            str(_required(arguments, 'decision')),
            list(_required(arguments, 'issues')),
            **write_kwargs(),
        )
    if name == 'request_publish_approval':
        return runtime.request_publish_approval(**write_kwargs())
    if name == 'bailian_visual_proxy':
        return runtime.bailian_visual_proxy(
            str(_required(arguments, 'artifact_name')),
            user_authorized=bool(_required(arguments, 'user_authorized')),
            **write_kwargs(),
        )
    raise ValueError(f'unknown tool: {name}')


def create_mcp_app(config_class=Config) -> Flask:
    configure_telemetry('chengzhu-mcp')
    app = Flask('chengzhu-mcp')
    app.config.from_object(config_class)
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
    init_db()

    @app.get('/health')
    def health():
        return {
            'status': 'ok',
            'service': 'chengzhu-mcp',
            'protocol_version': '2024-11-05',
            'agentteams_version': Config.AGENTTEAMS_VERSION,
        }

    @app.post('/mcp')
    @app.post('/mcp/<route_role>')
    def mcp(route_role: Optional[str] = None):
        role = _authenticated_role(app, route_role)
        if role is None:
            return jsonify({'error': 'unauthorized_agentteams_worker'}), 401
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            return jsonify({
                'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32700, 'message': 'Parse error'},
            }), 400
        method = value.get('method')
        request_id = value.get('id')
        if request_id is None and str(method).startswith('notifications/'):
            return Response(status=204)
        try:
            if method == 'initialize':
                result = {
                    'protocolVersion': '2024-11-05',
                    'serverInfo': {'name': 'chengzhu', 'version': '1.0.0'},
                    'capabilities': {'tools': {'listChanged': False}},
                }
            elif method == 'tools/list':
                result = {
                    'tools': [
                        {'name': name, **TOOL_SCHEMAS[name]}
                        for name in sorted(ROLE_TOOLS[role])
                    ],
                }
            elif method == 'tools/call':
                params = value.get('params') or {}
                name = str(params.get('name') or '')
                arguments = params.get('arguments') or {}
                if not isinstance(arguments, dict):
                    raise ValueError('tool arguments must be an object')
                started = time.monotonic()
                try:
                    with traced_span(
                        f'mcp.{name or "unknown"}',
                        attributes={
                            'run_id': str(arguments.get('run_id') or '')[:128],
                            'role': role,
                            'tool': name[:120],
                        },
                    ):
                        tool_payload = _call_tool(role, name, arguments)
                except Exception as error:
                    _record_tool_call(
                        role,
                        name or 'unknown',
                        arguments,
                        ok=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error_code=getattr(error, 'code', error.__class__.__name__),
                    )
                    raise
                _record_tool_call(
                    role,
                    name,
                    arguments,
                    ok=True,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    result=tool_payload,
                )
                result = _tool_result(tool_payload)
            else:
                return jsonify({
                    'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32601, 'message': 'Method not found'},
                })
            return jsonify({'jsonrpc': '2.0', 'id': request_id, 'result': result})
        except TeamConflictError as error:
            result = _tool_result({
                'ok': False,
                'error': error.code,
                'current_version': error.current_version,
                'retryable': True,
            }, is_error=True)
        except (TeamIdempotencyError, TeamInvariantError, TeamNotFoundError) as error:
            result = _tool_result({
                'ok': False,
                'error': getattr(error, 'code', 'team_error'),
                'retryable': False,
            }, is_error=True)
        except PermissionError:
            result = _tool_result({
                'ok': False, 'error': 'role_permission_denied', 'retryable': False,
            }, is_error=True)
        except (TypeError, ValueError) as error:
            result = _tool_result({
                'ok': False, 'error': 'invalid_tool_arguments',
                'detail': str(error)[:240], 'retryable': False,
            }, is_error=True)
        except Exception as error:
            from ..utils.llm_audit import safe_error_summary
            result = _tool_result({
                'ok': False,
                'error': safe_error_summary(error),
                'retryable': error.__class__.__name__ in {
                    'TimeoutError', 'ConnectError', 'ReadTimeout',
                },
            }, is_error=True)
        return jsonify({'jsonrpc': '2.0', 'id': request_id, 'result': result})

    return app
