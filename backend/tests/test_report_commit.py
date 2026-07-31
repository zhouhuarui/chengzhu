"""Report bundle publication is visible only after its final commit marker."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask
from app.services import report_assembler
from app.services.report_assembler import load_report, publish_report
from app.utils.report_commit import (
    REPORT_COMMIT,
    REPORT_FILES,
    REPORT_PUBLISH_STARTED,
    report_bundle_is_committed,
)


def _run(tmp_path: Path, monkeypatch) -> tuple[ResearchTask, str, Path]:
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    task = ResearchTask(task_id='task_report_commit', requirement='报告交易测试')
    task.set_task_card({
        'deliverable': 'summary',
        'symbols': [{'code': '300750'}],
        'time_window': {'start': '2025-01-01', 'end': '2025-12-31'},
        'analysis_mode': 'direct',
    })
    run_id = task.create_run(task.task_card)
    return task, run_id, Path(task.run_folder(run_id))


@pytest.mark.parametrize('failed_write', [2, 3])
def test_partial_report_file_set_is_never_committed(
    tmp_path,
    monkeypatch,
    failed_write,
):
    task, run_id, run_folder = _run(tmp_path, monkeypatch)
    original = report_assembler._write_immutable_text
    calls = 0

    def fail_one(path: str, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_write:
            raise OSError('injected report write failure')
        original(path, content)

    monkeypatch.setattr(report_assembler, '_write_immutable_text', fail_one)
    report = {
        'task_id': task.task_id,
        'run_id': run_id,
        'title': '未完成报告',
        'sections': [],
        'markdown': '# 未完成报告',
    }

    with pytest.raises(OSError, match='injected'):
        publish_report(task.task_id, report, run_id=run_id)

    assert (run_folder / REPORT_PUBLISH_STARTED).is_file()
    assert not (run_folder / REPORT_COMMIT).exists()
    assert not report_bundle_is_committed(
        str(run_folder), task_id=task.task_id, run_id=run_id,
    )
    assert load_report(task.task_id, run_id=run_id) is None
    assert not any((run_folder / name).exists() for name in REPORT_FILES)


def test_complete_report_bundle_is_committed_before_read(tmp_path, monkeypatch):
    task, run_id, run_folder = _run(tmp_path, monkeypatch)
    report = {
        'task_id': task.task_id,
        'run_id': run_id,
        'title': '已完成报告',
        'sections': [],
        'markdown': '# 已完成报告',
    }

    publish_report(task.task_id, report, run_id=run_id)

    assert report_bundle_is_committed(
        str(run_folder), task_id=task.task_id, run_id=run_id,
    )
    assert load_report(task.task_id, run_id=run_id)['title'] == '已完成报告'
    root = Path(task.folder)
    assert report_bundle_is_committed(str(root), task_id=task.task_id)
    assert (root / REPORT_COMMIT).is_file()


def test_existing_commit_marker_is_never_rewritten_after_bundle_damage(
    tmp_path,
    monkeypatch,
):
    task, run_id, run_folder = _run(tmp_path, monkeypatch)
    report = {
        'task_id': task.task_id,
        'run_id': run_id,
        'title': '首次发布',
        'sections': [],
        'markdown': '# 首次发布',
    }
    publish_report(task.task_id, report, run_id=run_id)
    original_commit = (run_folder / REPORT_COMMIT).read_bytes()

    # Simulate post-publication disk damage.  Hash validation now fails, but
    # that must not turn a committed run back into a writable run.
    (run_folder / 'report.json').write_text('{"damaged": true}', encoding='utf-8')
    damaged_report = (run_folder / 'report.json').read_bytes()

    with pytest.raises(FileExistsError, match='提交标记'):
        publish_report(
            task.task_id,
            {**report, 'title': '禁止覆盖', 'markdown': '# 禁止覆盖'},
            run_id=run_id,
        )

    assert (run_folder / REPORT_COMMIT).read_bytes() == original_commit
    assert (run_folder / 'report.json').read_bytes() == damaged_report
