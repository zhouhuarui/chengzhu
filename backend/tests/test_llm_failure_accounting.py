"""Offline regression tests for billable LLM failure accounting."""

from __future__ import annotations

import json
import os
import sys
import threading
from queue import Queue
from types import SimpleNamespace
from typing import Any, Callable, Optional

import httpx
from openai import OpenAI
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.utils import db as dbutil
from app.utils.llm_audit import (
    LLMBudgetExceeded,
    ensure_llm_call_budget,
    estimate_cost_cny,
    estimate_message_tokens,
    record_llm_client_error,
    record_llm_result,
    run_cost_cny,
    run_cost_summary,
)
from app.utils.llm_client import LLMClient, LLMResponseError


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    conn = getattr(dbutil._local, 'conn', None)
    if conn:
        conn.close()
        dbutil._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'llm-failures.db'))
    dbutil.init_db()
    yield
    conn = getattr(dbutil._local, 'conn', None)
    if conn:
        conn.close()
        dbutil._local.conn = None


_DEFAULT_USAGE = object()


def _completion(
    content: str,
    usage: Any = _DEFAULT_USAGE,
) -> httpx.Response:
    payload = {
            'id': 'chatcmpl-safe',
            'object': 'chat.completion',
            'created': 1,
            'model': 'deepseek-v4-pro',
            'choices': [{
                'index': 0,
                'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': content},
            }],
        }
    if usage is _DEFAULT_USAGE:
        payload['usage'] = {
            'prompt_tokens': 11,
            'completion_tokens': 7,
            'total_tokens': 18,
        }
    elif usage is not None:
        payload['usage'] = usage
    return httpx.Response(
        200,
        headers={'x-request-id': 'req-safe-123'},
        json=payload,
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 0,
    budget_run_id: Optional[str] = None,
) -> LLMClient:
    sdk = OpenAI(
        api_key='sk-test-never-persist',
        base_url='https://api.deepseek.com',
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
        timeout=httpx.Timeout(1.0),
    )
    return LLMClient(
        api_key='sk-test-never-persist',
        base_url='https://api.deepseek.com',
        model='deepseek-v4-pro',
        provider='deepseek',
        max_retries=max_retries,
        budget_run_id=budget_run_id,
        client=sdk,
    )


def test_two_paid_bad_json_responses_are_accounted_once(isolated_db):
    client = _client(lambda _request: _completion('not-json'))

    with pytest.raises(LLMResponseError) as raised:
        client.chat_json_result([{'role': 'user', 'content': 'private prompt'}])

    error = raised.value
    assert error.usage == {
        'prompt_tokens': 22,
        'completion_tokens': 14,
        'total_tokens': 36,
    }
    assert error.retry_count == 1
    assert record_llm_client_error('run-bad-json', 'reviewer', client, error) > 0

    rows = dbutil.list_llm_call_logs('run-bad-json')
    assert len(rows) == 1
    row = rows[0]
    assert row['ok'] == 0
    assert row['provider'] == 'deepseek'
    assert row['model'] == 'deepseek-v4-pro'
    assert row['prompt_tokens'] == 22
    assert row['completion_tokens'] == 14
    assert row['total_tokens'] == 36
    assert row['retry_count'] == 1
    assert row['cost_cny'] > 0


@pytest.mark.parametrize(
    'usage',
    [
        None,
        {'prompt_tokens': 11, 'total_tokens': 18},
        {'completion_tokens': 7, 'total_tokens': 18},
    ],
    ids=['missing-usage', 'missing-completion', 'missing-prompt'],
)
def test_success_with_incomplete_usage_settles_at_reserved_amount(
    isolated_db,
    monkeypatch,
    usage,
):
    run_id = 'run-incomplete-success'
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)
    client = _client(
        lambda _request: _completion('ok', usage),
        budget_run_id=run_id,
    )

    result = client.chat_result(
        [{'role': 'user', 'content': 'frozen evidence'}],
        max_tokens=128,
    )
    reservation = dbutil.get_llm_budget_reservation(
        result.budget_reservation_id,
    )
    assert reservation and reservation['status'] == 'active'

    record_llm_result(run_id, 'analyst', result)

    row = dbutil.list_llm_call_logs(run_id)[0]
    assert row['cost_cny'] == pytest.approx(reservation['amount_cny'])
    assert row['cost_cny'] > 0
    assert dbutil.get_llm_budget_reservation(
        result.budget_reservation_id,
    )['status'] == 'released'
    assert run_cost_cny(run_id) == pytest.approx(reservation['amount_cny'])


def test_timeout_without_provider_usage_settles_conservatively(
    isolated_db,
    monkeypatch,
):
    run_id = 'run-timeout-no-usage'
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('provider returned no usage', request=request)

    client = _client(timeout, budget_run_id=run_id)
    with pytest.raises(Exception) as raised:
        client.chat_result(
            [{'role': 'user', 'content': 'frozen evidence'}],
            max_tokens=128,
        )

    error = raised.value
    assert 'timeout' in error.__class__.__name__.lower()
    reservation = dbutil.get_llm_budget_reservation(
        error.budget_reservation_id,
    )
    assert reservation and reservation['status'] == 'active'
    record_llm_client_error(run_id, 'reviewer', client, error)

    row = dbutil.list_llm_call_logs(run_id)[0]
    assert row['ok'] == 0
    assert row['cost_cny'] == pytest.approx(reservation['amount_cny'])
    assert row['cost_cny'] > 0


def test_retry_then_partial_usage_cannot_look_complete_after_merge(
    isolated_db,
    monkeypatch,
):
    run_id = 'run-retry-partial-usage'
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout('first attempt uncertain', request=request)
        return _completion(
            'ok',
            {'prompt_tokens': 11, 'total_tokens': 11},
        )

    client = _client(
        handler,
        max_retries=1,
        budget_run_id=run_id,
    )
    result = client.chat_result(
        [{'role': 'user', 'content': 'frozen evidence'}],
        max_tokens=128,
    )
    # Conservative retry usage contributes completion_tokens, while the
    # successful response contributes only prompt_tokens.  The aggregate has
    # both keys but is still not provider-complete.
    assert 'prompt_tokens' in result.usage
    assert 'completion_tokens' in result.usage
    assert result.usage_complete is False
    reservation = dbutil.get_llm_budget_reservation(
        result.budget_reservation_id,
    )

    record_llm_result(run_id, 'analyst', result)

    assert dbutil.list_llm_call_logs(run_id)[0]['cost_cny'] == pytest.approx(
        reservation['amount_cny']
    )


def test_real_reservation_settlement_failure_is_not_best_effort(
    isolated_db,
    monkeypatch,
):
    run_id = 'run-settlement-failure'
    client = _client(
        lambda _request: _completion('ok'),
        budget_run_id=run_id,
    )
    result = client.chat_result(
        [{'role': 'user', 'content': 'x'}],
        max_tokens=128,
    )

    def fail_settlement(*_args, **_kwargs):
        raise OSError('database unavailable')

    monkeypatch.setattr(dbutil, 'insert_llm_call_log', fail_settlement)
    with pytest.raises(OSError, match='database unavailable'):
        record_llm_result(run_id, 'analyst', result)

    transport_error = LLMResponseError('provider failure')
    transport_error._llm_safe_metadata = True
    transport_error.budget_reservation_id = result.budget_reservation_id
    with pytest.raises(OSError, match='database unavailable'):
        record_llm_client_error(run_id, 'analyst', client, transport_error)


def test_run_cost_uses_settled_plus_active_committed_ledger(isolated_db):
    run_id = 'run-final-committed'
    dbutil.insert_llm_call_log(
        run_id,
        'deepseek',
        'deepseek-v4-flash',
        cost_cny=0.4,
    )
    assert dbutil.reserve_llm_budget(run_id, 'active-final-call', 0.6, 2.0)

    assert run_cost_summary(run_id) == pytest.approx({
        'settled_cny': 0.4,
        'reserved_cny': 0.6,
        'committed_cny': 1.0,
    })
    assert run_cost_cny(run_id) == pytest.approx(1.0)


def test_pipeline_budget_probe_fails_closed_when_ledger_is_unavailable(
    monkeypatch,
):
    from app.services import pipeline as pipeline_module

    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'configured')
    monkeypatch.setattr(
        pipeline_module,
        'run_cost_cny',
        lambda _run_id: (_ for _ in ()).throw(OSError('ledger unavailable')),
    )

    assert not pipeline_module._budget_allows_llm('run-ledger-down')


def test_paid_bad_json_before_transport_failure_keeps_prior_usage(isolated_db):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _completion('{"broken":')
        raise httpx.ReadTimeout(
            'request included private prompt and sk-do-not-store',
            request=request,
        )

    client = _client(handler)
    with pytest.raises(Exception) as raised:
        client.chat_json_result([{'role': 'user', 'content': 'private prompt'}])

    error = raised.value
    # The known first response must remain present.  The timed-out request is
    # conservatively charged up to its admitted token ceiling because the
    # provider may have processed it without returning usage metadata.
    assert error.usage['prompt_tokens'] >= 11
    assert error.usage['completion_tokens'] >= 7
    assert error.usage['total_tokens'] >= 18
    assert error.retry_count == 1
    assert error.request_id == 'req-safe-123'
    record_llm_client_error('run-transport', 'analyst', client, error)
    row = dbutil.list_llm_call_logs('run-transport')[0]
    assert row['total_tokens'] >= 18
    assert row['cost_cny'] > 0
    assert 'private prompt' not in (row['error'] or '')
    assert 'sk-do-not-store' not in (row['error'] or '')


def test_safe_error_helper_never_persists_exception_body_or_message(isolated_db):
    client = _client(lambda _request: _completion('unused'))
    error = RuntimeError('private prompt sk-secret reasoning chain')
    error.status_code = 500
    error.body = {
        'error': {
            'type': 'sk-secret',
            'code': 'private-prompt',
            'message': 'raw chain of thought',
        }
    }
    error.usage = {
        'prompt_tokens': 2,
        'completion_tokens': 3,
        'total_tokens': 5,
    }

    record_llm_client_error('run-redaction', 'chat', client, error)
    row = dbutil.list_llm_call_logs('run-redaction')[0]
    serialized = json.dumps(row, ensure_ascii=False)
    assert row['error'] == 'RuntimeError status=500'
    # Arbitrary exception attributes are untrusted; only metadata attached by
    # LLMClient is accepted for billing.
    assert row['total_tokens'] == 0
    for secret in ('private prompt', 'private-prompt', 'sk-secret', 'chain of thought'):
        assert secret not in serialized


def _paid_failure(message: str = 'invalid model output') -> LLMResponseError:
    error = LLMResponseError(message)
    error.usage = {
        'prompt_tokens': 20,
        'completion_tokens': 10,
        'total_tokens': 30,
    }
    error.retry_count = 1
    return error


def test_chat_binds_explicit_run_budget_and_accounts_fallback(
    isolated_db,
    monkeypatch,
):
    from app.services import chat_agent as chat_module
    from app.utils import llm_client as client_module

    captured: dict[str, Any] = {}

    class FailingClient:
        provider = 'deepseek'
        model = 'deepseek-v4-flash'

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat_result(self, *_args, **_kwargs):
            raise _paid_failure()

    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'sk-runtime-only')
    monkeypatch.setattr(client_module, 'LLMClient', FailingClient)
    monkeypatch.setattr(
        chat_module,
        'load_report',
        lambda *_args, **_kwargs: {
            'title': '测试报告',
            'summary': '摘要',
            'sections': [],
            'disclaimer': '非投资建议',
        },
    )
    monkeypatch.setattr(
        chat_module,
        'call_analyze_tool',
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            data={'text': '本地证据回答', 'card_ids': []},
        ),
    )

    answer = chat_module.ChatAgent('task-chat', run_id='run-chat').ask('发生了什么？')

    assert captured['budget_run_id'] == 'run-chat'
    assert '大模型不可用' in answer['answer']
    row = dbutil.list_llm_call_logs('run-chat')[0]
    assert row['agent'] == 'chat'
    assert row['ok'] == 0
    assert row['total_tokens'] == 30
    assert row['cost_cny'] > 0


def test_reflection_binds_explicit_run_budget_and_accounts_fallback(
    isolated_db,
    monkeypatch,
):
    from app.services import reflection as reflection_module
    from app.utils import llm_client as client_module

    captured: dict[str, Any] = {}

    class FailingClient:
        provider = 'deepseek'
        model = 'deepseek-v4-flash'

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat_json_result(self, *_args, **_kwargs):
            raise _paid_failure()

    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'sk-runtime-only')
    monkeypatch.setattr(client_module, 'LLMClient', FailingClient)
    dbutil.insert_feedback(
        'run-reflection',
        'section_vote',
        vote='down',
        comment='只要表格',
    )

    result = reflection_module.reflect_on_run('run-reflection')

    assert captured['budget_run_id'] == 'run-reflection'
    assert result['rules_created']
    row = dbutil.list_llm_call_logs('run-reflection')[0]
    assert row['agent'] == 'reflection'
    assert row['ok'] == 0
    assert row['total_tokens'] == 30


def test_announcement_reader_uses_run_budget_and_falls_back_safely(
    isolated_db,
    monkeypatch,
):
    from app.tools import read_announcement as announcement_module
    from app.utils import llm_client as client_module

    captured: dict[str, Any] = {}

    class FailingClient:
        provider = 'deepseek'
        model = 'deepseek-v4-flash'

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def chat_result(self, *_args, **_kwargs):
            raise _paid_failure('private announcement text')

    monkeypatch.setattr(Config, 'LLM_API_KEY', 'sk-legacy-switch')
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'sk-runtime-only')
    monkeypatch.setattr(client_module, 'LLMClient', FailingClient)
    monkeypatch.setattr(
        announcement_module,
        'download_announcement_pdf',
        lambda _url: '公告原文',
    )

    answer = announcement_module.read_announcement(
        'https://example.test/a.pdf',
        question='收入是多少？',
        run_id='run-announcement',
    )

    assert captured['budget_run_id'] == 'run-announcement'
    assert '公告原文' in answer
    row = dbutil.list_llm_call_logs('run-announcement')[0]
    assert row['agent'] == 'announcement_reader'
    assert row['ok'] == 0
    assert row['total_tokens'] == 30
    assert 'private announcement text' not in (row['error'] or '')


def test_concurrent_clients_cannot_both_admit_against_same_run(
    isolated_db,
    monkeypatch,
):
    run_id = 'run-concurrent-budget'
    messages = [{'role': 'user', 'content': '分析冻结证据'}]
    max_tokens = 4096
    estimated = estimate_cost_cny(
        'deepseek',
        'deepseek-v4-pro',
        {
            'prompt_tokens': estimate_message_tokens(messages),
            'completion_tokens': max_tokens,
        },
    )
    assert estimated > 0
    # One request fits below RMB 2, but two concurrent worst-case requests do
    # not.  Without an atomic reservation both check-then-call paths pass.
    settled = 2.0 - estimated * 1.5
    dbutil.insert_llm_call_log(
        run_id,
        'deepseek',
        'deepseek-v4-pro',
        agent='existing',
        cost_cny=settled,
    )
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)

    provider_entered = threading.Event()
    release_provider = threading.Event()
    provider_calls = 0
    provider_lock = threading.Lock()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        with provider_lock:
            provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return _completion('{"ok": true}')

    first = _client(handler, budget_run_id=run_id)
    second = _client(handler, budget_run_id=run_id)
    outcomes: Queue[Any] = Queue()

    def first_call() -> None:
        try:
            outcomes.put(first.chat_result(messages, max_tokens=max_tokens))
        except Exception as error:  # pragma: no cover - failure diagnostics
            outcomes.put(error)

    thread = threading.Thread(target=first_call)
    thread.start()
    assert provider_entered.wait(timeout=5)

    with pytest.raises(LLMBudgetExceeded):
        second.chat_result(messages, max_tokens=max_tokens)
    assert provider_calls == 1
    assert len(
        dbutil.list_llm_budget_reservations(run_id, active_only=True)
    ) == 1

    release_provider.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    first_result = outcomes.get_nowait()
    assert not isinstance(first_result, Exception)
    assert first_result.budget_reservation_id
    assert 'budget_reservation_id' not in first_result.to_metadata()

    record_llm_result(run_id, 'chat', first_result)
    assert dbutil.list_llm_budget_reservations(run_id, active_only=True) == []
    assert dbutil.llm_budget_totals(run_id)['committed_cny'] <= 2.0


def test_failed_call_reservation_releases_only_after_error_log(
    isolated_db,
    monkeypatch,
):
    run_id = 'run-failed-reservation'
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)
    client = _client(
        lambda _request: _completion('not-json'),
        budget_run_id=run_id,
    )

    with pytest.raises(LLMResponseError) as raised:
        client.chat_json_result(
            [{'role': 'user', 'content': 'private prompt'}],
            max_tokens=128,
        )

    error = raised.value
    reservation_id = error.budget_reservation_id
    assert reservation_id
    assert dbutil.get_llm_budget_reservation(reservation_id)['status'] == 'active'

    record_llm_client_error(run_id, 'reviewer', client, error)
    assert dbutil.get_llm_budget_reservation(reservation_id)['status'] == 'released'
    assert dbutil.list_llm_call_logs(run_id)[0]['ok'] == 0


def test_log_and_release_are_atomic_on_run_mismatch(isolated_db, monkeypatch):
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)
    client = _client(
        lambda _request: _completion('ok'),
        budget_run_id='run-owner',
    )
    result = client.chat_result(
        [{'role': 'user', 'content': 'x'}],
        max_tokens=128,
    )
    reservation_id = result.budget_reservation_id
    assert reservation_id

    with pytest.raises(ValueError, match='run mismatch'):
        record_llm_result('run-wrong', 'chat', result)

    assert dbutil.list_llm_call_logs('run-wrong') == []
    assert dbutil.get_llm_budget_reservation(reservation_id)['status'] == 'active'
    record_llm_result('run-owner', 'chat', result)
    assert dbutil.get_llm_budget_reservation(reservation_id)['status'] == 'released'
    with pytest.raises(ValueError, match='already settled'):
        record_llm_result('run-owner', 'chat', result)
    assert len(dbutil.list_llm_call_logs('run-owner')) == 1


def test_compatibility_budget_check_counts_active_reservations(
    isolated_db,
    monkeypatch,
):
    monkeypatch.setattr(Config, 'LLM_COST_BUDGET_CNY', 2.0)
    assert dbutil.reserve_llm_budget('run-ensure', 'manual-active', 1.999, 2.0)

    with pytest.raises(LLMBudgetExceeded):
        ensure_llm_call_budget(
            'run-ensure',
            provider='deepseek',
            model='deepseek-v4-pro',
            messages=[{'role': 'user', 'content': 'x'}],
            max_tokens=128,
        )
