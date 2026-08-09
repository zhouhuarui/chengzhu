"""Live competition preflight for the pinned AgentTeams deployment.

This module deliberately performs bounded, non-mutating control-plane reads,
one disposable MinIO round trip, unauthenticated route-denial checks, and two
one-token model probes.  It never prints response bodies or credentials.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from typing import Any, Dict

import httpx

from ...config import Config
from ...team import DEFAULT_AGENT_ROLE_IDS
from .client import AgentTeamsControllerClient


EXPECTED_WORKER_IMAGE = (
    'higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/'
    'agentteams-copaw-worker:v1.2.0@sha256:'
    'dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc'
)
EXPECTED_MODELS = {
    'research-lead': 'qwen3-30b-a3b-instruct-2507',
    'disclosure-researcher': 'qwen3-30b-a3b-instruct-2507',
    'market-context-researcher': 'qwen3-30b-a3b-instruct-2507',
    'quality-analyst': 'qwen3.5-plus',
    'growth-analyst': 'qwen3.5-plus',
    'evidence-judge': 'qwen3.5-plus',
    'report-writer': 'qwen3-30b-a3b-instruct-2507',
    'compliance-reviewer': 'qwen3-30b-a3b-instruct-2507',
}


class CompetitionPreflightError(RuntimeError):
    pass


def _resource_spec(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get('spec')
    return value if isinstance(value, dict) else payload


def _resource_name(payload: Dict[str, Any]) -> str:
    metadata = payload.get('metadata') or {}
    return str(payload.get('name') or metadata.get('name') or '')


def _validate_workers(controller: AgentTeamsControllerClient) -> None:
    for role in DEFAULT_AGENT_ROLE_IDS:
        payload = controller.worker(role)
        spec = _resource_spec(payload)
        if _resource_name(payload) not in {'', role}:
            raise CompetitionPreflightError(f'worker_identity_mismatch:{role}')
        if str(spec.get('model') or '') != EXPECTED_MODELS[role]:
            raise CompetitionPreflightError(f'worker_model_mismatch:{role}')
        if str(spec.get('runtime') or '').lower() not in {'copaw', 'qwenpaw'}:
            raise CompetitionPreflightError(f'worker_runtime_mismatch:{role}')
        if str(spec.get('image') or '') != EXPECTED_WORKER_IMAGE:
            raise CompetitionPreflightError(f'worker_image_mismatch:{role}')
        desired = str(spec.get('state') or '').lower()
        expected = 'running' if role == 'research-lead' else 'sleeping'
        if desired != expected:
            raise CompetitionPreflightError(f'worker_lifecycle_mismatch:{role}')


def _minio_round_trip() -> None:
    from ...services.artifact_store import AgentTeamsMinioArtifactStore

    store = AgentTeamsMinioArtifactStore()
    nonce = uuid.uuid4().hex
    object_name = f'chengzhu/preflight/{nonce}/probe.bin'
    payload = f'chengzhu-agentteams-v1.2.0:{nonce}'.encode('ascii')
    created = False
    try:
        store.client.put_object(
            store.bucket,
            object_name,
            io.BytesIO(payload),
            len(payload),
            content_type='application/octet-stream',
            metadata={'sha256': hashlib.sha256(payload).hexdigest()},
        )
        created = True
        stat = store.client.stat_object(store.bucket, object_name)
        if int(stat.size) != len(payload):
            raise CompetitionPreflightError('minio_round_trip_size_mismatch')
        response = store.client.get_object(store.bucket, object_name)
        try:
            restored = response.read(len(payload) + 1)
        finally:
            response.close()
            response.release_conn()
        if restored != payload:
            raise CompetitionPreflightError('minio_round_trip_content_mismatch')
    except CompetitionPreflightError:
        raise
    except Exception:
        raise CompetitionPreflightError('minio_round_trip_failed') from None
    finally:
        if created:
            try:
                store.client.remove_object(store.bucket, object_name)
            except Exception:
                # A failed cleanup is itself a failed write/delete health check.
                raise CompetitionPreflightError(
                    'minio_round_trip_cleanup_failed'
                ) from None


def _gateway_routes_deny_anonymous() -> None:
    gateway = os.environ.get(
        'AGENTTEAMS_AI_GATEWAY_INTERNAL_URL',
        'http://aigw-local.agentteams.io:8080',
    ).rstrip('/')
    host_header = os.environ.get(
        'AGENTTEAMS_AI_GATEWAY_HOST_HEADER',
        '',
    ).strip()
    request_headers = {'Host': host_header} if host_header else None
    for role in DEFAULT_AGENT_ROLE_IDS:
        url = f'{gateway}/mcp-servers/mcp-chengzhu-{role}/mcp'
        try:
            response = httpx.get(
                url,
                headers=request_headers,
                timeout=5.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise CompetitionPreflightError(
                f'mcp_gateway_unreachable:{role}'
            ) from error
        if response.status_code not in {401, 403}:
            raise CompetitionPreflightError(
                f'mcp_gateway_anonymous_not_denied:{role}:{response.status_code}'
            )


def _probe_models() -> None:
    if os.environ.get('AGENTTEAMS_PREFLIGHT_MODEL_CALLS', 'true').lower() != 'true':
        raise CompetitionPreflightError('model_preflight_may_not_be_disabled')
    if not Config.VISION_LLM_API_KEY:
        raise CompetitionPreflightError('model_preflight_key_missing')
    from openai import OpenAI

    client = OpenAI(
        api_key=Config.VISION_LLM_API_KEY,
        base_url=Config.VISION_LLM_BASE_URL,
        timeout=httpx.Timeout(10.0, connect=5.0),
        max_retries=0,
    )
    for model in ('qwen3-30b-a3b-instruct-2507', 'qwen3.5-plus'):
        try:
            client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': 'Reply OK.'}],
                max_tokens=1,
                temperature=0,
                extra_body={'enable_thinking': False},
            )
        except Exception as error:
            status = getattr(error, 'status_code', None)
            suffix = f':{status}' if isinstance(status, int) else ''
            raise CompetitionPreflightError(
                f'model_preflight_failed:{model}{suffix}'
            ) from None


def run_competition_preflight() -> Dict[str, Any]:
    controller = AgentTeamsControllerClient()
    state = controller.preflight()
    _validate_workers(controller)
    _minio_round_trip()
    _gateway_routes_deny_anonymous()
    _probe_models()
    return {
        'version': Config.AGENTTEAMS_VERSION,
        'team_phase': state['team'].get('phase'),
        'workers': len(DEFAULT_AGENT_ROLE_IDS),
        'models': len(set(EXPECTED_MODELS.values())),
        'minio_round_trip': True,
        'anonymous_mcp_denied': True,
    }


def main() -> None:
    result = run_competition_preflight()
    print(
        'AgentTeams application preflight passed: '
        f'version={result["version"]}, workers={result["workers"]}, '
        f'models={result["models"]}, MinIO=rw, MCP=deny-anonymous.'
    )


if __name__ == '__main__':
    main()
