"""Compensation helpers for multi-resource run admission.

Creating a run spans an immutable filesystem snapshot, SQLite metadata and the
task-level live pointer.  Those resources cannot share one transaction, so an
exception after the first durable write must leave either no run at all or a
terminal, inspectable failed run -- never an active row without a worker.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from ..models.research_task import ResearchTask, ResearchTaskStatus
from . import db as dbutil
from .llm_audit import safe_error_summary


_TERMINAL_STATUSES = {
    ResearchTaskStatus.COMPLETED.value,
    ResearchTaskStatus.COMPLETED_PARTIAL.value,
    ResearchTaskStatus.FAILED.value,
}


def compensate_failed_run_admission(
    task_id: str,
    run_id: Optional[str],
    error: Exception,
    *,
    message: str,
) -> str:
    """Make a partially admitted run safe and return a redacted error code.

    Once a ``task_run`` row exists it is retained as an audit record and moved
    to ``failed``.  This also preserves any Planner cost metadata atomically
    migrated to that run.  If admission failed before the row existed, only the
    freshly-created, unpublished run directory is removed.
    """

    safe_error = safe_error_summary(error)
    if not run_id:
        return safe_error

    row = None
    row_lookup_succeeded = False
    try:
        row = dbutil.get_task_run(run_id)
        row_lookup_succeeded = True
    except Exception:
        # Continue with task-state compensation even if the ledger is
        # temporarily unavailable; the original admission request still fails.
        row = None

    if row is not None:
        try:
            if str(row.get('status') or '') not in _TERMINAL_STATUSES:
                dbutil.finish_task_run(run_id, ResearchTaskStatus.FAILED.value)
        except Exception:
            pass
        try:
            debate = dbutil.get_debate_run(run_id)
            if debate and str(debate.get('status') or '') not in {'completed', 'failed'}:
                dbutil.finish_debate_run(run_id, 'failed', error=safe_error)
        except Exception:
            pass
    elif row_lookup_succeeded:
        # No DB ownership was published, so this directory is an unpublished
        # by-product of the current request rather than a historical run.
        try:
            task = ResearchTask.load(task_id)
            if task and task.current_run_id != run_id:
                folder = task.run_folder(run_id)
                if os.path.isdir(folder) and not os.path.islink(folder):
                    shutil.rmtree(folder)
        except Exception:
            pass

    try:
        task = ResearchTask.load(task_id)
        if task and task.current_run_id == run_id:
            task.error = safe_error
            task.collect_failures = []
            task.progress_detail = {
                'stage': ResearchTaskStatus.FAILED.value,
                'analysis_mode': (task.task_card or {}).get('analysis_mode', 'direct'),
                'run_id': run_id,
                'report_ready': False,
                'admission_failed': True,
            }
            task.set_status(
                ResearchTaskStatus.FAILED,
                f'{message}: {safe_error}',
                progress=100,
            )
    except Exception:
        pass
    return safe_error


__all__ = ['compensate_failed_run_admission']
