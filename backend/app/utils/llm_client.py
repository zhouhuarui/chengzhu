"""OpenAI-compatible LLM client with explicit provider capabilities.

Text generation defaults to DeepSeek while keeping the original ``chat`` and
``chat_json`` return types.  Metadata-aware callers can use ``chat_result`` or
``chat_json_result``; neither result contains prompts, credentials, or model
reasoning content.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional

import httpx
from openai import OpenAI

from ..config import Config
from .openai_chat_compat import create_chat_completion, extract_chat_completion_text


logger = logging.getLogger(__name__)


class LLMResponseError(ValueError):
    """A safe, structured error for unusable model responses."""

    def __init__(self, message: str, *, finish_reason: Optional[str] = None):
        super().__init__(message)
        self.finish_reason = finish_reason
        # These fields contain billing/transport metadata only.  They let the
        # audit layer account for paid but unusable JSON responses without
        # persisting their content.
        self.usage: Dict[str, int] = {}
        self.provider: Optional[str] = None
        self.model: Optional[str] = None
        self.request_id: Optional[str] = None
        self.latency_ms: float = 0.0
        self.retry_count: int = 0
        self.budget_reservation_id: Optional[str] = None
        self._llm_safe_metadata = True


@dataclass(frozen=True)
class LLMResult:
    """Model output plus safe operational metadata.

    ``parsed_json`` is populated only by :meth:`LLMClient.chat_json_result`.
    Raw prompts and DeepSeek ``reasoning_content`` are deliberately excluded.
    """

    content: str
    provider: str
    model: str
    finish_reason: Optional[str]
    usage: Dict[str, int] = field(default_factory=dict)
    request_id: Optional[str] = None
    latency_ms: float = 0.0
    retry_count: int = 0
    parsed_json: Optional[Dict[str, Any]] = field(default=None, repr=False)
    budget_reservation_id: Optional[str] = field(default=None, repr=False)
    # Internal settlement signal. ``None`` keeps compatibility with injected
    # result objects, whose completeness is inferred from the usage mapping.
    # It is deliberately excluded from persisted metadata.
    usage_complete: Optional[bool] = field(default=None, repr=False)

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        """Compatibility-friendly alias for structured callers."""

        return self.parsed_json

    def to_metadata(self) -> Dict[str, Any]:
        """Return fields suitable for ``llm_call_log`` persistence."""

        usage = dict(self.usage)
        return {
            'provider': self.provider,
            'model': self.model,
            'finish_reason': self.finish_reason,
            'usage': usage,
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
            'request_id': self.request_id,
            'latency_ms': self.latency_ms,
            'retry_count': self.retry_count,
        }


def _is_response_format_unsupported(error: Exception) -> bool:
    """Detect an explicit provider rejection of JSON response_format."""

    if getattr(error, "status_code", None) not in {400, 422}:
        return False

    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return False

    details = body.get("error", body)
    if not isinstance(details, dict):
        return False

    param = str(details.get("param") or "").strip().lower()
    if param == "response_format" or param.startswith("response_format."):
        return True

    message = str(details.get("message") or "").lower()
    if "response_format" not in message:
        return False

    code = str(details.get("code") or "").lower()
    unsupported_codes = {
        "unsupported_parameter",
        "unsupported_value",
        "unknown_parameter",
        "invalid_parameter",
    }
    unsupported_phrases = (
        "not support",
        "unsupported",
        "unknown parameter",
        "unrecognized parameter",
    )
    return code in unsupported_codes or any(
        phrase in message for phrase in unsupported_phrases
    )


def _clean_chat_text(content: str) -> str:
    """Remove common reasoning wrappers and an outer Markdown JSON fence."""

    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
    cleaned = cleaned.lstrip("\ufeff")
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    return cleaned.strip()


def _contains_additional_json_container(content: str) -> bool:
    """Return True when trailing text embeds another JSON object or array."""

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", content):
        try:
            value, _ = decoder.raw_decode(content[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


def _normalise_usage(raw_usage: Any) -> Dict[str, int]:
    if raw_usage is None:
        return {}
    if isinstance(raw_usage, Mapping):
        values = raw_usage
    elif hasattr(raw_usage, 'model_dump'):
        values = raw_usage.model_dump()
    else:
        values = {
            key: getattr(raw_usage, key, None)
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
        }

    result: Dict[str, int] = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = values.get(key) if isinstance(values, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = int(value)
    return result


def _usage_has_billable_pair(raw_usage: Any) -> bool:
    usage = _normalise_usage(raw_usage)
    return 'prompt_tokens' in usage and 'completion_tokens' in usage


def _merge_usage(*items: Mapping[str, Any]) -> Dict[str, int]:
    """Add usage from every billable response in a logical call."""

    result: Dict[str, int] = {}
    for item in items:
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = item.get(key) if isinstance(item, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = result.get(key, 0) + max(0, int(value))
    if 'total_tokens' not in result and (
        'prompt_tokens' in result or 'completion_tokens' in result
    ):
        result['total_tokens'] = (
            result.get('prompt_tokens', 0) + result.get('completion_tokens', 0)
        )
    return result


def _attach_error_metadata(
    error: Exception,
    *,
    provider: str,
    model: str,
    usage: Optional[Mapping[str, Any]] = None,
    request_id: Optional[str] = None,
    latency_ms: float = 0.0,
    retry_count: int = 0,
    budget_reservation_id: Optional[str] = None,
    usage_complete: Optional[bool] = None,
) -> None:
    """Attach billing metadata to an exception without retaining content.

    Some SDK exception implementations may reject arbitrary attributes, so
    every assignment is deliberately best-effort.  Callers still have the
    client provider/model as a safe fallback when persisting the failure.
    """

    metadata = {
        'usage': _merge_usage(
            getattr(error, 'usage', None) or {},
            usage or {},
        ),
        'provider': provider,
        'model': model,
        'request_id': request_id,
        'latency_ms': round(max(0.0, float(latency_ms or 0.0)), 3),
        'retry_count': max(0, int(retry_count or 0)),
        'budget_reservation_id': (
            budget_reservation_id
            or getattr(error, 'budget_reservation_id', None)
        ),
        '_llm_safe_metadata': True,
    }
    existing_usage_complete = getattr(error, '_llm_usage_complete', None)
    if isinstance(existing_usage_complete, bool):
        metadata['_llm_usage_complete'] = (
            existing_usage_complete
            and bool(usage_complete)
        )
    elif isinstance(usage_complete, bool):
        metadata['_llm_usage_complete'] = usage_complete
    for key, value in metadata.items():
        try:
            setattr(error, key, value)
        except Exception:
            continue


def _response_metadata(response: Any, default_model: str) -> tuple[str, Optional[str], Dict[str, int]]:
    """Extract only safe billing identifiers from a raw response."""

    model = str(getattr(response, 'model', None) or default_model)
    request_id = (
        getattr(response, '_request_id', None)
        or getattr(response, 'request_id', None)
        or getattr(response, 'id', None)
    )
    return (
        model,
        str(request_id) if request_id else None,
        _normalise_usage(getattr(response, 'usage', None)),
    )


def _is_retryable_transport_error(error: Exception) -> bool:
    """Retry only the transient conditions allowed by the provider contract."""

    # An absolute run deadline is terminal.  Treating it as a normal timeout
    # would start a retry after the task's publication window has closed.
    if error.__class__.__name__ == 'RunDeadlineExceeded':
        return False
    status = getattr(error, 'status_code', None)
    if status in {429, 500, 503}:
        return True
    class_name = error.__class__.__name__.lower()
    if 'timeout' in class_name:
        return True
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return True
    body = getattr(error, 'body', None)
    details = body.get('error', body) if isinstance(body, Mapping) else {}
    code = str(details.get('code') or '').lower() if isinstance(details, Mapping) else ''
    error_type = str(details.get('type') or '').lower() if isinstance(details, Mapping) else ''
    return any(token in f'{code} {error_type}' for token in (
        'resource_unavailable', 'resource_exhausted', 'insufficient_resource',
        'server_overloaded',
    ))


def _provider_name(base_url: str, model: str, configured: Optional[str]) -> str:
    value = f'{base_url} {model}'.lower()
    if 'deepseek' in value:
        return 'deepseek'
    if 'dashscope' in value or model.lower().startswith('qwen'):
        return 'dashscope'
    return (configured or 'openai_compatible').strip().lower()


def _json_guided_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add provider-required JSON wording and a minimal valid example."""

    guidance = (
        '只返回一个有效的 JSON 对象，不要使用 Markdown 代码围栏或附加说明。'
        'JSON 示例：{"result": null}'
    )
    # Make a new list so retries and caller-owned conversation state are never
    # mutated. A separate system message also works for multimodal user input.
    return [{'role': 'system', 'content': guidance}] + [dict(item) for item in messages]


class LLMClient:
    """OpenAI-compatible text client with bounded retry behavior."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        *,
        provider: Optional[str] = None,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        deadline_epoch: Optional[float] = None,
        deadline_reserve_seconds: float = 0.0,
        budget_run_id: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        self.api_key = api_key or Config.TEXT_LLM_API_KEY
        self.base_url = base_url or Config.TEXT_LLM_BASE_URL
        self.model = model or Config.TEXT_LLM_FAST_MODEL
        self.provider = _provider_name(
            self.base_url,
            self.model,
            provider or Config.TEXT_LLM_PROVIDER,
        )
        self.connect_timeout = float(
            connect_timeout
            if connect_timeout is not None
            else Config.LLM_CONNECT_TIMEOUT_SECONDS
        )
        self.read_timeout = float(
            read_timeout
            if read_timeout is not None
            else Config.LLM_READ_TIMEOUT_SECONDS
        )
        self.max_retries = int(
            max_retries if max_retries is not None else Config.LLM_MAX_RETRIES
        )
        self.deadline_epoch = float(deadline_epoch) if deadline_epoch is not None else None
        self.deadline_reserve_seconds = max(0.0, float(deadline_reserve_seconds or 0))
        self.budget_run_id = str(budget_run_id) if budget_run_id else None

        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError('LLM timeout must be greater than zero')
        if self.max_retries not in {0, 1}:
            raise ValueError('LLM max_retries must be 0 or 1')

        if client is not None:
            # Dependency injection keeps unit tests entirely offline.
            self.client = client
            return

        if not self.api_key:
            raise ValueError("TEXT_LLM_API_KEY/LLM_API_KEY 未配置")

        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
            # Retry here, not inside the SDK, so retry_count and the one-retry
            # ceiling remain observable and provider-independent.
            max_retries=0,
        )

    def _create_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[Dict[str, Any]],
        thinking: bool,
        reasoning_effort: Optional[str],
    ) -> Any:
        """Send one raw request through the model compatibility layer."""

        return create_chat_completion(
            self.client,
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            provider=self.provider,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )

    def _conservative_attempt_usage(self, kwargs: Mapping[str, Any]) -> Dict[str, int]:
        """Bound a possibly billable request whose response usage is unknown."""

        from .llm_audit import estimate_message_tokens
        prompt = estimate_message_tokens(kwargs.get('messages') or [])
        completion = max(0, int(kwargs.get('max_tokens') or 0))
        return {
            'prompt_tokens': prompt,
            'completion_tokens': completion,
            'total_tokens': prompt + completion,
        }

    def _create_completion_with_retry(
        self,
        **kwargs: Any,
    ) -> tuple[Any, int, Dict[str, int]]:
        """Return response, physical attempts and conservative failed usage."""

        attempts = 0
        failed_usage: Dict[str, int] = {}
        while True:
            if self.deadline_epoch is not None:
                from .run_limits import ensure_time_remaining
                ensure_time_remaining(
                    self.deadline_epoch,
                    reserve_seconds=(
                        self.connect_timeout
                        + self.read_timeout
                        + self.deadline_reserve_seconds
                    ),
                    stage='llm_call',
                )
            attempts += 1
            try:
                if self.deadline_epoch is None:
                    response = self._create_completion(**kwargs)
                else:
                    from .run_limits import call_with_deadline
                    response = call_with_deadline(
                        lambda: self._create_completion(**kwargs),
                        self.deadline_epoch,
                        reserve_seconds=self.deadline_reserve_seconds,
                        maximum_seconds=self.connect_timeout + self.read_timeout,
                        stage='llm_call',
                    )
                return response, attempts, failed_usage
            except Exception as error:
                if attempts <= self.max_retries and _is_retryable_transport_error(error):
                    failed_usage = _merge_usage(
                        failed_usage,
                        self._conservative_attempt_usage(kwargs),
                    )
                    logger.warning(
                        'Transient LLM transport failure (%s); retrying once',
                        error.__class__.__name__,
                    )
                    continue
                try:
                    setattr(error, '_llm_attempt_count', attempts)
                    if not getattr(error, 'usage', None):
                        setattr(
                            error,
                            'usage',
                            _merge_usage(
                                failed_usage,
                                self._conservative_attempt_usage(kwargs),
                            ),
                        )
                    setattr(error, 'provider', self.provider)
                    setattr(error, 'model', self.model)
                    setattr(error, 'retry_count', max(0, attempts - 1))
                except Exception:
                    pass
                raise

    def _admit_budget(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int],
        *,
        logical_attempts: int = 1,
    ) -> Optional[str]:
        if not self.budget_run_id:
            return None
        from .llm_audit import reserve_llm_call_budget
        return reserve_llm_call_budget(
            self.budget_run_id,
            provider=self.provider,
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            # A timed-out transport may still have been processed upstream;
            # reserve for it even though usage metadata might never arrive.
            attempts=max(1, logical_attempts) * (1 + self.max_retries),
        )

    def _result_from_response(
        self,
        response: Any,
        *,
        latency_ms: float,
        retry_count: int = 0,
    ) -> LLMResult:
        choices = getattr(response, 'choices', None) or []
        choice = choices[0] if choices else None
        finish_reason = getattr(choice, 'finish_reason', None) if choice else None
        model, request_id, usage = _response_metadata(response, self.model)
        return LLMResult(
            content=_clean_chat_text(extract_chat_completion_text(response)),
            provider=self.provider,
            model=model,
            finish_reason=finish_reason,
            usage=usage,
            request_id=request_id,
            latency_ms=round(latency_ms, 3),
            retry_count=retry_count,
            usage_complete=_usage_has_billable_pair(usage),
        )

    def chat_result(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
        *,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> LLMResult:
        """Return text and safe response metadata for one completion."""

        started = time.perf_counter()
        response: Optional[Any] = None
        attempts = 0
        failed_usage: Dict[str, int] = {}
        budget_reservation_id: Optional[str] = None
        try:
            budget_reservation_id = self._admit_budget(
                messages,
                max_tokens,
                logical_attempts=1,
            )
            from ..observability import traced_span
            with traced_span(
                'llm.chat',
                attributes={
                    'provider': self.provider,
                    'model': self.model,
                    'run_id': self.budget_run_id or '',
                    'thinking': bool(thinking),
                },
            ):
                response, attempts, failed_usage = self._create_completion_with_retry(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    thinking=thinking,
                    reasoning_effort=reasoning_effort,
                )
            elapsed_ms = (time.perf_counter() - started) * 1000
            result = self._result_from_response(
                response,
                latency_ms=elapsed_ms,
                retry_count=max(0, attempts - 1),
            )
            if failed_usage:
                result = replace(
                    result,
                    usage=_merge_usage(failed_usage, result.usage),
                    usage_complete=False,
                )
            return replace(
                result,
                budget_reservation_id=budget_reservation_id,
            )
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000
            attempts = max(
                attempts,
                int(getattr(error, '_llm_attempt_count', 0) or 0),
            )
            error_model = self.model
            request_id = None
            usage: Dict[str, int] = dict(failed_usage)
            if response is not None:
                error_model, request_id, response_usage = _response_metadata(
                    response, self.model,
                )
                usage = _merge_usage(usage, response_usage)
            _attach_error_metadata(
                error,
                provider=self.provider,
                model=error_model,
                usage=usage,
                request_id=request_id,
                latency_ms=elapsed_ms,
                retry_count=max(0, attempts - 1),
                budget_reservation_id=budget_reservation_id,
                usage_complete=False,
            )
            raise

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        response_format: Optional[Dict[str, Any]] = None,
        *,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """Send a chat request and preserve the original string return type."""

        try:
            result = self.chat_result(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
        except Exception as error:
            if getattr(error, 'budget_reservation_id', None):
                from .llm_audit import record_llm_client_error
                record_llm_client_error(
                    self.budget_run_id,
                    'llm_client_legacy',
                    self,
                    error,
                )
            raise
        if result.budget_reservation_id:
            from .llm_audit import record_llm_result
            record_llm_result(
                self.budget_run_id,
                'llm_client_legacy',
                result,
            )
        return result.content

    def chat_json_result(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        max_attempts: int = 2,
        *,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> LLMResult:
        """Return one JSON object and metadata with one bounded regeneration.

        Empty, malformed, or truncated output consumes a content attempt. The
        same output token limit is retained on every attempt. An explicit
        provider rejection of ``response_format`` gets one capability fallback
        and does not consume the content regeneration allowance.
        """

        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")

        # Validate/copy caller-owned messages before reserving money so a local
        # shape error cannot strand an active reservation.
        guided_messages = _json_guided_messages(messages)
        budget_reservation_id = self._admit_budget(
            guided_messages,
            max_tokens,
            logical_attempts=max_attempts,
        )
        response_format: Optional[Dict[str, str]] = {"type": "json_object"}
        format_fallback_used = False
        last_error: Optional[LLMResponseError] = None
        overall_started = time.perf_counter()
        application_request_count = 0
        cumulative_usage: Dict[str, int] = {}
        cumulative_usage_complete = True
        last_request_id: Optional[str] = None
        last_model = self.model

        for attempt in range(1, max_attempts + 1):
            while True:
                try:
                    response, physical_attempts, failed_usage = self._create_completion_with_retry(
                        messages=guided_messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                        thinking=thinking,
                        reasoning_effort=reasoning_effort,
                    )
                except Exception as error:
                    application_request_count += max(
                        1, int(getattr(error, '_llm_attempt_count', 1) or 1)
                    )
                    if (
                        response_format is not None
                        and not format_fallback_used
                        and _is_response_format_unsupported(error)
                    ):
                        logger.warning(
                            "LLM provider rejected response_format; retrying once "
                            "with prompt-only JSON guidance"
                        )
                        response_format = None
                        format_fallback_used = True
                        continue
                    _attach_error_metadata(
                        error,
                        provider=self.provider,
                        model=last_model,
                        usage=cumulative_usage,
                        request_id=last_request_id,
                        latency_ms=(time.perf_counter() - overall_started) * 1000,
                        retry_count=max(0, application_request_count - 1),
                        budget_reservation_id=budget_reservation_id,
                        usage_complete=False,
                    )
                    raise
                application_request_count += physical_attempts
                cumulative_usage = _merge_usage(cumulative_usage, failed_usage)
                if failed_usage:
                    cumulative_usage_complete = False
                break

            try:
                result = self._result_from_response(
                    response,
                    latency_ms=(time.perf_counter() - overall_started) * 1000,
                    retry_count=application_request_count - 1,
                )
            except Exception as error:
                response_model, response_request_id, response_usage = _response_metadata(
                    response, self.model,
                )
                _attach_error_metadata(
                    error,
                    provider=self.provider,
                    model=response_model,
                    usage=_merge_usage(cumulative_usage, response_usage),
                    request_id=response_request_id or last_request_id,
                    latency_ms=(time.perf_counter() - overall_started) * 1000,
                    retry_count=max(0, application_request_count - 1),
                    budget_reservation_id=budget_reservation_id,
                    usage_complete=False,
                )
                raise
            cumulative_usage = _merge_usage(cumulative_usage, result.usage)
            cumulative_usage_complete = (
                cumulative_usage_complete
                and result.usage_complete is not False
                and _usage_has_billable_pair(result.usage)
            )
            last_request_id = result.request_id or last_request_id
            last_model = result.model or last_model
            result = replace(
                result,
                usage=cumulative_usage,
                request_id=last_request_id,
                model=last_model,
                budget_reservation_id=budget_reservation_id,
                usage_complete=cumulative_usage_complete,
            )
            try:
                value = self._parse_json_result(result)
                return replace(result, parsed_json=value)
            except LLMResponseError as error:
                last_error = error
                if attempt >= max_attempts:
                    _attach_error_metadata(
                        error,
                        provider=self.provider,
                        model=last_model,
                        usage=cumulative_usage,
                        request_id=last_request_id,
                        latency_ms=(time.perf_counter() - overall_started) * 1000,
                        retry_count=max(0, application_request_count - 1),
                        budget_reservation_id=budget_reservation_id,
                        usage_complete=cumulative_usage_complete,
                    )
                    raise
                logger.warning(
                    "LLM returned unusable JSON (finish_reason=%s); "
                    "retrying once with the same output token limit",
                    error.finish_reason or "unknown",
                )

        if last_error is not None:  # pragma: no cover - defensive loop guard
            _attach_error_metadata(
                last_error,
                provider=self.provider,
                model=last_model,
                usage=cumulative_usage,
                request_id=last_request_id,
                latency_ms=(time.perf_counter() - overall_started) * 1000,
                retry_count=max(0, application_request_count - 1),
                budget_reservation_id=budget_reservation_id,
                usage_complete=cumulative_usage_complete,
            )
            raise last_error
        error = LLMResponseError("LLM did not produce a JSON response")
        _attach_error_metadata(
            error,
            provider=self.provider,
            model=last_model,
            usage=cumulative_usage,
            request_id=last_request_id,
            latency_ms=(time.perf_counter() - overall_started) * 1000,
            retry_count=max(0, application_request_count - 1),
            budget_reservation_id=budget_reservation_id,
            usage_complete=cumulative_usage_complete,
        )
        raise error

    def chat_json(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4096,
        max_attempts: int = 2,
        *,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a JSON request and preserve the original dictionary return type."""

        try:
            result = self.chat_json_result(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                max_attempts=max_attempts,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
        except Exception as error:
            if getattr(error, 'budget_reservation_id', None):
                from .llm_audit import record_llm_client_error
                record_llm_client_error(
                    self.budget_run_id,
                    'llm_client_legacy',
                    self,
                    error,
                )
            raise
        if result.budget_reservation_id:
            from .llm_audit import record_llm_result
            record_llm_result(
                self.budget_run_id,
                'llm_client_legacy',
                result,
            )
        # ``chat_json_result`` guarantees a dictionary before returning.
        return result.parsed_json or {}

    @staticmethod
    def _parse_json_result(result: LLMResult) -> Dict[str, Any]:
        finish_reason = result.finish_reason
        if finish_reason in {'length', 'max_tokens'}:
            raise LLMResponseError(
                "LLM JSON output was truncated at the token limit",
                finish_reason=finish_reason,
            )
        if finish_reason not in {None, "stop"}:
            raise LLMResponseError(
                f"LLM JSON generation stopped unexpectedly ({finish_reason})",
                finish_reason=finish_reason,
            )

        content = result.content
        if not content:
            raise LLMResponseError(
                "LLM returned empty JSON content",
                finish_reason=finish_reason,
            )

        try:
            value = json.loads(content)
        except json.JSONDecodeError as strict_error:
            # Diagnose a complete leading value only to report the boundary;
            # no non-whitespace suffix is accepted or silently discarded.
            try:
                value, end = json.JSONDecoder().raw_decode(content)
            except json.JSONDecodeError:
                raise LLMResponseError(
                    "LLM returned invalid JSON "
                    f"(line {strict_error.lineno}, column {strict_error.colno})",
                    finish_reason=finish_reason,
                ) from strict_error

            trailing = content[end:].strip()
            if trailing:
                if _contains_additional_json_container(trailing):
                    raise LLMResponseError(
                        "LLM returned multiple JSON values",
                        finish_reason=finish_reason,
                    )
                raise LLMResponseError(
                    "LLM returned trailing content after the JSON object",
                    finish_reason=finish_reason,
                )

        if not isinstance(value, dict):
            raise LLMResponseError(
                "LLM JSON response must be a top-level JSON object",
                finish_reason=finish_reason,
            )
        return value

    @staticmethod
    def _parse_json_response(response: Any) -> Dict[str, Any]:
        """Backward-compatible parser used by older tests and integrations."""

        choices = getattr(response, 'choices', None) or []
        choice = choices[0] if choices else None
        finish_reason = getattr(choice, 'finish_reason', None) if choice else None
        result = LLMResult(
            content=_clean_chat_text(extract_chat_completion_text(response)),
            provider='unknown',
            model=str(getattr(response, 'model', None) or 'unknown'),
            finish_reason=finish_reason,
        )
        return LLMClient._parse_json_result(result)
