"""Process- and host-local serialization for creating runs of one task.

The in-memory lock closes races between Flask threads.  ``flock`` additionally
serializes workers which share the same uploads volume, so the active-status
check and run creation can be treated as one critical section.
"""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterator

from ..config import Config


_locks_guard = threading.Lock()
_task_locks: Dict[str, threading.RLock] = {}


def _thread_lock(task_id: str) -> threading.RLock:
    key = str(task_id or '')
    with _locks_guard:
        lock = _task_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _task_locks[key] = lock
        return lock


@contextmanager
def task_run_lock(task_id: str) -> Iterator[None]:
    """Exclusively guard run admission and creation for ``task_id``.

    Lock files use a hash rather than the caller-provided identifier, avoiding
    path traversal and keeping the lock location independent from task deletion.
    """

    task_key = str(task_id or '')
    if not task_key:
        raise ValueError('task_id 不能为空')
    lock = _thread_lock(task_key)
    with lock:
        lock_dir = os.path.join(Config.UPLOAD_FOLDER, '.task_run_locks')
        os.makedirs(lock_dir, exist_ok=True)
        digest = hashlib.sha256(task_key.encode('utf-8')).hexdigest()
        path = os.path.join(lock_dir, f'{digest}.lock')
        with open(path, 'a+b') as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                # Windows/test fallbacks still retain the process-local lock.
                fcntl = None
            try:
                yield
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass


__all__ = ['task_run_lock']
