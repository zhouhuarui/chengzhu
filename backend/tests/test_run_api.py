"""run 隔离、证据快照与兼容 API 的离线验收。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus, task_card_for_run
from app.models.task_card import TaskCard
from app.services.evidence_store import EvidenceStore
from app.tools.schema import EvidenceCard
from app.utils import db as dbmod


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'chengzhu.db'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    monkeypatch.setattr(Config, 'DATAYES_ENABLED', False)
    dbmod.init_db()
    yield tmp_path
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None


def _card(title: str = '宁德时代 H1 报告') -> EvidenceCard:
    return EvidenceCard(
        source_type='financial_report',
        title=title,
        url='https://example.test/report/300750-H1',
        publish_time='2026-07-01T09:00:00+08:00',
        source_name='上市公司公告',
        symbol='300750',
        excerpt='营业收入 100 亿元',
        structured={'period': 'H1', 'revenue_yi': 100},
        reliability=5,
        fetch_tool='fixture',
    )


def _task(task_id: str = 'task_run_fixture', mode: str = 'direct') -> ResearchTask:
    task = ResearchTask(task_id=task_id, requirement='宁德时代摘要')
    task.set_task_card({
        'deliverable': 'summary',
        'symbols': [{'code': '300750', 'name': '宁德时代'}],
        'time_window': {'start': '2026-01-01', 'end': '2026-07-31'},
        'info_types': ['financial_report'],
        'analysis_mode': mode,
    })
    return task


def _write_legacy_card(root: Path, task_id: str, card: EvidenceCard) -> None:
    folder = root / 'tasks' / task_id / 'evidence'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'financial.jsonl').write_text(
        json.dumps(card.to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )


def test_task_card_analysis_mode_is_backward_compatible_and_validated():
    legacy = TaskCard.from_dict({
        'deliverable': 'summary',
        'symbols': [{'code': '300750'}],
        'time_window': {'start': '2026-01-01', 'end': '2026-07-31'},
    })
    assert legacy.analysis_mode == 'direct'
    assert legacy.validate() == []

    tracking = TaskCard.from_dict({
        **legacy.to_dict(),
        'deliverable': 'tracking',
        'analysis_mode': 'evidence_debate',
    })
    assert any('tracking' in error for error in tracking.validate())


def test_run_creation_and_frozen_evidence_are_immutable(isolated_runtime):
    task = _task()
    _write_legacy_card(isolated_runtime, task.task_id, _card())
    upload = Path(task.folder) / 'files' / 'input.txt'
    upload.write_text('run-one-input', encoding='utf-8')
    run_id = task.create_run()
    first_card = task_card_for_run(task, run_id)
    upload.write_text('later-edit', encoding='utf-8')
    task.task_card['focus_points'] = ['后来修改']
    task.save()
    assert task_card_for_run(task, run_id) == first_card
    assert (Path(task.run_folder(run_id)) / 'files' / 'input.txt').read_text(
        encoding='utf-8',
    ) == 'run-one-input'

    legacy = EvidenceStore(task.task_id)
    assert len(legacy.cards) == 1
    assert legacy.search('完全不相关的查询') == []
    uid = legacy.cards[0].evidence_uid
    snapshot = legacy.freeze_to_run(run_id)
    assert Path(snapshot).is_file()

    _write_legacy_card(isolated_runtime, task.task_id, _card('被修改的根目录证据'))
    frozen = EvidenceStore(task.task_id, run_id=run_id)
    assert frozen.cards[0].title == '宁德时代 H1 报告'
    assert frozen.cards[0].evidence_uid == uid
    assert frozen.sources_index()[0]['display_id'] == 'E1'
    with pytest.raises(FileExistsError):
        legacy.freeze_to_run(run_id)
    assert EvidenceStore(task.task_id, run_id=task.task_id).cards[0].title == '被修改的根目录证据'

    second_run = task.create_run()
    assert second_run != run_id
    assert EvidenceStore(task.task_id, run_id=second_run).cards == []
    assert ResearchTask.load(task.task_id).current_run_id == second_run


def test_debate_and_llm_metadata_crud(isolated_runtime):
    task = _task(mode='evidence_debate')
    run_id = task.create_run()
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'collecting')
    dbmod.insert_debate_run(run_id, task.task_id, 'debating')
    dbmod.update_debate_run(
        run_id,
        current_round=2,
        current_role='growth_change',
        claim_count=3,
        audit_failure_count=1,
    )
    dbmod.insert_llm_call_log(
        run_id,
        'deepseek',
        'deepseek-v4-pro',
        agent='judge',
        finish_reason='stop',
        prompt_tokens=10,
        completion_tokens=5,
        request_id='request-safe-metadata',
        latency_ms=120,
        retry_count=1,
    )

    assert dbmod.list_task_runs(task.task_id)[0]['run_id'] == run_id
    debate = dbmod.get_debate_run(run_id)
    assert debate['current_round'] == 2
    assert debate['audit_failure_count'] == 1
    llm_log = dbmod.list_llm_call_logs(run_id)[0]
    assert llm_log['total_tokens'] == 15
    assert llm_log['request_id'] == 'request-safe-metadata'
    assert 'prompt' not in llm_log


def test_staging_evidence_is_not_published_and_latest_report_survives_new_run(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_publish_marker')
    run_one = task.create_run()
    run_one_evidence = Path(task.run_folder(run_one)) / 'evidence' / 'financial.jsonl'
    run_one_evidence.write_text(
        json.dumps(_card('run-one').to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    EvidenceStore(task.task_id, run_id=run_one, allow_staging=True).freeze_to_run(run_one)
    report = {'run_id': run_one, 'title': 'run one', 'sections': [], 'markdown': '# run one'}
    (Path(task.run_folder(run_one)) / 'report.json').write_text(
        json.dumps(report), encoding='utf-8',
    )
    (Path(task.folder) / 'report.json').write_text(json.dumps(report), encoding='utf-8')

    run_two = task.create_run()
    (Path(task.run_folder(run_two)) / 'evidence' / 'partial.jsonl').write_text(
        json.dumps(_card('partial-run-two').to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    from app import create_app
    client = create_app(Config).test_client()

    staging = client.get(
        f'/api/task/{task.task_id}/evidence', query_string={'run_id': run_two},
    )
    assert staging.status_code == 202
    assert staging.get_json()['data']['items'] == []
    latest_evidence = client.get(f'/api/task/{task.task_id}/evidence')
    assert latest_evidence.status_code == 200
    assert latest_evidence.get_json()['data']['run_id'] == run_one
    assert latest_evidence.get_json()['data']['items'][0]['title'] == 'run-one'
    latest_report = client.get(f'/api/report/{task.task_id}')
    assert latest_report.status_code == 200
    assert latest_report.get_json()['data']['run_id'] == run_one


def test_incomplete_report_transaction_is_not_latest_or_report_ready(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_incomplete_report_commit', mode='direct')
    published_run = task.create_run(task.task_card)
    dbmod.insert_task_run(
        published_run, task.task_id, task.task_card, 'completed',
    )
    published = {
        'run_id': published_run,
        'title': 'last committed report',
        'sections': [],
        'markdown': '# committed',
    }
    (Path(task.run_folder(published_run)) / 'report.json').write_text(
        json.dumps(published), encoding='utf-8',
    )
    (Path(task.folder) / 'report.json').write_text(
        json.dumps(published), encoding='utf-8',
    )

    incomplete_run = task.create_run(task.task_card)
    dbmod.insert_task_run(
        incomplete_run, task.task_id, task.task_card, 'assembling',
    )
    incomplete_folder = Path(task.run_folder(incomplete_run))
    (incomplete_folder / 'report_publish_started.json').write_text(
        json.dumps({
            'task_id': task.task_id,
            'run_id': incomplete_run,
            'transaction_id': 'unfinished',
        }),
        encoding='utf-8',
    )
    (incomplete_folder / 'report.json').write_text(
        json.dumps({
            'run_id': incomplete_run,
            'title': 'must remain invisible',
            'sections': [],
            'markdown': '# incomplete',
        }),
        encoding='utf-8',
    )

    from app import create_app
    client = create_app(Config).test_client()
    latest = client.get(f'/api/report/{task.task_id}')
    runs = client.get(f'/api/task/{task.task_id}/runs').get_json()['data']

    assert latest.status_code == 200
    assert latest.get_json()['data']['run_id'] == published_run
    by_run = {item['run_id']: item for item in runs}
    assert by_run[published_run]['report_ready'] is True
    assert by_run[incomplete_run]['report_ready'] is False

    # AgentTeams admission compensation must not mistake a started-only bundle
    # for a successfully generated partial report.  There is intentionally no
    # legacy realtime ``run_full_pipeline`` entrypoint in the task API.
    from app.utils.run_admission import compensate_failed_run_admission
    current = ResearchTask.load(task.task_id)
    current.begin_run(
        incomplete_run,
        ResearchTaskStatus.ASSEMBLING,
        '装配中',
        97,
        analysis_mode='direct',
    )
    compensate_failed_run_admission(
        task.task_id,
        incomplete_run,
        RuntimeError('injected'),
        message='AgentTeams 派发失败',
    )
    failed = ResearchTask.load(task.task_id)
    assert failed.status is ResearchTaskStatus.FAILED
    assert failed.progress_detail['report_ready'] is False
    assert dbmod.get_task_run(incomplete_run)['status'] == 'failed'


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


class _FailingStartThread(_NoopThread):
    def start(self):
        raise RuntimeError('worker unavailable')


class _InlineThread(_NoopThread):
    def start(self):
        target = self.kwargs.get('target') or self.args[0]
        args = self.kwargs.get('args', ())
        kwargs = self.kwargs.get('kwargs', {})
        target(*args, **kwargs)


def test_confirm_and_run_aware_artifact_apis(isolated_runtime, monkeypatch):
    task = _task(mode='direct')
    from app.api import task as task_api
    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)

    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    client = app.test_client()

    response = client.post(
        f'/api/task/{task.task_id}/confirm',
        json={'analysis_mode': 'evidence_debate'},
    )
    assert response.status_code == 200
    run_id = response.get_json()['data']['run_id']
    assert run_id.startswith('run_')
    assert dbmod.get_task_run(run_id)['task_id'] == task.task_id
    assert dbmod.get_debate_run(run_id)['status'] == 'pending'
    status = client.get(f'/api/task/{task.task_id}/status').get_json()['data']
    assert status['run_id'] == run_id
    assert status['progress_detail']['debate']['claim_count'] == 0

    run_folder = isolated_runtime / 'tasks' / task.task_id / 'runs' / run_id
    _write_legacy_card(isolated_runtime, task.task_id, _card())
    EvidenceStore(task.task_id).freeze_to_run(run_id)
    (run_folder / 'report.json').write_text(
        json.dumps({
            'title': 'run 报告',
            'summary': '摘要',
            'sections': [],
            'markdown': '# run 报告',
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (run_folder / 'graph.json').write_text(
        json.dumps({'nodes': [{'id': 'run-node'}], 'edges': []}),
        encoding='utf-8',
    )
    (run_folder / 'debate' / 'claims.jsonl').write_text(
        json.dumps({'claim_id': 'C1', 'status': 'proposed'}) + '\n',
        encoding='utf-8',
    )

    runs = client.get(f'/api/task/{task.task_id}/runs').get_json()['data']
    assert runs[0]['run_id'] == run_id
    assert runs[0]['task_card']['analysis_mode'] == 'evidence_debate'
    assert runs[0]['analysis_mode'] == 'evidence_debate'
    evidence = client.get(
        f'/api/task/{task.task_id}/evidence', query_string={'run_id': run_id},
    ).get_json()['data']
    assert evidence['items'][0]['evidence_uid'].startswith('ev_')
    assert evidence['items'][0]['display_id'] == 'E1'
    report = client.get(
        f'/api/report/{task.task_id}', query_string={'run_id': run_id},
    ).get_json()['data']
    assert report['title'] == 'run 报告'
    assert report['run_id'] == run_id
    graph = client.get(
        f'/api/task/{task.task_id}/graph', query_string={'run_id': run_id},
    ).get_json()['data']
    assert graph['nodes'][0]['id'] == 'run-node'
    debate = client.get(
        f'/api/task/{task.task_id}/debate', query_string={'run_id': run_id},
    ).get_json()['data']
    assert debate['claims'][0]['claim_id'] == 'C1'

    invalid = client.get(
        f'/api/report/{task.task_id}', query_string={'run_id': 'run_not_owned'},
    )
    assert invalid.status_code == 404

    dbmod.insert_llm_call_log(run_id, 'deepseek', 'deepseek-v4-flash', agent='analyst')
    finished_task = ResearchTask.load(task.task_id)
    finished_task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)
    dbmod.finish_task_run(run_id, 'completed')
    deleted = client.delete(f'/api/task/{task.task_id}')
    assert deleted.status_code == 200
    assert not (isolated_runtime / 'tasks' / task.task_id).exists()
    assert dbmod.list_task_runs(task.task_id) == []
    assert dbmod.get_debate_run(run_id) is None
    assert dbmod.list_llm_call_logs(run_id) == []


def test_confirm_resets_previous_run_runtime_state(isolated_runtime, monkeypatch):
    task = _task(task_id='task_confirm_state_reset', mode='direct')
    task.error = 'old-run-error'
    task.collect_failures = [{'agent': 'old-collector', 'error': 'old'}]
    task.progress_detail = {
        'stage': 'completed_partial',
        'report_ready': True,
        'old_marker': 'must-not-leak',
        'debate': {'claim_count': 99, 'audit_failure_count': 7},
    }
    task.set_status(ResearchTaskStatus.COMPLETED_PARTIAL, '旧 run', progress=100)

    from app.api import task as task_api
    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)
    from app import create_app
    client = create_app(Config).test_client()

    response = client.post(f'/api/task/{task.task_id}/confirm', json={})

    assert response.status_code == 200
    run_id = response.get_json()['data']['run_id']
    current = ResearchTask.load(task.task_id)
    assert current.current_run_id == run_id
    assert current.status is ResearchTaskStatus.COLLECTING
    assert current.error is None
    assert current.collect_failures == []
    assert current.progress_detail == {
        'stage': 'collecting',
        'analysis_mode': 'direct',
        'run_id': run_id,
        'report_ready': False,
    }

    status = client.get(f'/api/task/{task.task_id}/status').get_json()['data']
    assert status['error'] is None
    assert status['collect_failures'] == []
    assert 'old_marker' not in status['progress_detail']
    assert 'debate' not in status['progress_detail']


def test_confirm_admission_failure_never_leaves_an_active_ghost_run(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_confirm_admission_compensation', mode='direct')
    from app.api import task as task_api
    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)
    original_assign = task_api.dbutil.assign_pending_llm_logs
    monkeypatch.setattr(
        task_api.dbutil,
        'assign_pending_llm_logs',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('injected admission failure')
        ),
    )
    from app import create_app
    client = create_app(Config).test_client()

    failed_response = client.post(f'/api/task/{task.task_id}/confirm', json={})

    assert failed_response.status_code == 500
    failed_rows = dbmod.list_task_runs(task.task_id)
    assert len(failed_rows) == 1
    assert failed_rows[0]['status'] == 'failed'
    unchanged = ResearchTask.load(task.task_id)
    assert unchanged.current_run_id is None
    assert unchanged.status is ResearchTaskStatus.CREATED

    # The terminal audit row must not block a later admission or deletion.
    monkeypatch.setattr(task_api.dbutil, 'assign_pending_llm_logs', original_assign)
    retry = client.post(f'/api/task/{task.task_id}/confirm', json={})
    assert retry.status_code == 200


def test_confirm_worker_start_failure_terminalizes_the_published_run(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_confirm_worker_compensation', mode='direct')
    from app.api import task as task_api
    monkeypatch.setattr(task_api.threading, 'Thread', _FailingStartThread)
    from app import create_app
    client = create_app(Config).test_client()

    response = client.post(f'/api/task/{task.task_id}/confirm', json={})

    assert response.status_code == 500
    current = ResearchTask.load(task.task_id)
    assert current.current_run_id
    assert current.status is ResearchTaskStatus.FAILED
    assert current.progress_detail['admission_failed'] is True
    assert current.progress_detail['report_ready'] is False
    assert dbmod.get_task_run(current.current_run_id)['status'] == 'failed'


def test_first_confirm_moves_pending_planner_spend_without_reassigning_on_ab_run(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_confirm_planner_budget', mode='direct')
    dbmod.insert_llm_call_log(
        task.task_id,
        'deepseek',
        'deepseek-v4-flash',
        agent='planner',
        cost_cny=0.01,
    )
    assert dbmod.reserve_llm_budget(
        task.task_id,
        'planner-pending-extreme',
        1.99,
        2.0,
    )

    from app.api import task as task_api
    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)
    from app import create_app
    client = create_app(Config).test_client()

    first = client.post(f'/api/task/{task.task_id}/confirm', json={})
    assert first.status_code == 200
    first_run = first.get_json()['data']['run_id']
    assert len(dbmod.list_llm_call_logs(first_run)) == 1
    assert dbmod.list_llm_call_logs(task.task_id) == []
    assert dbmod.get_llm_budget_reservation('planner-pending-extreme')['run_id'] == first_run

    completed = ResearchTask.load(task.task_id)
    completed.set_status(ResearchTaskStatus.COMPLETED, '首轮完成', progress=100)
    dbmod.finish_task_run(first_run, 'completed')
    second = client.post(f'/api/task/{task.task_id}/confirm', json={})
    assert second.status_code == 200
    second_run = second.get_json()['data']['run_id']
    assert second_run != first_run
    assert len(dbmod.list_llm_call_logs(first_run)) == 1
    assert dbmod.list_llm_call_logs(second_run) == []
    assert dbmod.get_llm_budget_reservation('planner-pending-extreme')['run_id'] == first_run


def test_legacy_rerun_route_cannot_start_a_second_realtime_orchestrator(
    isolated_runtime,
):
    task = _task(task_id='task_agentteams_rerun_tombstone', mode='direct')
    source_run = task.create_run(task.task_card)
    task.set_status(ResearchTaskStatus.COMPLETED, '首轮完成', progress=100)
    before_folders = sorted(path.name for path in (Path(task.folder) / 'runs').iterdir())
    before_rows = dbmod.list_task_runs(task.task_id)

    from app import create_app
    client = create_app(Config).test_client()
    response = client.post(f'/api/report/{task.task_id}/rerun-analysis')

    assert response.status_code == 409
    assert response.get_json()['code'] == 'agentteams_rerun_requires_confirmation'
    current = ResearchTask.load(task.task_id)
    assert current.current_run_id == source_run
    assert current.status is ResearchTaskStatus.COMPLETED
    assert sorted(path.name for path in (Path(task.folder) / 'runs').iterdir()) == before_folders
    assert dbmod.list_task_runs(task.task_id) == before_rows


def test_replay_rerun_route_is_read_only(isolated_runtime):
    task = _task(task_id='task_replay_rerun_tombstone', mode='evidence_debate')
    replay_card = {**task.task_card, 'execution_mode': 'replay'}
    task.set_task_card(replay_card)
    source_run = task.create_run(replay_card)
    task.set_status(ResearchTaskStatus.COMPLETED, '回放已装载', progress=100)

    from app import create_app
    client = create_app(Config).test_client()
    response = client.post(f'/api/report/{task.task_id}/rerun-analysis')

    assert response.status_code == 409
    assert response.get_json()['code'] == 'replay_read_only'
    assert ResearchTask.load(task.task_id).current_run_id == source_run
    assert len(list((Path(task.folder) / 'runs').iterdir())) == 1


def test_delete_task_cleans_owned_related_rows_and_artifacts(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_owned_artifacts', mode='direct')
    run_id = task.create_run(task.task_card)
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'completed')

    scenario_id = 'scen_0123456789'
    dbmod.insert_scenario_run(scenario_id, task.task_id, {'run_id': run_id}, 'completed')
    scenario_folder = isolated_runtime / 'scenarios' / scenario_id
    scenario_folder.mkdir(parents=True)
    (scenario_folder / 'report.json').write_text('{}', encoding='utf-8')

    sub_id = 'sub_0123456789'
    brief_id = 'brief_0123456789'
    dbmod.insert_tracking_sub(sub_id, task.task_id, 'weekly', 8)
    brief_folder = isolated_runtime / 'briefs' / sub_id
    brief_folder.mkdir(parents=True)
    brief_path = brief_folder / f'{brief_id}.md'
    brief_path.write_text('# brief', encoding='utf-8')
    dbmod.insert_brief(brief_id, sub_id, run_id='tracking-run', markdown_path=str(brief_path))

    dedup_key = 'owned-evidence-row'
    dbmod.insert_evidence_dedup(dedup_key, task.task_id, {'title': 'owned'})
    task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)

    from app import create_app
    client = create_app(Config).test_client()
    response = client.delete(f'/api/task/{task.task_id}')

    assert response.status_code == 200
    assert response.get_json()['data'] == {
        'deleted_scenarios': 1,
        'deleted_tracking_subscriptions': 1,
    }
    assert not Path(task.folder).exists()
    assert not scenario_folder.exists()
    assert not brief_folder.exists()
    assert dbmod.get_task_run(run_id) is None
    assert dbmod.get_scenario_run(scenario_id) is None
    assert dbmod.get_tracking_sub(sub_id) is None
    assert dbmod.list_briefs(sub_id) == []
    assert dbmod.get_evidence_dedup(dedup_key) is None


def test_delete_task_reports_filesystem_failure_without_clearing_db(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_failure_visible', mode='direct')
    run_id = task.create_run(task.task_card)
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'completed')
    task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)

    from app.api import task as task_api

    def fail_remove(_path):
        raise OSError('private filesystem detail')

    monkeypatch.setattr(task_api, '_remove_owned_directory', fail_remove)
    from app import create_app
    client = create_app(Config).test_client()
    response = client.delete(f'/api/task/{task.task_id}')

    assert response.status_code == 500
    assert response.get_json()['error'] == '任务产物删除失败，数据库记录未清理'
    assert Path(task.folder).is_dir()
    assert dbmod.get_task_run(run_id) is not None


def test_delete_restores_staged_directories_when_manifest_transaction_fails(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_manifest_rollback', mode='direct')
    run_id = task.create_run(task.task_card)
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'completed')
    scenario_id = 'scen_manifestrollback'
    dbmod.insert_scenario_run(scenario_id, task.task_id, {}, 'completed')
    scenario_folder = isolated_runtime / 'scenarios' / scenario_id
    scenario_folder.mkdir(parents=True)
    (scenario_folder / 'scenario.json').write_text('{}', encoding='utf-8')
    task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)

    def fail_after_stage(
        _task_id,
        *,
        expected_manifest=None,
        stage_filesystem=None,
    ):
        assert expected_manifest is not None
        assert stage_filesystem is not None
        stage_filesystem()
        raise RuntimeError('task_related_state_changed_during_delete')

    monkeypatch.setattr(dbmod, 'delete_task_runs', fail_after_stage)
    from app import create_app
    client = create_app(Config).test_client()

    response = client.delete(f'/api/task/{task.task_id}')

    assert response.status_code == 409
    assert Path(task.folder).is_dir()
    assert scenario_folder.is_dir()
    assert dbmod.get_task_run(run_id) is not None
    assert dbmod.get_scenario_run(scenario_id) is not None


def test_delete_task_rejects_active_db_run_even_if_task_json_is_stale(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_db_active_guard', mode='direct')
    run_id = task.create_run(task.task_card)
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'debating')
    # Simulate a stale task.json that incorrectly looks terminal.
    task.set_status(ResearchTaskStatus.COMPLETED, 'stale terminal state', progress=100)

    from app import create_app
    client = create_app(Config).test_client()
    response = client.delete(f'/api/task/{task.task_id}')

    assert response.status_code == 409
    assert Path(task.folder).is_dir()
    assert dbmod.get_task_run(run_id)['status'] == 'debating'


def test_delete_holds_admission_lock_until_confirm_can_no_longer_insert(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_confirm_lock', mode='direct')
    task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)

    from app.api import task as task_api
    real_remove = task_api._remove_owned_directory
    delete_checked = threading.Event()
    allow_delete = threading.Event()

    def slow_remove(path):
        basename = os.path.basename(path)
        if task.task_id in basename and '.deleting-' in basename:
            delete_checked.set()
            assert allow_delete.wait(timeout=3)
        return real_remove(path)

    monkeypatch.setattr(task_api, '_remove_owned_directory', slow_remove)
    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    responses = {}

    def delete_request():
        with app.test_client() as client:
            responses['delete'] = client.delete(f'/api/task/{task.task_id}').status_code

    def confirm_request():
        with app.test_client() as client:
            responses['confirm'] = client.post(
                f'/api/task/{task.task_id}/confirm', json={},
            ).status_code

    delete_worker = threading.Thread(target=delete_request)
    confirm_worker = threading.Thread(target=confirm_request)
    delete_worker.start()
    assert delete_checked.wait(timeout=3)
    confirm_worker.start()
    time.sleep(0.05)
    assert confirm_worker.is_alive(), 'confirm 应等待同一 task_run_lock'
    allow_delete.set()
    delete_worker.join(timeout=3)
    confirm_worker.join(timeout=3)

    assert responses == {'delete': 200, 'confirm': 404}
    assert dbmod.list_task_runs(task.task_id) == []
    assert not Path(task.folder).exists()


def test_delete_waits_for_all_terminal_pipeline_writes(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_terminal_barrier', mode='direct')
    run_id = task.create_run(task.task_card)
    dbmod.insert_task_run(run_id, task.task_id, task.task_card, 'assembling')
    task.set_status(ResearchTaskStatus.ASSEMBLING, '装配中', progress=97)

    from app.services import pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, 'remember_task_episode', lambda _task_id: None)
    logger_entered = threading.Event()
    allow_logger = threading.Event()

    class BlockingLogger:
        def log(self, *_args, **_kwargs):
            logger_entered.set()
            assert allow_logger.wait(timeout=3)

    finalize_errors = []

    def finalize():
        try:
            pipeline_module._finalize_success(
                task_id=task.task_id,
                run_id=run_id,
                analysis_mode='direct',
                debate_status=None,
                report={'title': '已提交', 'sections': [], 'cited_ids': []},
                timings={},
                total_tokens=0,
                total_cost=0.0,
                cost_summary={
                    'settled_cny': 0.0,
                    'reserved_cny': 0.0,
                    'committed_cny': 0.0,
                },
                elapsed_seconds=1.0,
                logger=BlockingLogger(),
            )
        except Exception as error:  # pragma: no cover - assertion aid
            finalize_errors.append(error)

    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    responses = {}

    def delete_request():
        with app.test_client() as client:
            responses['delete'] = client.delete(f'/api/task/{task.task_id}').status_code

    finalizer = threading.Thread(target=finalize)
    deleter = threading.Thread(target=delete_request)
    finalizer.start()
    assert logger_entered.wait(timeout=3)
    deleter.start()
    time.sleep(0.05)
    assert deleter.is_alive(), 'DELETE 应等待 terminal task_run_lock 释放'
    allow_logger.set()
    finalizer.join(timeout=3)
    deleter.join(timeout=3)

    assert not finalize_errors
    assert responses == {'delete': 200}
    assert not Path(task.folder).exists()
    assert dbmod.get_task_run(run_id) is None


def test_delete_task_rejects_symlinked_owned_artifact(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_delete_symlink_guard', mode='direct')
    scenario_id = 'scen_abcdef1234'
    dbmod.insert_scenario_run(scenario_id, task.task_id, {}, 'completed')
    outside = isolated_runtime / 'outside-scenario'
    outside.mkdir()
    marker = outside / 'keep.txt'
    marker.write_text('keep', encoding='utf-8')
    scenario_root = isolated_runtime / 'scenarios'
    scenario_root.mkdir()
    (scenario_root / scenario_id).symlink_to(outside, target_is_directory=True)
    task.set_status(ResearchTaskStatus.COMPLETED, '完成', progress=100)

    from app import create_app
    client = create_app(Config).test_client()
    response = client.delete(f'/api/task/{task.task_id}')

    assert response.status_code == 409
    assert marker.read_text(encoding='utf-8') == 'keep'
    assert Path(task.folder).is_dir()
    assert dbmod.get_scenario_run(scenario_id) is not None


def test_concurrent_confirms_create_only_one_active_run(isolated_runtime, monkeypatch):
    task = _task(task_id='task_confirm_lock', mode='direct')
    from app.api import task as task_api
    real_thread = threading.Thread
    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)

    original_create_run = ResearchTask.create_run

    def slow_create_run(self, *args, **kwargs):
        # Widen the check/create race. Without the shared task lock both
        # requests pass the status check before either marks the task active.
        time.sleep(0.08)
        return original_create_run(self, *args, **kwargs)

    monkeypatch.setattr(ResearchTask, 'create_run', slow_create_run)

    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    start = threading.Barrier(3)
    statuses = []

    def confirm():
        with app.test_client() as client:
            start.wait()
            response = client.post(f'/api/task/{task.task_id}/confirm', json={})
            statuses.append(response.status_code)

    # ``task_api.threading`` is the shared stdlib module, so retain the real
    # class before replacing the background-pipeline constructor above.
    workers = [real_thread(target=confirm) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=3)

    assert sorted(statuses) == [200, 409]
    assert len(dbmod.list_task_runs(task.task_id)) == 1


def test_rerun_missing_task_is_not_found(isolated_runtime):
    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    client = app.test_client()

    response = client.post('/api/report/not-a-task/rerun-analysis')

    assert response.status_code == 404


def test_scenario_is_bound_to_one_published_run_and_budget_ledger(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_scenario_run_binding', mode='direct')

    run_a = task.create_run()
    evidence_a = Path(task.run_folder(run_a)) / 'evidence' / 'financial.jsonl'
    evidence_a.write_text(
        json.dumps(_card('run-a-only').to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    EvidenceStore(task.task_id, run_id=run_a, allow_staging=True).freeze_to_run(run_a)
    report_a = {
        'run_id': run_a,
        'title': 'published A',
        'summary': 'A',
        'sections': [],
        'markdown': '# A',
    }
    (Path(task.run_folder(run_a)) / 'report.json').write_text(
        json.dumps(report_a), encoding='utf-8',
    )
    (Path(task.folder) / 'report.json').write_text(json.dumps(report_a), encoding='utf-8')

    # B is current and frozen, but has no published report. It must not replace
    # A merely because EvidenceStore(task_id) would otherwise prefer current.
    run_b = task.create_run()
    evidence_b = Path(task.run_folder(run_b)) / 'evidence' / 'financial.jsonl'
    evidence_b.write_text(
        json.dumps(_card('run-b-only').to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    EvidenceStore(task.task_id, run_id=run_b, allow_staging=True).freeze_to_run(run_b)

    captured = {}
    from app.utils import llm_client as llm_client_module
    from app.utils.llm_client import LLMResult

    class FakeScenarioLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.provider = 'deepseek'
            self.model = 'deepseek-v4-flash'
            self.max_retries = 0

        def chat_json_result(self, messages, **kwargs):
            return LLMResult(
                content='{}',
                provider=self.provider,
                model=self.model,
                finish_reason='stop',
                usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                parsed_json={
                    'scenario_title': 'A run scenario',
                    'hypothesis': 'run-a-only',
                    'baseline_facts': ['E1', 'E999'],
                    'injected_events': [],
                    'counter_scenario': {'enabled': True, 'hypothesis': '未发生'},
                },
            )

    monkeypatch.setattr(llm_client_module, 'LLMClient', FakeScenarioLLM)
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'test-key')
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)

    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    client = app.test_client()

    unpublished = client.post('/api/scenario/create', json={
        'task_id': task.task_id,
        'run_id': run_b,
        'hypothesis': 'run-b-only',
    })
    assert unpublished.status_code == 409

    created = client.post('/api/scenario/create', json={
        'task_id': task.task_id,
        'run_id': run_a,
        'hypothesis': 'run-a-only',
    })
    assert created.status_code == 200
    payload = created.get_json()['data']
    assert payload['run_id'] == run_a
    assert payload['scenario_config']['run_id'] == run_a
    assert payload['scenario_config']['baseline_facts'] == ['E1']
    assert captured['budget_run_id'] == run_a
    assert len(dbmod.list_llm_call_logs(run_a)) == 1
    assert dbmod.list_llm_call_logs(run_b) == []

    # Omitting run_id resolves the latest published report (A), not current B.
    latest = client.post('/api/scenario/create', json={
        'task_id': task.task_id,
        'hypothesis': 'run-a-only',
    })
    assert latest.status_code == 200
    assert latest.get_json()['data']['run_id'] == run_a

    # User-edited start parameters cannot retarget the scenario to B or carry
    # invalid display IDs into A's report.
    from app.services.scenario import runner as scenario_runner

    class FakeGraph:
        def add_episode(self, **kwargs):
            return None

    monkeypatch.setattr(scenario_runner, 'get_graph_client', lambda *_: FakeGraph())
    completed = scenario_runner.run_scenario(payload['scenario_id'], {
        'task_id': task.task_id,
        'run_id': run_b,
        'scenario_title': 'tamper attempt',
        'hypothesis': 'run-b-only',
        'baseline_facts': ['E999'],
        'injected_events': [],
        'agent_scale': 1,
        'max_rounds': 1,
        'counter_scenario': {'enabled': True, 'hypothesis': '未发生'},
    })
    assert completed['run_id'] == run_a
    assert completed['config']['run_id'] == run_a
    assert completed['config']['baseline_facts'] == ['E1']


@pytest.mark.parametrize('failure_kind', ['bad_json', 'transport'])
def test_scenario_llm_failure_is_accounted_on_source_run(
    isolated_runtime,
    monkeypatch,
    failure_kind,
):
    task = _task(task_id=f'task_scenario_failure_{failure_kind}', mode='direct')
    source_run = task.create_run()
    evidence = Path(task.run_folder(source_run)) / 'evidence' / 'financial.jsonl'
    evidence.write_text(
        json.dumps(_card(f'{failure_kind}-source').to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    EvidenceStore(
        task.task_id,
        run_id=source_run,
        allow_staging=True,
    ).freeze_to_run(source_run)
    report = {
        'run_id': source_run,
        'title': 'published source',
        'summary': 'source',
        'sections': [],
        'markdown': '# source',
    }
    (Path(task.run_folder(source_run)) / 'report.json').write_text(
        json.dumps(report),
        encoding='utf-8',
    )

    from app.utils import llm_client as llm_client_module
    from app.utils.llm_client import LLMResponseError

    class FailingScenarioLLM:
        def __init__(self, **kwargs):
            self.provider = 'deepseek'
            self.model = 'deepseek-v4-flash'
            assert kwargs['budget_run_id'] == source_run

        def chat_json_result(self, messages, **kwargs):
            if failure_kind == 'bad_json':
                error = LLMResponseError('unusable json')
                error.usage = {
                    'prompt_tokens': 21,
                    'completion_tokens': 9,
                    'total_tokens': 30,
                }
                error.retry_count = 1
                raise error
            raise RuntimeError('private prompt must never be persisted')

    monkeypatch.setattr(llm_client_module, 'LLMClient', FailingScenarioLLM)
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', 'test-key')

    from app.services.scenario.scenario_agent import design_scenario
    config = design_scenario(
        task.task_id,
        '验证失败回退',
        run_id=source_run,
    )

    assert config['run_id'] == source_run
    rows = dbmod.list_llm_call_logs(source_run)
    assert len(rows) == 1
    assert rows[0]['agent'] == 'scenario'
    assert rows[0]['ok'] == 0
    assert rows[0]['total_tokens'] == (30 if failure_kind == 'bad_json' else 0)
    assert 'private prompt' not in str(rows[0])


def test_status_query_isolates_historical_run_and_validates_ownership(
    isolated_runtime,
    monkeypatch,
):
    task = _task(task_id='task_status_isolation', mode='evidence_debate')

    historical_run = task.create_run()
    dbmod.insert_task_run(
        historical_run,
        task.task_id,
        task.task_card,
        # Simulate a stale process row: a published report is the stronger
        # terminal marker for a historical run.
        'analyzing',
    )
    dbmod.update_task_run(
        historical_run,
        stage_timings_json=json.dumps({'debating': 12}),
        collect_failures_json=json.dumps([{'source': 'historical-fixture'}]),
    )
    dbmod.insert_debate_run(historical_run, task.task_id, 'completed')
    dbmod.update_debate_run(
        historical_run,
        current_round=2,
        current_role='historical-judge',
        claim_count=7,
        challenge_count=3,
        withdrawn_count=1,
        audit_failure_count=2,
    )
    historical_folder = Path(task.run_folder(historical_run))
    (historical_folder / 'report.json').write_text(
        json.dumps({
            'run_id': historical_run,
            'title': '历史 run 报告',
            'analysis_mode': 'evidence_debate',
            'debate_status': 'completed',
            'sections': [],
            'markdown': '# 历史 run 报告',
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (historical_folder / 'evidence_index.json').write_text(
        json.dumps({'run_id': historical_run, 'items': []}),
        encoding='utf-8',
    )
    (historical_folder / 'debate' / 'verdict.json').write_text(
        json.dumps({'status': 'complete', 'accepted_claim_ids': ['old-claim']}),
        encoding='utf-8',
    )

    current_run = task.create_run()
    dbmod.insert_task_run(current_run, task.task_id, task.task_card, 'debating')
    dbmod.insert_debate_run(current_run, task.task_id, 'debating')
    dbmod.update_debate_run(
        current_run,
        current_round=1,
        current_role='current-growth',
        claim_count=2,
        challenge_count=1,
    )
    current_task = ResearchTask.load(task.task_id)
    current_task.error = 'current-run-only-error'
    current_task.collect_failures = [{'source': 'current-fixture'}]
    current_task.progress_detail = {'stage': 'debating', 'marker': 'current-only'}
    current_task.set_status(ResearchTaskStatus.DEBATING, '当前 run 辩论中', progress=73)

    other_task = _task(task_id='task_status_other', mode='direct')
    other_run = other_task.create_run()
    dbmod.insert_task_run(other_run, other_task.task_id, other_task.task_card, 'completed')

    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    from app import create_app
    app = create_app(Config)
    app.config['TESTING'] = True
    client = app.test_client()

    historical_response = client.get(
        f'/api/task/{task.task_id}/status',
        query_string={'run_id': historical_run},
    )
    assert historical_response.status_code == 200
    historical = historical_response.get_json()['data']
    assert historical['run_id'] == historical_run
    assert historical['is_current'] is False
    assert historical['status'] == 'completed'
    assert historical['progress'] == 100
    assert historical['error'] is None
    assert historical['collect_failures'] == [{'source': 'historical-fixture'}]
    assert historical['progress_detail']['historical'] is True
    assert historical['progress_detail']['report_title'] == '历史 run 报告'
    assert historical['progress_detail']['stage_timings'] == {'debating': 12}
    assert historical['progress_detail']['artifacts']['report'] is True
    assert historical['progress_detail']['artifacts']['evidence_snapshot'] is True
    assert historical['progress_detail']['artifacts']['debate_verdict'] is True
    assert historical['progress_detail']['debate']['claim_count'] == 7
    assert historical['progress_detail']['debate']['current_role'] == 'historical-judge'
    assert 'current-only' not in json.dumps(historical, ensure_ascii=False)

    for query in ({'run_id': current_run}, None):
        response = client.get(
            f'/api/task/{task.task_id}/status',
            query_string=query,
        )
        assert response.status_code == 200
        current = response.get_json()['data']
        assert current['run_id'] == current_run
        assert current['is_current'] is True
        assert current['status'] == 'debating'
        assert current['progress'] == 73
        assert current['error'] == 'current-run-only-error'
        assert current['collect_failures'] == [{'source': 'current-fixture'}]
        assert current['progress_detail']['marker'] == 'current-only'
        assert current['progress_detail']['debate']['claim_count'] == 2
        assert current['progress_detail']['debate']['current_role'] == 'current-growth'

    foreign = client.get(
        f'/api/task/{task.task_id}/status',
        query_string={'run_id': other_run},
    )
    assert foreign.status_code == 404
    assert foreign.get_json()['success'] is False

    invalid = client.get(
        f'/api/task/{task.task_id}/status',
        query_string={'run_id': '../not-a-run'},
    )
    assert invalid.status_code == 404
