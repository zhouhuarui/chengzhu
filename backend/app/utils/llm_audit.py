"""Safe LLM call accounting: metadata and conservative CNY cost only."""

from __future__ import annotations

import os
import logging
import re
import uuid
from typing import Any, Dict, Optional

from . import db as dbutil


logger = logging.getLogger(__name__)


class LLMBudgetExceeded(RuntimeError):
    pass


def _safe_identifier(value: Any, *, limit: int = 200) -> Optional[str]:
    """Return a bounded metadata identifier, never an arbitrary object repr."""

    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    cleaned = re.sub(r'[^A-Za-z0-9._:/-]', '_', str(value).strip())
    return cleaned[:limit] or None


_SAFE_ERROR_CATEGORIES = {
    'api_error',
    'authentication_error',
    'conflict_error',
    'insufficient_resource',
    'invalid_parameter',
    'invalid_request_error',
    'not_found_error',
    'permission_error',
    'rate_limit_error',
    'resource_exhausted',
    'resource_unavailable',
    'server_error',
    'server_overloaded',
    'timeout_error',
    'unknown_parameter',
    'unsupported_parameter',
    'unsupported_value',
}


def _safe_error_category(value: Any) -> Optional[str]:
    """Expose only known protocol categories, never provider-returned prose."""

    token = _safe_identifier(value, limit=80)
    if token and token.lower() in _SAFE_ERROR_CATEGORIES:
        return token.lower()
    return None


def safe_error_summary(error: Exception) -> str:
    """Return an operational code that cannot echo prompts or credentials."""

    status = getattr(error, 'status_code', None)
    body = getattr(error, 'body', None)
    details = body.get('error', body) if isinstance(body, dict) else {}
    code = _safe_error_category(details.get('code')) if isinstance(details, dict) else None
    error_type = _safe_error_category(details.get('type')) if isinstance(details, dict) else None
    parts = [error.__class__.__name__]
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(f'status={status}')
    if error_type:
        parts.append(f'type={error_type}')
    if code:
        parts.append(f'code={code}')
    return ' '.join(parts)[:500]


def estimate_cost_cny(provider: str, model: str, usage: Dict[str, Any]) -> float:
    """Estimate list-price cost without inspecting prompts or response content.

    DeepSeek is conservatively costed at the announced 2x peak multiplier and
    treats all input as cache misses. Qwen3-VL uses the mainland list-price
    tiers. Environment overrides keep the estimate auditable when prices move.
    """

    prompt = max(0, int(usage.get('prompt_tokens') or 0))
    completion = max(0, int(usage.get('completion_tokens') or 0))
    provider_key = str(provider or '').lower()
    model_key = str(model or '').lower()

    if provider_key == 'deepseek' or 'deepseek' in model_key:
        if 'pro' in model_key:
            input_usd = float(os.environ.get('DEEPSEEK_PRO_INPUT_USD_PER_M', '0.435'))
            output_usd = float(os.environ.get('DEEPSEEK_PRO_OUTPUT_USD_PER_M', '0.87'))
        else:
            input_usd = float(os.environ.get('DEEPSEEK_FLASH_INPUT_USD_PER_M', '0.14'))
            output_usd = float(os.environ.get('DEEPSEEK_FLASH_OUTPUT_USD_PER_M', '0.28'))
        fx = float(os.environ.get('LLM_USD_CNY_RATE', '7.5'))
        peak = float(os.environ.get('DEEPSEEK_PRICE_MULTIPLIER', '2.0'))
        return round(((prompt * input_usd + completion * output_usd) / 1_000_000) * fx * peak, 6)

    if provider_key == 'dashscope' or 'qwen3-vl-plus' in model_key:
        if prompt <= 32_000:
            input_cny, output_cny = 1.0, 10.0
        elif prompt <= 128_000:
            input_cny, output_cny = 1.5, 15.0
        else:
            input_cny, output_cny = 3.0, 30.0
        return round((prompt * input_cny + completion * output_cny) / 1_000_000, 6)

    return 0.0


def estimate_message_tokens(messages: Any) -> int:
    """Conservative token admission estimate without retaining message text."""

    text_chars = 0
    image_count = 0

    def visit(value: Any) -> None:
        nonlocal text_chars, image_count
        if isinstance(value, str):
            if value.startswith('data:image/') and ';base64,' in value:
                image_count += 1
            else:
                # Chinese commonly approaches one token per character.  Using
                # the raw character count is intentionally conservative.
                text_chars += len(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(messages)
    return max(1, text_chars + image_count * 16_000 + 256)


def ensure_llm_call_budget(
    run_id: Optional[str],
    *,
    provider: str,
    model: str,
    messages: Any,
    max_tokens: Optional[int],
    attempts: int = 1,
    budget_cny: Optional[float] = None,
) -> float:
    """Read-only admission check including every outstanding reservation.

    Callers that own an :class:`LLMClient` should use its ``budget_run_id``;
    the client performs an atomic reservation.  This compatibility check is
    retained for injected/legacy clients and intentionally creates no orphan
    reservation because those callers cannot propagate a reservation ID.
    """

    if not run_id:
        return 0.0
    from ..config import Config

    budget = float(
        Config.LLM_COST_BUDGET_CNY if budget_cny is None else budget_cny
    )
    multiplier = max(1, int(attempts or 1))
    estimated = estimate_cost_cny(
        provider,
        model,
        {
            'prompt_tokens': estimate_message_tokens(messages) * multiplier,
            'completion_tokens': max(0, int(max_tokens or 0)) * multiplier,
        },
    )
    current = dbutil.llm_budget_totals(run_id)['committed_cny']
    if current + estimated > budget:
        raise LLMBudgetExceeded(
            f'llm_budget_admission_rejected current={current:.6f} estimated={estimated:.6f}'
        )
    return estimated


def reserve_llm_call_budget(
    run_id: Optional[str],
    *,
    provider: str,
    model: str,
    messages: Any,
    max_tokens: Optional[int],
    attempts: int = 1,
    budget_cny: Optional[float] = None,
) -> Optional[str]:
    """Atomically reserve a call's conservative maximum cost.

    The opaque ID is the only value propagated to ``LLMResult`` or a safe
    client exception.  It contains no prompt, credential, response content or
    reasoning data and is released only by a successful audit-log settlement.
    """

    if not run_id:
        return None
    from ..config import Config

    budget = float(
        Config.LLM_COST_BUDGET_CNY if budget_cny is None else budget_cny
    )
    multiplier = max(1, int(attempts or 1))
    estimated = estimate_cost_cny(
        provider,
        model,
        {
            'prompt_tokens': estimate_message_tokens(messages) * multiplier,
            'completion_tokens': max(0, int(max_tokens or 0)) * multiplier,
        },
    )
    reservation_id = f'llmres_{uuid.uuid4().hex}'
    if not dbutil.reserve_llm_budget(
        str(run_id),
        reservation_id,
        estimated,
        budget,
    ):
        totals = dbutil.llm_budget_totals(str(run_id))
        raise LLMBudgetExceeded(
            'llm_budget_admission_rejected '
            f'committed={totals["committed_cny"]:.6f} '
            f'estimated={estimated:.6f}'
        )
    return reservation_id


def _usage_has_billable_pair(usage: Any) -> bool:
    """Whether provider usage has both independently billable token fields."""

    if not isinstance(usage, dict):
        return False
    for key in ('prompt_tokens', 'completion_tokens'):
        value = usage.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            return False
    return True


def _usage_is_complete(subject: Any, usage: Any) -> bool:
    declared = getattr(subject, 'usage_complete', None)
    if not isinstance(declared, bool):
        declared = getattr(subject, '_llm_usage_complete', None)
    if isinstance(declared, bool):
        return declared
    return _usage_has_billable_pair(usage)


def record_llm_result(run_id: Optional[str], agent: str, result: Any) -> int:
    """Persist only fields exposed by LLMResult.to_metadata()."""

    metadata = result.to_metadata()
    usage = metadata.get('usage') or {}
    reservation_id = _safe_identifier(
        getattr(result, 'budget_reservation_id', None),
        limit=80,
    )
    return dbutil.insert_llm_call_log(
        run_id=run_id,
        provider=str(metadata.get('provider') or ''),
        model=str(metadata.get('model') or ''),
        agent=agent,
        finish_reason=metadata.get('finish_reason'),
        prompt_tokens=int(metadata.get('prompt_tokens') or 0),
        completion_tokens=int(metadata.get('completion_tokens') or 0),
        total_tokens=int(metadata.get('total_tokens') or 0),
        cost_cny=estimate_cost_cny(
            str(metadata.get('provider') or ''),
            str(metadata.get('model') or ''),
            usage,
        ),
        request_id=metadata.get('request_id'),
        latency_ms=int(metadata.get('latency_ms') or 0),
        retry_count=int(metadata.get('retry_count') or 0),
        ok=True,
        budget_reservation_id=reservation_id,
        settle_at_reserved_cost=bool(
            reservation_id and not _usage_is_complete(result, usage)
        ),
    )


def record_llm_error(
    run_id: Optional[str],
    agent: str,
    *,
    provider: str,
    model: str,
    error: Exception,
) -> int:
    """Record a safe bounded error; request bodies are never accepted here."""

    # Never persist Exception.__str__ here: SDK exceptions may echo request
    # bodies, which can contain prompts or Base64 image data.
    message = safe_error_summary(error)
    trusted_metadata = bool(getattr(error, '_llm_safe_metadata', False))
    usage = (getattr(error, 'usage', None) or {}) if trusted_metadata else {}
    reservation_id = (
        _safe_identifier(
            getattr(error, 'budget_reservation_id', None),
            limit=80,
        )
        if trusted_metadata else None
    )
    # Provider/model come from the configured client, never arbitrary
    # exception attributes.  Response-specific identifiers are accepted only
    # when the client marked them as safe operational metadata.
    error_provider = _safe_identifier(provider) or 'unknown'
    error_model = _safe_identifier(model) or 'unknown'
    return dbutil.insert_llm_call_log(
        run_id=run_id,
        provider=error_provider,
        model=error_model,
        agent=agent,
        finish_reason=(
            _safe_identifier(getattr(error, 'finish_reason', None))
            if trusted_metadata else None
        ),
        prompt_tokens=int(usage.get('prompt_tokens') or 0),
        completion_tokens=int(usage.get('completion_tokens') or 0),
        total_tokens=int(usage.get('total_tokens') or 0),
        cost_cny=estimate_cost_cny(error_provider, error_model, usage),
        request_id=(
            _safe_identifier(getattr(error, 'request_id', None))
            if trusted_metadata else None
        ),
        latency_ms=(
            int(getattr(error, 'latency_ms', 0) or 0)
            if trusted_metadata else 0
        ),
        retry_count=(
            int(getattr(error, 'retry_count', 0) or 0)
            if trusted_metadata else 0
        ),
        ok=False,
        error=message[:500],
        budget_reservation_id=reservation_id,
        settle_at_reserved_cost=bool(
            reservation_id and not _usage_is_complete(error, usage)
        ),
    )


def record_llm_client_error(
    run_id: Optional[str],
    agent: str,
    client: Any,
    error: Exception,
) -> int:
    """Best-effort accounting for a failed client call.

    Only the client's public provider/model labels and the safe operational
    fields attached to ``error`` are inspected.  Prompt text, credentials,
    raw responses and reasoning content are neither accepted nor persisted.
    With a real budget reservation, settlement failure is a hard accounting
    failure and is re-raised so the caller can take its existing explicit
    degradation path.  Calls without a reservation retain best-effort logging.
    """

    try:
        provider = _safe_identifier(getattr(client, 'provider', None)) or 'unknown'
        model = _safe_identifier(getattr(client, 'model', None)) or 'unknown'
        return record_llm_error(
            run_id,
            agent,
            provider=provider,
            model=model,
            error=error,
        )
    except Exception as audit_error:  # pragma: no cover - defensive I/O guard
        trusted_reservation = (
            bool(getattr(error, '_llm_safe_metadata', False))
            and bool(_safe_identifier(
                getattr(error, 'budget_reservation_id', None),
                limit=80,
            ))
        )
        if trusted_reservation:
            raise
        logger.warning(
            'Failed to persist safe LLM error metadata (%s)',
            audit_error.__class__.__name__,
        )
        return 0


def run_cost_summary(run_id: str) -> Dict[str, float]:
    """Return the budget ledger used for gates and user-facing cost display."""

    totals = dbutil.llm_budget_totals(run_id)
    return {
        key: round(float(totals.get(key) or 0), 6)
        for key in ('settled_cny', 'reserved_cny', 'committed_cny')
    }


def run_cost_cny(run_id: str) -> float:
    """Committed spend: settled logs plus every still-active reservation."""

    return run_cost_summary(run_id)['committed_cny']


def run_token_usage(run_id: str) -> int:
    return sum(int(row.get('total_tokens') or 0) for row in dbutil.list_llm_call_logs(run_id))
