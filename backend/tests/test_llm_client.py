"""Offline contract tests for DeepSeek's OpenAI-compatible transport."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Dict, List

import httpx
from openai import OpenAI
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.utils.llm_client import LLMClient, LLMResponseError
from app.utils.run_limits import RunDeadlineExceeded


def _completion(
    content: str,
    *,
    finish_reason: str = 'stop',
    model: str = 'deepseek-v4-pro',
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={'x-request-id': 'req-test-123'},
        json={
            'id': 'chatcmpl-test',
            'object': 'chat.completion',
            'created': 1,
            'model': model,
            'choices': [
                {
                    'index': 0,
                    'finish_reason': finish_reason,
                    'message': {'role': 'assistant', 'content': content},
                }
            ],
            'usage': {
                'prompt_tokens': 11,
                'completion_tokens': 7,
                'total_tokens': 18,
            },
        },
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model: str = 'deepseek-v4-pro',
    sdk_retries: int = 1,
) -> LLMClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk = OpenAI(
        api_key='sk-deepseek-test',
        base_url='https://api.deepseek.com',
        http_client=http_client,
        # LLMClient owns the bounded retry so metadata remains observable.
        max_retries=0,
        timeout=httpx.Timeout(1.0),
    )
    return LLMClient(
        api_key='sk-deepseek-test',
        base_url='https://api.deepseek.com',
        model=model,
        provider='deepseek',
        max_retries=sdk_retries,
        client=sdk,
    )


def test_deepseek_thinking_shape_and_result_metadata():
    requests: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL('https://api.deepseek.com/chat/completions')
        assert request.headers['authorization'] == 'Bearer sk-deepseek-test'
        requests.append(json.loads(request.content))
        return _completion('最终答案')

    result = _client(handler).chat_result(
        [{'role': 'user', 'content': '分析证据'}],
        temperature=0.1,
        max_tokens=321,
        thinking=True,
        reasoning_effort='high',
    )

    assert requests == [
        {
            'messages': [{'role': 'user', 'content': '分析证据'}],
            'model': 'deepseek-v4-pro',
            'max_tokens': 321,
            'reasoning_effort': 'high',
            'thinking': {'type': 'enabled'},
        }
    ]
    assert result.content == '最终答案'
    assert result.provider == 'deepseek'
    assert result.model == 'deepseek-v4-pro'
    assert result.finish_reason == 'stop'
    assert result.usage == {
        'prompt_tokens': 11,
        'completion_tokens': 7,
        'total_tokens': 18,
    }
    assert result.request_id == 'req-test-123'
    assert result.latency_ms >= 0
    assert 'content' not in result.to_metadata()


def test_non_thinking_is_explicit_and_legacy_chat_returns_string():
    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _completion('<think>private</think> visible')

    value = _client(handler, model='deepseek-v4-flash').chat(
        [{'role': 'user', 'content': 'hello'}],
        temperature=0.25,
    )

    assert value == 'visible'
    assert payloads[0]['thinking'] == {'type': 'disabled'}
    assert payloads[0]['temperature'] == 0.25
    assert 'reasoning_effort' not in payloads[0]


@pytest.mark.parametrize(
    ('first_content', 'first_finish'),
    [
        ('', 'stop'),
        ('{"broken":', 'stop'),
        ('{"partial": true', 'length'),
        ('{"ok": true} trailing prose', 'stop'),
    ],
)
def test_json_regeneration_is_once_and_keeps_token_limit(
    first_content: str,
    first_finish: str,
):
    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return _completion(first_content, finish_reason=first_finish)
        return _completion('{"ok": true}')

    result = _client(handler).chat_json_result(
        [{'role': 'user', 'content': '给出结果'}],
        max_tokens=77,
    )

    assert result.data == {'ok': True}
    assert result.retry_count == 1
    assert result.usage == {
        'prompt_tokens': 22,
        'completion_tokens': 14,
        'total_tokens': 36,
    }
    assert len(payloads) == 2
    assert [payload['max_tokens'] for payload in payloads] == [77, 77]
    assert all(payload['thinking'] == {'type': 'disabled'} for payload in payloads)
    combined_messages = '\n'.join(
        str(message.get('content', '')) for message in payloads[0]['messages']
    )
    assert 'JSON' in combined_messages
    assert '{"result": null}' in combined_messages


def test_json_fails_after_one_regeneration():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion('not-json')

    with pytest.raises(LLMResponseError, match='invalid JSON'):
        _client(handler).chat_json([{'role': 'user', 'content': 'x'}])
    assert calls == 2


def test_json_trailing_prose_fails_after_one_regeneration():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion('{"ok": true} trailing prose')

    with pytest.raises(LLMResponseError, match='trailing content'):
        _client(handler).chat_json([{'role': 'user', 'content': 'x'}])
    assert calls == 2


def test_explicit_response_format_rejection_has_one_prompt_only_fallback():
    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(
                400,
                json={
                    'error': {
                        'message': 'response_format is unsupported',
                        'type': 'invalid_request_error',
                        'param': 'response_format',
                        'code': 'unsupported_parameter',
                    }
                },
            )
        return _completion('{"fallback": true}')

    assert _client(handler).chat_json([{'role': 'user', 'content': 'x'}]) == {
        'fallback': True
    }
    assert len(payloads) == 2
    assert payloads[0]['response_format'] == {'type': 'json_object'}
    assert 'response_format' not in payloads[1]


@pytest.mark.parametrize('status_code', [429, 500, 503])
def test_sdk_retries_transient_http_status_once(status_code: int):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                status_code,
                headers={'retry-after-ms': '0'},
                json={
                    'error': {
                        'message': 'temporarily unavailable',
                        'type': 'server_error',
                        'code': 'resource_unavailable',
                    }
                },
            )
        return _completion('ok')

    assert _client(handler).chat([{'role': 'user', 'content': 'x'}]) == 'ok'
    assert calls == 2


def test_sdk_retries_timeout_once():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout('timed out', request=request)
        return _completion('ok')

    assert _client(handler).chat([{'role': 'user', 'content': 'x'}]) == 'ok'
    assert calls == 2


def test_retry_configuration_rejects_more_than_one():
    with pytest.raises(ValueError, match='max_retries'):
        LLMClient(
            api_key='x',
            base_url='https://api.deepseek.com',
            model='deepseek-v4-flash',
            max_retries=2,
            client=object(),
        )


def test_absolute_deadline_drops_a_late_transport_result():
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        return _completion('too late')

    client = _client(handler, sdk_retries=0)
    client.connect_timeout = 0.01
    client.read_timeout = 0.01
    client.deadline_epoch = time.time() + 0.1
    started = time.monotonic()
    with pytest.raises(RunDeadlineExceeded) as caught:
        client.chat_result(
            [{'role': 'user', 'content': 'x'}],
            max_tokens=20,
        )

    assert time.monotonic() - started < 0.1
    # The upstream may still have processed a timed-out request, so the error
    # carries a conservative billable bound for the audit ledger.
    assert caught.value.usage['completion_tokens'] == 20
