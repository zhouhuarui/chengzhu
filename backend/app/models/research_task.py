"""ResearchTask 状态机 + JSON 持久化（仿 MiroFish project.py）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.report_commit import report_bundle_is_committed
from .task_card import TaskCard


class ResearchTaskStatus(str, Enum):
    CREATED = 'created'
    PARSING = 'parsing'
    AWAITING_CONFIRM = 'awaiting_confirm'
    COLLECTING = 'collecting'
    INGESTING = 'ingesting'
    NORMALIZING = 'normalizing'
    DEBATING = 'debating'
    ADJUDICATING = 'adjudicating'
    ANALYZING = 'analyzing'
    REVIEWING = 'reviewing'
    ASSEMBLING = 'assembling'
    COMPLETED = 'completed'
    COMPLETED_PARTIAL = 'completed_partial'
    FAILED = 'failed'


class ResearchTask:
    def __init__(
        self,
        task_id: Optional[str] = None,
        requirement: str = '',
        user_id: str = 'default',
    ):
        self.task_id = task_id or f'task_{uuid.uuid4().hex[:12]}'
        self.user_id = user_id
        self.requirement = requirement
        self.status = ResearchTaskStatus.CREATED
        self.task_card: Optional[Dict[str, Any]] = None
        self.progress = 0
        self.message = ''
        self.error: Optional[str] = None
        self.collect_failures: List[Dict[str, Any]] = []
        self.created_at = datetime.now().isoformat(timespec='seconds')
        self.updated_at = self.created_at
        self.progress_detail: Dict[str, Any] = {}
        # 旧 task.json 不包含此字段，load 时会自动回退为 None。
        self.current_run_id: Optional[str] = None

    @property
    def folder(self) -> str:
        return os.path.join(Config.UPLOAD_FOLDER, 'tasks', self.task_id)

    def ensure_folder(self) -> str:
        os.makedirs(self.folder, exist_ok=True)
        os.makedirs(os.path.join(self.folder, 'files'), exist_ok=True)
        os.makedirs(os.path.join(self.folder, 'evidence'), exist_ok=True)
        os.makedirs(os.path.join(self.folder, 'sections'), exist_ok=True)
        os.makedirs(self.runs_folder, exist_ok=True)
        return self.folder

    @property
    def runs_folder(self) -> str:
        return os.path.join(self.folder, 'runs')

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        value = str(run_id or '')
        if not value or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
            raise ValueError('非法 run_id')
        return value

    def run_folder(self, run_id: Optional[str] = None) -> str:
        """返回任务内的 run 产物目录，并阻断路径穿越。"""
        resolved = self._validate_run_id(run_id or self.current_run_id or '')
        return os.path.join(self.runs_folder, resolved)

    def has_run(self, run_id: str) -> bool:
        try:
            return os.path.isdir(self.run_folder(run_id))
        except ValueError:
            return False

    def create_run(
        self,
        task_card: Optional[Dict[str, Any]] = None,
        *,
        deadline_epoch: Optional[float] = None,
        publish_current: bool = True,
    ) -> str:
        """为一次执行生成唯一 run_id 并写入不可变的输入快照。"""
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        run_id = f'run_{stamp}_{uuid.uuid4().hex[:8]}'
        folder = self.run_folder(run_id)
        try:
            for name in ('evidence', 'sections', 'debate', 'files'):
                os.makedirs(os.path.join(folder, name), exist_ok=False)

            # A run owns an input-file snapshot as well as a TaskCard snapshot.
            # Later uploads or edits under the task root can therefore never
            # change an already confirmed run's evidence.
            source_files = os.path.join(self.folder, 'files')
            target_files = os.path.join(folder, 'files')
            if os.path.isdir(source_files):
                for name in sorted(os.listdir(source_files)):
                    source = os.path.join(source_files, name)
                    if os.path.isfile(source) and not os.path.islink(source):
                        target = os.path.join(target_files, name)
                        if deadline_epoch is None:
                            shutil.copy2(source, target)
                        else:
                            from ..utils.run_limits import call_with_deadline
                            call_with_deadline(
                                lambda source=source, target=target: shutil.copy2(source, target),
                                deadline_epoch,
                                stage='run_input_snapshot',
                            )

            snapshot = {
                'run_id': run_id,
                'task_id': self.task_id,
                'task_card': task_card if task_card is not None else (self.task_card or {}),
                'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            }
            if deadline_epoch is not None:
                from ..utils.run_limits import ensure_time_remaining
                ensure_time_remaining(deadline_epoch, stage='run_snapshot_publish')
            self._atomic_json_write(os.path.join(folder, 'run.json'), snapshot)
        except Exception:
            # Only the freshly generated, validated run directory is removed.
            if os.path.isdir(folder):
                shutil.rmtree(folder)
            raise
        if publish_current:
            self.current_run_id = run_id
            self.save()
        return run_id

    def begin_run(
        self,
        run_id: str,
        status: ResearchTaskStatus,
        message: str,
        progress: int,
        *,
        analysis_mode: Optional[str] = None,
    ) -> None:
        """Atomically publish a clean task-level state for one admitted run.

        ``task.json`` is the live status view for the current run.  Reusing its
        mutable fields without resetting them leaks the previous run's error,
        report marker and debate counters into a newly admitted execution.
        Confirm/rerun callers hold the shared task lock and call this only after
        the run directory and database rows have been created.
        """

        resolved = self._validate_run_id(run_id)
        if not self.has_run(resolved):
            raise ValueError('run 不存在或不属于该任务')
        mode = str(
            analysis_mode
            or (self.task_card or {}).get('analysis_mode')
            or 'direct'
        )
        self.current_run_id = resolved
        self.status = status
        self.message = message
        self.progress = int(progress)
        self.error = None
        self.collect_failures = []
        self.progress_detail = {
            'stage': status.value,
            'analysis_mode': mode,
            'run_id': resolved,
            'report_ready': False,
        }
        self.updated_at = datetime.now().isoformat(timespec='seconds')
        self.save()

    @staticmethod
    def _atomic_json_write(path: str, data: Dict[str, Any]) -> None:
        tmp_path = f'{path}.tmp-{uuid.uuid4().hex}'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def set_status(self, status: ResearchTaskStatus, message: str = '', progress: Optional[int] = None):
        self.status = status
        if message:
            self.message = message
        if progress is not None:
            self.progress = progress
        self.updated_at = datetime.now().isoformat(timespec='seconds')
        self.save()

    def set_task_card(self, card: TaskCard | Dict[str, Any]):
        self.task_card = card.to_dict() if isinstance(card, TaskCard) else card
        if isinstance(self.task_card, dict):
            self.task_card.setdefault('analysis_mode', 'direct')
            self.task_card.setdefault('execution_mode', 'agentteams')
        self.save()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'task_id': self.task_id,
            'user_id': self.user_id,
            'requirement': self.requirement,
            'status': self.status.value,
            'task_card': self.task_card,
            'progress': self.progress,
            'message': self.message,
            'error': self.error,
            'collect_failures': self.collect_failures,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'progress_detail': self.progress_detail,
            'current_run_id': self.current_run_id,
        }

    def save(self) -> None:
        self.ensure_folder()
        path = os.path.join(self.folder, 'task.json')
        self._atomic_json_write(path, self.to_dict())

    @classmethod
    def load(cls, task_id: str) -> Optional['ResearchTask']:
        path = os.path.join(Config.UPLOAD_FOLDER, 'tasks', task_id, 'task.json')
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        t = cls(task_id=data['task_id'], requirement=data.get('requirement', ''), user_id=data.get('user_id', 'default'))
        t.status = ResearchTaskStatus(data.get('status', 'created'))
        t.task_card = data.get('task_card')
        if isinstance(t.task_card, dict):
            t.task_card.setdefault('analysis_mode', 'direct')
            t.task_card.setdefault('execution_mode', 'agentteams')
        t.progress = data.get('progress', 0)
        t.message = data.get('message', '')
        t.error = data.get('error')
        t.collect_failures = data.get('collect_failures') or []
        t.created_at = data.get('created_at', t.created_at)
        t.updated_at = data.get('updated_at', t.updated_at)
        t.progress_detail = data.get('progress_detail') or {}
        t.current_run_id = data.get('current_run_id') or None
        return t

    @classmethod
    def list_tasks(cls, limit: int = 20) -> List['ResearchTask']:
        root = os.path.join(Config.UPLOAD_FOLDER, 'tasks')
        if not os.path.isdir(root):
            return []
        ids = sorted(os.listdir(root), reverse=True)
        out: List[ResearchTask] = []
        for tid in ids:
            t = cls.load(tid)
            if t:
                out.append(t)
            if len(out) >= limit:
                break
        return out


def resolve_task_run_id(
    task_id: str,
    requested_run_id: Optional[str] = None,
    *,
    allow_legacy: bool = True,
) -> Optional[str]:
    """解析 API 的 run_id：显式值须属于任务，省略时取 latest。"""
    task = ResearchTask.load(task_id)
    if requested_run_id:
        try:
            requested = ResearchTask._validate_run_id(requested_run_id)
        except ValueError:
            return None
        if allow_legacy and requested == task_id:
            return requested
        if task and task.has_run(requested):
            return requested
        try:
            from ..utils import db as dbutil
            row = dbutil.get_task_run(requested)
            if row and row.get('task_id') == task_id:
                return requested
        except Exception:
            pass
        return None

    # Omitted run_id means latest *published report*, not the newest active
    # run. The task root is an atomic latest alias written only after report
    # publication, so a collecting/failed run cannot hide the prior result.
    root = os.path.join(Config.UPLOAD_FOLDER, 'tasks', task_id)
    latest_report = os.path.join(root, 'report.json')
    if report_bundle_is_committed(root, task_id=task_id):
        try:
            with open(latest_report, 'r', encoding='utf-8') as handle:
                report_meta = json.load(handle)
        except (OSError, ValueError, TypeError):
            report_meta = {}
        published_run = str(report_meta.get('run_id') or '')
        if task and published_run and task.has_run(published_run):
            published_folder = task.run_folder(published_run)
            if report_bundle_is_committed(
                published_folder,
                task_id=task_id,
                run_id=published_run,
            ):
                return published_run
        return task_id
    try:
        from ..utils import db as dbutil
        for row in dbutil.list_task_runs(task_id, limit=100):
            candidate = str(row['run_id'])
            if task and task.has_run(candidate) and report_bundle_is_committed(
                task.run_folder(candidate),
                task_id=task_id,
                run_id=(candidate if candidate != task_id else None),
            ):
                return candidate
    except Exception:
        pass
    if task and task.current_run_id and task.has_run(task.current_run_id):
        return task.current_run_id
    return task_id if allow_legacy and os.path.isdir(root) else None


def task_artifact_folder(task_id: str, run_id: Optional[str]) -> str:
    root = os.path.join(Config.UPLOAD_FOLDER, 'tasks', task_id)
    if not run_id or run_id == task_id:
        return root
    validated = ResearchTask._validate_run_id(run_id)
    return os.path.join(root, 'runs', validated)


def task_card_for_run(task: ResearchTask, run_id: Optional[str]) -> Dict[str, Any]:
    """Read the immutable TaskCard input for a run, with legacy fallback."""

    if not run_id or run_id == task.task_id:
        return dict(task.task_card or {})
    path = os.path.join(task.run_folder(run_id), 'run.json')
    with open(path, 'r', encoding='utf-8') as handle:
        snapshot = json.load(handle)
    if snapshot.get('task_id') != task.task_id or snapshot.get('run_id') != run_id:
        raise ValueError('run 输入快照与任务不匹配')
    card = snapshot.get('task_card')
    if not isinstance(card, dict):
        raise ValueError('run 输入快照缺少 task_card')
    return dict(card)
