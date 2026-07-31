"""工具层公共辅助。"""

from __future__ import annotations

import functools
import traceback
import time
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from typing import Any, Callable, Optional, TypeVar

from ..utils.logger import get_logger

logger = get_logger('chengzhu.tools')
T = TypeVar('T')
_TOOL_DEADLINE_EPOCH: ContextVar[Optional[float]] = ContextVar(
    'chengzhu_tool_deadline_epoch', default=None
)


def set_tool_deadline(deadline_epoch: Optional[float]) -> None:
    _TOOL_DEADLINE_EPOCH.set(deadline_epoch)


def _bounded_tool_timeout(timeout: float) -> float:
    deadline = _TOOL_DEADLINE_EPOCH.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError('tool run deadline exceeded')
    return max(0.05, min(float(timeout), remaining))


def run_with_timeout(fn: Callable[[], T], timeout: float = 30) -> T:
    bounded = _bounded_tool_timeout(timeout)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        result = fut.result(timeout=bounded)
    except FuturesTimeout as e:
        fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f'tool timed out after {bounded:.2f}s') from e
    except Exception:
        ex.shutdown(wait=True)
        raise
    ex.shutdown(wait=True)
    return result


def retry_call(fn: Callable[[], T], retries: int = 3, base_delay: float = 1.0) -> T:
    last: Optional[Exception] = None
    for i in range(retries):
        _bounded_tool_timeout(10**9)
        try:
            return fn()
        except Exception as e:
            last = e
            logger.warning(f'retry {i + 1}/{retries}: {e}')
            delay = base_delay * (2 ** i)
            deadline = _TOOL_DEADLINE_EPOCH.get()
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - time.time()))
            if delay <= 0:
                break
            time.sleep(delay)
    assert last is not None
    raise last


def to_iso(dt_like: Any) -> str:
    if dt_like is None:
        return ''
    if isinstance(dt_like, datetime):
        return dt_like.astimezone().isoformat(timespec='seconds')
    s = str(dt_like).strip()
    if not s or s.lower() == 'nan':
        return ''
    # 常见格式：20260428 / 2026-04-28 / 2026-04-28 18:30:00
    try:
        if len(s) == 8 and s.isdigit():
            d = datetime.strptime(s, '%Y%m%d')
            return d.strftime('%Y-%m-%dT00:00:00+08:00')
        if ' ' in s and len(s) >= 16:
            d = datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
            return d.strftime('%Y-%m-%dT%H:%M:%S+08:00')
        if len(s) >= 10 and s[4] == '-':
            d = datetime.strptime(s[:10], '%Y-%m-%d')
            return d.strftime('%Y-%m-%dT00:00:00+08:00')
    except Exception:
        pass
    return s


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except Exception:
        return default


def yi_yuan(v: Any) -> Optional[float]:
    """金额转为亿元，保留两位。"""
    f = safe_float(v)
    if f is None:
        return None
    return round(f / 1e8, 2)


def truncate(text: str, n: int = 800) -> str:
    text = (text or '').strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + '…'
