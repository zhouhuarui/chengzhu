"""Bound and redact durable team-event payloads before they reach SQLite."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEYS = {
    'prompt', 'system_prompt', 'messages', 'api_key', 'apikey',
    'authorization', 'cookie', 'password', 'secret', 'credential',
    'access_token', 'refresh_token', 'reasoning', 'reasoning_content',
    'chain_of_thought', 'thought', 'raw_thought', 'raw_content',
    'private_data', 'personal_data', 'pii', 'raw_document', 'document_text',
    'source_text', 'full_text', 'base64', 'image_bytes', 'file_bytes',
}
_MAX_DEPTH = 8
_MAX_ITEMS = 100
_MAX_STRING = 4000


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace('-', '_')
    if normalized in _SENSITIVE_KEYS:
        return True
    if normalized.endswith((
        '_api_key', '_access_token', '_refresh_token', '_password',
        '_secret', '_authorization', '_credential', '_credentials',
        '_prompt', '_messages', '_reasoning', '_thought',
        '_private_data', '_personal_data', '_pii', '_raw_document',
        '_document_text', '_source_text', '_full_text', '_base64',
        '_image_bytes', '_file_bytes',
    )):
        return True
    return 'chain_of_thought' in normalized


def _redact_string(value: str) -> str:
    lowered = value.lower()
    if 'data:image/' in lowered and 'base64,' in lowered:
        return '[image payload omitted]'
    # Matrix/team events have no legitimate reason to contain an opaque
    # binary payload.  Catch unlabelled Base64 blobs without treating normal
    # short IDs, hashes or prose as sensitive.
    compact = re.sub(r'\s+', '', value)
    if (
        len(compact) >= 512
        and len(compact) % 4 == 0
        and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', compact)
    ):
        return '[binary payload omitted]'
    value = re.sub(r'(?i)bearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
    value = re.sub(
        r'(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)'
        r'\s*[:=]\s*[^\s,;]+',
        r'\1=[REDACTED]',
        value,
    )
    if len(value) > _MAX_STRING:
        return value[:_MAX_STRING] + '…[truncated]'
    return value


def redact_event_payload(value: Any, *, _depth: int = 0, _key: str = '') -> Any:
    """Return a JSON-safe, bounded value with prompts, CoT and secrets removed."""

    if _sensitive_key(_key):
        return '[REDACTED]'
    if _depth >= _MAX_DEPTH:
        return '[max depth omitted]'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result['_truncated'] = True
                break
            safe_key = str(key)[:200]
            result[safe_key] = redact_event_payload(
                item,
                _depth=_depth + 1,
                _key=safe_key,
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [
            redact_event_payload(item, _depth=_depth + 1)
            for item in value[:_MAX_ITEMS]
        ]
        if len(value) > _MAX_ITEMS:
            items.append('[truncated]')
        return items
    return _redact_string(str(value))
