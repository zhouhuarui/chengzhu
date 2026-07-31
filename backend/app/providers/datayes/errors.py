"""Datayes Provider 的安全异常类型。"""

from __future__ import annotations

import re
from typing import Optional


class DatayesError(RuntimeError):
    """所有可对上层暴露的 Datayes 错误基类。"""


class EndpointNotAllowed(DatayesError):
    pass


class ParameterValidationError(DatayesError):
    pass


class AuthenticationError(DatayesError):
    pass


class RateLimitError(DatayesError):
    pass


class ServiceUnavailableError(DatayesError):
    pass


class WarehouseUnavailableError(DatayesError):
    pass


def redact_secret(message: object, token: Optional[str] = None) -> str:
    """移除 Token、Authorization header 和 URL 中的潜在凭证。"""
    text = str(message or '')
    if token:
        text = text.replace(token, '[REDACTED]')
    text = re.sub(
        r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;\]}]+',
        r'\1Bearer [REDACTED]',
        text,
    )
    text = re.sub(
        r'(?i)(?:token|api[_-]?key)=([^&\s]+)',
        lambda m: m.group(0).split('=', 1)[0] + '=[REDACTED]',
        text,
    )
    return text
