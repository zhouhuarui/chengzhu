"""Agent Team HTTP publication, rejection, and rollback contracts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus
from app.services import graph_ingest, report_assembler
from app.team import AgentTeamStore
from app.utils import db as dbmod
from app.utils.report_commit import file_sha256


@pytest.fixture
def team_client(tmp_path, monkeypatch):
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'team-api.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    from app import create_app

    app = create_app(Config)
    app.config['TESTING'] = True
    yield app.test_client()
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None


def _new_task(task_id: str) -> ResearchTask:
    task = ResearchTask(task_id=task_id, requirement='测试 Agent Team 报告')
    task.set_task_card({
        'title': '测试任务',
        'analysis_mode': 'direct',
        'execution_mode': 'agentteams',
    })
    return task


def _new_run(task: ResearchTask, team_id: str) -> str:
    run_id = task.create_run(task.task_card)
    task.begin_run(
        run_id,
        ResearchTaskStatus.REVIEWING,
        '等待人工审批',
        95,
        analysis_mode='direct',
    )
    dbmod.insert_task_run(
        run_id,
        task.task_id,
        task.task_card or {},
        ResearchTaskStatus.REVIEWING.value,
    )
    AgentTeamStore.create_team_run(team_id, run_id, task.task_id)
    return run_id


def _report(task_id: str, run_id: str, title: str) -> Dict[str, Any]:
    return {
        'task_id': task_id,
        'run_id': run_id,
        'title': title,
        'sections': [{'title': '结论', 'content': f'{title} 的内容'}],
        'markdown': f'# {title}\n\n{title} 的内容',
    }


def _register_candidate(
    task: ResearchTask,
    team_id: str,
    run_id: str,
    *,
    expected_version: int,
    version: int,
    title: str,
    safe_fallback: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    candidate = _report(task.task_id, run_id, title)
    marker = 'safe_' if safe_fallback else ''
    candidate_name = f'report_candidate_{marker}v{version}.json'
    candidate_path = Path(task.run_folder(run_id)) / candidate_name
    candidate_path.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    artifact = AgentTeamStore.register_artifact(
        team_id,
        artifact_type='report',
        uri=f'runs/{run_id}/{candidate_name}',
        sha256=file_sha256(str(candidate_path)),
        metadata={
            'task_id': task.task_id,
            'run_id': run_id,
            'candidate_path': candidate_name,
        },
        expected_version=expected_version,
        idempotency_key=f'register-{team_id}-{version}',
        artifact_version=version,
    )
    return artifact, candidate


def _candidate_team(
    task_id: str,
    team_id: str,
    *,
    title: str = '待发布报告',
) -> Tuple[ResearchTask, str, Dict[str, Any], Dict[str, Any]]:
    task = _new_task(task_id)
    run_id = _new_run(task, team_id)
    artifact, candidate = _register_candidate(
        task,
        team_id,
        run_id,
        expected_version=0,
        version=1,
        title=title,
    )
    assert AgentTeamStore.get_team(team_id)['team']['current_stage'] == (
        'awaiting_publish_approval'
    )
    return task, run_id, artifact, candidate


def _publish_team_run(
    task: ResearchTask,
    team_id: str,
    *,
    title: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    run_id = _new_run(task, team_id)
    report = _report(task.task_id, run_id, title)
    report_assembler.publish_report(task.task_id, report, run_id=run_id)
    graph_path = Path(task.run_folder(run_id)) / 'graph.json'
    graph_path.write_text(
        json.dumps({'run_id': run_id, 'title': title}, ensure_ascii=False),
        encoding='utf-8',
    )
    graph_ingest.publish_latest_graph(task.task_id, run_id)
    artifact = AgentTeamStore.register_artifact(
        team_id,
        artifact_type='report',
        uri=f'runs/{run_id}/report.json',
        expected_version=0,
        idempotency_key=f'register-published-{team_id}',
        metadata={'task_id': task.task_id, 'run_id': run_id},
    )
    AgentTeamStore.decide_approval(
        team_id,
        artifact['artifact_id'],
        'approved',
        expected_version=1,
        idempotency_key=f'approve-published-{team_id}',
        authority='vue',
        actor='vue-user',
    )
    return run_id, artifact, report


def _two_published_runs(
    task_id: str,
) -> Tuple[
    ResearchTask,
    Tuple[str, str, Dict[str, Any], Dict[str, Any]],
    Tuple[str, str, Dict[str, Any], Dict[str, Any]],
]:
    task = _new_task(task_id)
    first_team = f'team_{task_id}_one'
    first_run, first_artifact, first_report = _publish_team_run(
        task, first_team, title='第一版',
    )
    second_team = f'team_{task_id}_two'
    second_run, second_artifact, second_report = _publish_team_run(
        task, second_team, title='第二版',
    )
    task.set_status(ResearchTaskStatus.COMPLETED, '第二版已发布', progress=100)
    dbmod.finish_task_run(second_run, ResearchTaskStatus.COMPLETED.value)
    return (
        task,
        (first_team, first_run, first_artifact, first_report),
        (second_team, second_run, second_artifact, second_report),
    )


def test_read_and_cursor_event_api(team_client):
    AgentTeamStore.create_team_run('team_api', 'run_api', 'task_api')
    AgentTeamStore.append_event(
        'team_api',
        'member_note',
        actor='growth-analyst',
        idempotency_key='api-event',
        payload={'reasoning': 'private chain', 'summary': '可公开摘要'},
    )
    response = team_client.get('/api/team/team_api')
    assert response.status_code == 200
    assert len(response.get_json()['data']['agent_roles']) == 8

    first = team_client.get('/api/team/team_api/events?cursor=0&limit=1')
    page = first.get_json()['data']
    assert page['has_more'] is True
    second = team_client.get(
        f"/api/team/team_api/events?cursor={page['next_cursor']}&limit=20"
    )
    events = second.get_json()['data']['events']
    assert events[0]['payload']['reasoning'] == '[REDACTED]'
    assert events[0]['payload']['summary'] == '可公开摘要'

    task_view = team_client.get('/api/task/task_api/team?run_id=run_api')
    assert task_view.status_code == 200
    assert task_view.get_json()['data']['team']['team_id'] == 'team_api'


def test_internal_approval_requires_explicit_vue_and_publishes_report(team_client):
    task, run_id, artifact, candidate = _candidate_team(
        'task_approve_api', 'team_approve_api',
    )
    endpoint = '/api/team/team_approve_api/approval'
    base = {
        'artifact_id': artifact['artifact_id'],
        'decision': 'approve',
        'expected_version': 1,
        'idempotency_key': 'approve-api-report',
    }

    assert team_client.post(endpoint, json=base).status_code == 403
    forbidden = team_client.post(endpoint, json={**base, 'source': 'agent'})
    assert forbidden.status_code == 403

    stale = team_client.post(endpoint, json={
        **base,
        'source': 'vue',
        'expected_version': 0,
        'idempotency_key': 'stale-approval',
    })
    assert stale.status_code == 409
    assert stale.get_json()['current_version'] == 1

    approved = team_client.post(endpoint, json={**base, 'source': 'vue'})
    assert approved.status_code == 200
    data = approved.get_json()['data']
    assert data['snapshot']['team']['status'] == 'published'
    assert data['snapshot']['team']['current_stage'] == 'published'
    assert data['snapshot']['team']['latest_artifact_id'] == artifact['artifact_id']
    assert report_assembler.load_report(task.task_id, run_id=run_id) == candidate
    assert report_assembler.load_report(task.task_id) == candidate
    stored_task = ResearchTask.load(task.task_id)
    assert stored_task is not None
    assert stored_task.status == ResearchTaskStatus.COMPLETED
    assert stored_task.progress_detail['report_ready'] is True

    replay = team_client.post(endpoint, json={**base, 'source': 'vue'})
    assert replay.status_code == 200
    assert replay.get_json()['data']['replayed'] is True
    assert len(AgentTeamStore.get_team('team_approve_api')['approvals']) == 1


def test_task_approval_is_resumable_after_decision_is_durable(
    team_client,
    monkeypatch,
):
    task, run_id, artifact, candidate = _candidate_team(
        'task_retry_publish', 'team_retry_publish',
    )
    endpoint = f'/api/task/{task.task_id}/runs/{run_id}/approval'
    body = {
        'decision': 'approve',
        'reason': '人工确认发布',
        'expected_version': 1,
    }
    original = report_assembler.publish_report

    def fail_first_publish(*args, **kwargs):
        raise OSError('simulated disk interruption')

    monkeypatch.setattr(report_assembler, 'publish_report', fail_first_publish)
    interrupted = team_client.post(endpoint, json=body)
    assert interrupted.status_code == 500
    assert interrupted.get_json()['code'] == 'publication_retry_required'
    interrupted_snapshot = AgentTeamStore.get_team('team_retry_publish')
    assert interrupted_snapshot['team']['status'] == 'approved'
    assert interrupted_snapshot['artifacts'][0]['status'] == 'approved'
    assert interrupted_snapshot['artifacts'][0]['is_latest'] is False
    assert len(interrupted_snapshot['approvals']) == 1
    waiting = team_client.get(
        f'/api/task/{task.task_id}/team?run_id={run_id}'
    ).get_json()['data']['approval']
    assert waiting['required'] is True
    assert waiting['expected_version'] == 2
    interrupted_task = ResearchTask.load(task.task_id)
    assert interrupted_task is not None
    assert interrupted_task.status == ResearchTaskStatus.REVIEWING
    assert interrupted_task.progress_detail['team_stage'] == 'publish_retry_pending'

    monkeypatch.setattr(report_assembler, 'publish_report', original)
    retried = team_client.post(endpoint, json=body)
    assert retried.status_code == 200
    data = retried.get_json()['data']
    assert data['replayed'] is True
    assert data['snapshot']['team']['status'] == 'published'
    assert data['snapshot']['team']['latest_artifact_id'] == artifact['artifact_id']
    assert len(data['snapshot']['approvals']) == 1
    assert report_assembler.load_report(task.task_id, run_id=run_id) == candidate
    assert report_assembler.load_report(task.task_id) == candidate
    completed = ResearchTask.load(task.task_id)
    assert completed is not None
    assert completed.status == ResearchTaskStatus.COMPLETED
    assert dbmod.get_task_run(run_id)['status'] == ResearchTaskStatus.COMPLETED.value


def test_safe_fallback_candidate_can_pass_the_same_publication_gate(team_client):
    task = _new_task('task_safe_candidate')
    run_id = _new_run(task, 'team_safe_candidate')
    artifact, candidate = _register_candidate(
        task,
        'team_safe_candidate',
        run_id,
        expected_version=0,
        version=2,
        title='安全降级候选',
        safe_fallback=True,
    )
    response = team_client.post(
        f'/api/task/{task.task_id}/runs/{run_id}/approval',
        json={'decision': 'approve', 'expected_version': 1},
    )
    assert response.status_code == 200
    assert response.get_json()['data']['snapshot']['team']['latest_artifact_id'] == (
        artifact['artifact_id']
    )
    assert report_assembler.load_report(task.task_id) == candidate


def test_reject_updates_task_and_second_reject_is_terminal(team_client):
    task, run_id, first, _ = _candidate_team(
        'task_rejections', 'team_rejections', title='第一份候选',
    )
    endpoint = f'/api/task/{task.task_id}/runs/{run_id}/approval'
    first_body = {
        'decision': 'reject',
        'reason': '引用不足',
        'expected_version': 1,
    }
    rejected_once = team_client.post(endpoint, json=first_body)
    assert rejected_once.status_code == 200
    snapshot = rejected_once.get_json()['data']['snapshot']
    assert snapshot['team']['status'] == 'changes_requested'
    assert snapshot['team']['current_stage'] == 'revision'
    assert snapshot['team']['rejection_count'] == 1
    stored_task = ResearchTask.load(task.task_id)
    assert stored_task is not None
    assert stored_task.status == ResearchTaskStatus.REVIEWING
    assert stored_task.progress_detail['team_stage'] == 'revision'
    assert stored_task.progress_detail['report_ready'] is False
    assert report_assembler.load_report(task.task_id) is None

    replay = team_client.post(endpoint, json=first_body)
    assert replay.status_code == 200
    assert replay.get_json()['data']['replayed'] is True
    assert replay.get_json()['data']['snapshot']['team']['rejection_count'] == 1

    second, _ = _register_candidate(
        task,
        'team_rejections',
        run_id,
        expected_version=2,
        version=2,
        title='第二份候选',
    )
    rejected_twice = team_client.post(endpoint, json={
        'decision': 'reject',
        'reason': '仍不通过',
        'expected_version': 3,
    })
    assert rejected_twice.status_code == 200
    terminal = rejected_twice.get_json()['data']['snapshot']
    assert terminal['team']['status'] == 'rejected_terminal'
    assert terminal['team']['current_stage'] == 'rejected'
    assert terminal['team']['rejection_count'] == 2
    assert terminal['team']['latest_artifact_id'] is None
    assert next(
        item for item in terminal['artifacts']
        if item['artifact_id'] == second['artifact_id']
    )['status'] == 'rejected'
    failed = ResearchTask.load(task.task_id)
    assert failed is not None
    assert failed.status == ResearchTaskStatus.FAILED
    assert failed.error == 'human_rejected_twice'
    assert failed.progress_detail['team_stage'] == 'rejected'
    assert dbmod.get_task_run(run_id)['status'] == ResearchTaskStatus.FAILED.value


def test_task_rollback_mirrors_report_and_graph_without_mutating_runs(team_client):
    task, first, second = _two_published_runs('task_cross_run')
    first_team, first_run, first_artifact, first_report = first
    second_team, second_run, second_artifact, second_report = second
    first_report_path = Path(task.run_folder(first_run)) / 'report.json'
    second_report_path = Path(task.run_folder(second_run)) / 'report.json'
    first_before = first_report_path.read_bytes()
    second_before = second_report_path.read_bytes()

    current = team_client.get(
        f'/api/task/{task.task_id}/team?run_id={second_run}'
    ).get_json()['data']
    assert current['source'] == 'live'
    assert [item['run_id'] for item in current['rollback']['targets']] == [first_run]

    endpoint = f'/api/task/{task.task_id}/runs/{second_run}/rollback'
    body = {
        'target_run_id': first_run,
        'reason': '恢复上一轮已批准报告',
        'expected_version': 2,
    }
    rolled_back = team_client.post(endpoint, json=body)
    assert rolled_back.status_code == 200
    data = rolled_back.get_json()['data']
    assert data['target_run_id'] == first_run
    assert data['snapshot']['team']['latest_artifact_id'] == first_artifact['artifact_id']
    assert report_assembler.load_report(task.task_id) == first_report
    assert report_assembler.load_report(task.task_id, run_id=first_run) == first_report
    assert report_assembler.load_report(task.task_id, run_id=second_run) == second_report
    assert first_report_path.read_bytes() == first_before
    assert second_report_path.read_bytes() == second_before
    root_graph = json.loads((Path(task.folder) / 'graph.json').read_text(encoding='utf-8'))
    assert root_graph == {'run_id': first_run, 'title': '第一版'}
    assert AgentTeamStore.get_team(first_team)['artifacts'][0]['is_latest'] is True
    assert AgentTeamStore.get_team(second_team)['artifacts'][0]['is_latest'] is False
    assert second_artifact['artifact_id'] != first_artifact['artifact_id']
    stored_task = ResearchTask.load(task.task_id)
    assert stored_task is not None
    assert stored_task.current_run_id == second_run
    assert stored_task.status == ResearchTaskStatus.COMPLETED
    assert stored_task.progress_detail['team_stage'] == 'rolled_back'
    assert stored_task.progress_detail['latest_report_run_id'] == first_run

    replay = team_client.post(endpoint, json=body)
    assert replay.status_code == 200
    assert replay.get_json()['data']['replayed'] is True

    # A delayed retry of the already published second-run approval must not
    # promote it again after the rollback moved the durable latest pointer.
    stale_approval = team_client.post(
        f'/api/task/{task.task_id}/runs/{second_run}/approval',
        json={'decision': 'approve', 'expected_version': 1},
    )
    assert stale_approval.status_code == 200
    assert stale_approval.get_json()['data']['superseded'] is True
    assert report_assembler.load_report(task.task_id) == first_report


def test_internal_rollback_uses_same_closure_and_requires_explicit_vue(team_client):
    task, first, second = _two_published_runs('task_internal_rollback')
    _, first_run, first_artifact, first_report = first
    second_team, second_run, _, _ = second
    endpoint = f'/api/team/{second_team}/rollback'
    body = {
        'target_artifact_id': first_artifact['artifact_id'],
        'reason': '内部 Vue 回滚',
        'expected_version': 2,
        'idempotency_key': 'internal-rollback',
    }
    assert team_client.post(endpoint, json=body).status_code == 403
    rolled_back = team_client.post(endpoint, json={**body, 'source': 'vue'})
    assert rolled_back.status_code == 200
    assert rolled_back.get_json()['data']['target_run_id'] == first_run
    assert report_assembler.load_report(task.task_id) == first_report
    assert json.loads(
        (Path(task.folder) / 'graph.json').read_text(encoding='utf-8')
    )['run_id'] == first_run
    assert ResearchTask.load(task.task_id).current_run_id == second_run


def test_non_report_artifacts_cannot_enter_report_approval_or_rollback(team_client):
    task = _new_task('task_non_report_approval')
    run_id = _new_run(task, 'team_non_report_approval')
    candidate = _report(task.task_id, run_id, '伪装报告')
    candidate_path = Path(task.run_folder(run_id)) / 'report_candidate_v1.json'
    candidate_path.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    visual = AgentTeamStore.register_artifact(
        'team_non_report_approval',
        artifact_type='visual-analysis',
        uri='runs/visual.json',
        sha256=file_sha256(str(candidate_path)),
        metadata={
            'task_id': task.task_id,
            'run_id': run_id,
            'candidate_path': candidate_path.name,
        },
        expected_version=0,
        idempotency_key='register-visual',
    )
    response = team_client.post(
        '/api/team/team_non_report_approval/approval',
        json={
            'artifact_id': visual['artifact_id'],
            'decision': 'approve',
            'expected_version': 1,
            'idempotency_key': 'approve-visual',
            'source': 'vue',
        },
    )
    assert response.status_code == 400
    assert AgentTeamStore.get_team('team_non_report_approval')['approvals'] == []

    rollback_task, first, second = _two_published_runs('task_non_report_rollback')
    _, _, first_artifact, _ = first
    second_team, _, second_artifact, second_report = second
    # Simulate a legacy row created before the report-only invariant existed.
    with dbmod.db_cursor() as cur:
        cur.execute(
            "UPDATE artifact_manifest SET artifact_type = 'visual-analysis' "
            'WHERE artifact_id = ?',
            (first_artifact['artifact_id'],),
        )
    denied = team_client.post(
        f'/api/team/{second_team}/rollback',
        json={
            'target_artifact_id': first_artifact['artifact_id'],
            'reason': '非报告不可回滚',
            'expected_version': 2,
            'idempotency_key': 'rollback-visual',
            'source': 'vue',
        },
    )
    assert denied.status_code == 404
    assert AgentTeamStore.get_team(second_team)['team']['latest_artifact_id'] == (
        second_artifact['artifact_id']
    )
    assert report_assembler.load_report(rollback_task.task_id) == second_report


def test_candidate_symlink_is_rejected_before_human_decision(team_client):
    task, run_id, _, candidate = _candidate_team(
        'task_candidate_symlink', 'team_candidate_symlink',
    )
    candidate_path = Path(task.run_folder(run_id)) / 'report_candidate_v1.json'
    external = Path(task.folder) / 'external-candidate.json'
    external.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    candidate_path.unlink()
    candidate_path.symlink_to(external)
    response = team_client.post(
        f'/api/task/{task.task_id}/runs/{run_id}/approval',
        json={'decision': 'approve', 'expected_version': 1},
    )
    assert response.status_code == 400
    snapshot = AgentTeamStore.get_team('team_candidate_symlink')
    assert snapshot['approvals'] == []
    assert snapshot['team']['state_version'] == 1
    assert snapshot['team']['latest_artifact_id'] is None


def test_active_team_cannot_rollback_completed_report(team_client):
    task, first, second = _two_published_runs('task_active_rollback')
    _, first_run, first_artifact, _ = first
    _, _, second_artifact, second_report = second
    active_team = 'team_task_active_rollback_three'
    active_run = _new_run(task, active_team)
    AgentTeamStore.transition_team(
        active_team,
        'running',
        expected_version=0,
        idempotency_key='start-active-team',
        current_stage='collecting',
    )
    task.set_status(ResearchTaskStatus.COLLECTING, '正在采集', progress=30)
    dbmod.update_task_run(active_run, status=ResearchTaskStatus.COLLECTING.value)

    response = team_client.post(
        f'/api/team/{active_team}/rollback',
        json={
            'target_artifact_id': first_artifact['artifact_id'],
            'reason': '运行中不得回滚',
            'expected_version': 1,
            'idempotency_key': 'active-rollback',
            'source': 'vue',
        },
    )
    assert response.status_code == 400
    assert AgentTeamStore.get_team(active_team)['team']['status'] == 'running'
    assert AgentTeamStore.get_team(active_team)['team']['latest_artifact_id'] is None
    assert ResearchTask.load(task.task_id).status == ResearchTaskStatus.COLLECTING
    assert dbmod.get_task_run(active_run)['status'] == ResearchTaskStatus.COLLECTING.value
    assert report_assembler.load_report(task.task_id) == second_report
    assert AgentTeamStore.resolve_published_artifact(
        task.task_id, first_run,
    )['artifact_id'] == first_artifact['artifact_id']
    assert AgentTeamStore.resolve_published_artifact(
        task.task_id, second_artifact['run_id'],
    )['is_latest'] is True


def test_rollback_failure_reopens_task_and_same_request_repairs_it(
    team_client,
    monkeypatch,
):
    task, first, second = _two_published_runs('task_rollback_retry')
    _, first_run, first_artifact, first_report = first
    second_team, second_run, _, _ = second
    endpoint = f'/api/task/{task.task_id}/runs/{second_run}/rollback'
    body = {
        'target_run_id': first_run,
        'reason': '故障后继续回滚',
        'expected_version': 2,
    }
    original = graph_ingest.publish_latest_graph

    def fail_graph(*args, **kwargs):
        raise OSError('simulated graph mirror interruption')

    monkeypatch.setattr(graph_ingest, 'publish_latest_graph', fail_graph)
    interrupted = team_client.post(endpoint, json=body)
    assert interrupted.status_code == 500
    assert interrupted.get_json()['code'] == 'rollback_retry_required'
    assert AgentTeamStore.get_team(second_team)['team']['latest_artifact_id'] == (
        first_artifact['artifact_id']
    )
    assert report_assembler.load_report(task.task_id) == first_report
    retry_task = ResearchTask.load(task.task_id)
    assert retry_task is not None
    assert retry_task.status == ResearchTaskStatus.REVIEWING
    assert retry_task.progress_detail['team_stage'] == 'rollback_retry_pending'
    assert dbmod.get_task_run(second_run)['status'] == ResearchTaskStatus.REVIEWING.value
    retry_view = team_client.get(
        f'/api/task/{task.task_id}/team?run_id={second_run}'
    ).get_json()['data']['rollback']
    assert retry_view['allowed'] is True
    assert retry_view['retry']['required'] is True
    assert retry_view['retry']['target']['run_id'] == first_run

    monkeypatch.setattr(graph_ingest, 'publish_latest_graph', original)
    repaired = team_client.post(endpoint, json=body)
    assert repaired.status_code == 200
    assert repaired.get_json()['data']['replayed'] is True
    completed = ResearchTask.load(task.task_id)
    assert completed is not None
    assert completed.status == ResearchTaskStatus.COMPLETED
    assert completed.progress_detail['team_stage'] == 'rolled_back'
    assert dbmod.get_task_run(second_run)['status'] == ResearchTaskStatus.COMPLETED.value


def test_published_artifact_exposes_retry_if_final_task_state_write_fails(
    team_client,
    monkeypatch,
):
    task, run_id, artifact, candidate = _candidate_team(
        'task_final_state_retry', 'team_final_state_retry',
    )
    endpoint = f'/api/task/{task.task_id}/runs/{run_id}/approval'
    body = {'decision': 'approve', 'expected_version': 1}
    original = dbmod.finish_task_run

    def fail_final_state(*args, **kwargs):
        raise OSError('simulated task_run final-state failure')

    monkeypatch.setattr(dbmod, 'finish_task_run', fail_final_state)
    interrupted = team_client.post(endpoint, json=body)
    assert interrupted.status_code == 500
    snapshot = AgentTeamStore.get_team('team_final_state_retry')
    assert snapshot['team']['status'] == 'published'
    assert snapshot['team']['latest_artifact_id'] == artifact['artifact_id']
    assert report_assembler.load_report(task.task_id) == candidate
    retry_task = ResearchTask.load(task.task_id)
    assert retry_task is not None
    assert retry_task.status == ResearchTaskStatus.REVIEWING
    assert retry_task.progress_detail['team_stage'] == 'publish_retry_pending'
    retry_view = team_client.get(
        f'/api/task/{task.task_id}/team?run_id={run_id}'
    ).get_json()['data']
    assert retry_view['approval']['required'] is True
    assert retry_view['approval']['artifact_id'] == artifact['artifact_id']

    monkeypatch.setattr(dbmod, 'finish_task_run', original)
    repaired = team_client.post(endpoint, json=body)
    assert repaired.status_code == 200
    assert repaired.get_json()['data']['replayed'] is True
    assert ResearchTask.load(task.task_id).status == ResearchTaskStatus.COMPLETED


def test_matrix_mirror_and_long_key_never_gate_local_rollback(
    team_client,
    monkeypatch,
):
    task, first, second = _two_published_runs('task_matrix_best_effort')
    _, first_run, first_artifact, first_report = first
    second_team, _, _, _ = second
    AgentTeamStore.append_event(
        second_team,
        'matrix_dispatch_sent',
        actor='chengzhu-backend',
        payload={'matrix_room_id': '!room:example.invalid'},
        idempotency_key='matrix-room-for-test',
    )
    from app.integrations.agentteams.client import MatrixClient

    monkeypatch.setattr(
        MatrixClient,
        'send_message',
        lambda *args, **kwargs: '$matrix-event',
    )

    def fail_matrix_audit(*args, **kwargs):
        raise OSError('simulated audit persistence failure')

    monkeypatch.setattr(AgentTeamStore, 'append_event', fail_matrix_audit)
    response = team_client.post(
        f'/api/team/{second_team}/rollback',
        json={
            'target_artifact_id': first_artifact['artifact_id'],
            'reason': '本地回滚不受 Matrix 影响',
            'expected_version': 2,
            'idempotency_key': 'x' * 240,
            'source': 'vue',
        },
    )
    assert response.status_code == 200
    assert response.get_json()['data']['target_run_id'] == first_run
    assert report_assembler.load_report(task.task_id) == first_report
