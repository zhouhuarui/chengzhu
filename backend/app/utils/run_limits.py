"""Absolute wall-clock limits shared by collection and analysis stages."""

from __future__ import annotations

import time
import queue
import threading
from datetime import datetime
from typing import Callable, Optional, TypeVar

from ..config import Config
from . import db as dbutil


class RunDeadlineExceeded(TimeoutError):
    pass


T = TypeVar('T')


def deadline_epoch_for_run(
    run_id: Optional[str],
    *,
    fallback_started_epoch: Optional[float] = None,
) -> float:
    started_epoch = fallback_started_epoch or time.time()
    if run_id:
        row = dbutil.get_task_run(run_id) or {}
        raw = row.get('started_at')
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw))
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                started_epoch = parsed.timestamp()
            except (TypeError, ValueError, OSError):
                pass
    return started_epoch + float(Config.PIPELINE_TIMEOUT_SECONDS)


def remaining_seconds(deadline_epoch: float) -> float:
    return float(deadline_epoch) - time.time()


def ensure_time_remaining(
    deadline_epoch: float,
    *,
    reserve_seconds: float = 0.0,
    stage: str = 'pipeline',
) -> float:
    remaining = remaining_seconds(deadline_epoch)
    if remaining <= float(reserve_seconds):
        raise RunDeadlineExceeded(f'{stage}: run_deadline_exceeded')
    return remaining


def bounded_timeout(
    deadline_epoch: float,
    maximum: float,
    *,
    reserve_seconds: float = 0.0,
    minimum: float = 1.0,
    stage: str = 'llm',
) -> float:
    remaining = ensure_time_remaining(
        deadline_epoch,
        reserve_seconds=reserve_seconds,
        stage=stage,
    )
    return max(float(minimum), min(float(maximum), remaining - reserve_seconds))


def call_with_deadline(
    operation: Callable[[], T],
    deadline_epoch: float,
    *,
    reserve_seconds: float = 0.0,
    maximum_seconds: Optional[float] = None,
    stage: str = 'operation',
) -> T:
    """Return by the absolute wall clock even if a dependency ignores timeout.

    Python cannot safely kill an arbitrary thread.  The detached worker is
    therefore restricted to side-effect-free provider/file reads, while the
    caller alone owns publication of run artifacts.  A late result is dropped
    and can never be assembled into a report.
    """

    remaining = ensure_time_remaining(
        deadline_epoch,
        reserve_seconds=reserve_seconds,
        stage=stage,
    ) - float(reserve_seconds)
    timeout = remaining
    if maximum_seconds is not None:
        timeout = min(timeout, max(0.001, float(maximum_seconds)))
    result_queue: 'queue.Queue[tuple[bool, object]]' = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, operation()), block=False)
        except BaseException as error:  # re-raised in the owning thread
            result_queue.put((False, error), block=False)

    worker = threading.Thread(
        target=invoke,
        name=f'bounded-{stage}',
        daemon=True,
    )
    worker.start()
    try:
        ok, value = result_queue.get(timeout=max(0.001, timeout))
    except queue.Empty as error:
        raise RunDeadlineExceeded(f'{stage}: run_deadline_exceeded') from error
    if not ok:
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError(f'{stage}: operation_failed')
    ensure_time_remaining(
        deadline_epoch,
        reserve_seconds=reserve_seconds,
        stage=stage,
    )
    return value  # type: ignore[return-value]


__all__ = [
    'RunDeadlineExceeded', 'bounded_timeout', 'call_with_deadline',
    'deadline_epoch_for_run', 'ensure_time_remaining', 'remaining_seconds',
]
