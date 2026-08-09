"""Bounded Reviewer revisions and human approval-cycle semantics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask
from app.models.task_card import SymbolRef, TaskCard
from app.services import agentteam_runtime as runtime_module
from app.services.agentteam_runtime import AgentTeamRuntime
from app.team import AgentTeamStore, TeamInvariantError
from app.team.contracts import DEFAULT_TEAM_DAG
from app.utils import db as dbmod


def _team_task(snapshot, task_key):
    return next(item for item in snapshot['tasks'] if item['task_key'] == task_key)


def _complete_task(team_id: str, task_key: str, ordinal: int) -> None:
    snapshot = AgentTeamStore.get_team(team_id)
    task = _team_task(snapshot, task_key)
    snapshot = AgentTeamStore.transition_task(
        team_id,
        task['team_task_id'],
        'running',
        expected_version=task['state_version'],
        idempotency_key=f'setup-{ordinal}-{task_key}-running',
        actor=task['assigned_agent'],
    )
    task = _team_task(snapshot, task_key)
    AgentTeamStore.transition_task(
        team_id,
        task['team_task_id'],
        'completed',
        expected_version=task['state_version'],
        idempotency_key=f'setup-{ordinal}-{task_key}-completed',
        actor=task['assigned_agent'],
        output={'_idempotency_key': f'setup-{ordinal}-{task_key}'},
    )


@pytest.fixture
def review_cycle(tmp_path, monkeypatch):
    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'review-cycle.db'))
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    monkeypatch.setattr(Config, 'REVIEWER_MAX_ROUNDS', 2)
    dbmod.init_db()

    task = ResearchTask(task_id='task_review_cycle', requirement='审校轮次测试')
    card = TaskCard(
        deliverable='summary',
        symbols=[SymbolRef(code='600000', name='浦发银行')],
        time_window={'start': '2025-01-01', 'end': '2025-06-30'},
        # This suite isolates revision/approval-cycle policy. Claim-gated
        # evidence_debate rendering has its own runtime tests, so direct mode
        # keeps these fixtures independent from Judge artifacts.
        analysis_mode='direct',
        execution_mode='agentteams',
    )
    task.set_task_card(card)
    run_id = task.create_run(card.to_dict())
    team_id = f'team-{run_id}'
    AgentTeamStore.create_team_run(team_id, run_id, task.task_id)

    # Materialise the prerequisite chain through the first Writer output. The
    # compliance-review task remains pending for the tests below.
    for ordinal, template in enumerate(DEFAULT_TEAM_DAG[:-1], start=1):
        _complete_task(team_id, template.task_key, ordinal)

    run_folder = Path(task.run_folder(run_id))
    run_folder.mkdir(parents=True, exist_ok=True)

    def fake_assemble(task_id, reviewed, *, run_id=None, publish=False):
        return {
            'task_id': task_id,
            'run_id': run_id,
            'title': reviewed.get('title') or '安全候选',
            'sections': list(reviewed.get('sections') or []),
            'markdown': '# 安全候选',
        }

    monkeypatch.setattr(runtime_module, 'assemble_report', fake_assemble)
    monkeypatch.setattr(
        AgentTeamRuntime,
        '_publish_refs',
        lambda self, paths, *, artifact_type, producer=None: ([], False),
    )

    def write_review_files(version: int) -> None:
        reviewed = {
            'title': f'候选 {version}',
            'sections': [{
                'title': '结论',
                'content': '仅包含通过确定性证据门禁的结论。',
                'issues': [],
            }],
        }
        (run_folder / f'reviewed_report_v{version}.json').write_text(
            json.dumps(reviewed, ensure_ascii=False), encoding='utf-8'
        )
        (run_folder / f'report_candidate_v{version}.json').write_text(
            json.dumps(fake_assemble(task.task_id, reviewed, run_id=run_id),
                       ensure_ascii=False),
            encoding='utf-8',
        )

    write_review_files(1)
    yield {
        'task': task,
        'run_id': run_id,
        'team_id': team_id,
        'run_folder': run_folder,
        'write_review_files': write_review_files,
    }

    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None


def _review(context, decision: str, key: str):
    runtime = AgentTeamRuntime(
        context['task'].task_id,
        context['run_id'],
        'compliance-reviewer',
    )
    expected_version = AgentTeamStore.get_team(
        context['team_id']
    )['team']['state_version']
    return runtime.submit_review(
        decision,
        [{'type': 'citation', 'message': f'issue-{key}'}],
        expected_version=expected_version,
        idempotency_key=key,
    )


def _rewrite(context, version: int):
    runtime = AgentTeamRuntime(
        context['task'].task_id,
        context['run_id'],
        'report-writer',
    )
    expected_version = AgentTeamStore.get_team(
        context['team_id']
    )['team']['state_version']
    result = runtime.store_report_draft(
        {
            'title': f'修订稿 {version}',
            'sections': [{
                'title': '结论',
                'content': f'第 {version} 次修订后的证据约束内容。',
            }],
        },
        expected_version=expected_version,
        idempotency_key=f'writer-revision-{version}',
    )
    context['write_review_files'](version + 1)
    return result


def test_two_revisions_then_pass_is_not_blocked_by_total_review_calls(review_cycle):
    first = _review(review_cycle, 'revise', 'review-revise-1')
    assert first['safe_fallback'] is False
    assert first['revision_request_count'] == 1

    _rewrite(review_cycle, 1)
    second = _review(review_cycle, 'revise', 'review-revise-2')
    assert second['safe_fallback'] is False
    assert second['revision_request_count'] == 2

    _rewrite(review_cycle, 2)
    passed = _review(review_cycle, 'pass', 'review-pass-3')
    assert passed['decision'] == 'pass'
    assert passed['review_round'] == 3
    assert passed['revision_request_count'] == 2
    assert passed['safe_fallback'] is False


def test_third_revise_generates_disclosed_safe_candidate(review_cycle):
    _review(review_cycle, 'revise', 'review-safe-revise-1')
    _rewrite(review_cycle, 1)
    _review(review_cycle, 'revise', 'review-safe-revise-2')
    _rewrite(review_cycle, 2)

    fallback = _review(review_cycle, 'revise', 'review-safe-revise-3')
    assert fallback['decision'] == 'safe_fallback'
    assert fallback['requested_decision'] == 'revise'
    assert fallback['review_round'] == 3
    assert fallback['revision_request_count'] == 3
    assert fallback['safe_fallback_reason'] == 'reviewer_revision_limit'
    safe_path = review_cycle['run_folder'] / fallback['candidate_path']
    assert safe_path.name == 'report_candidate_safe_v3.json'
    candidate = json.loads(safe_path.read_text(encoding='utf-8'))
    disclosure = candidate['sections'][-1]
    assert disclosure['title'] == '审校未决事项披露'
    assert '2 次审校退回上限' in disclosure['content']


def test_human_rejection_allows_one_writer_reviewer_cycle_and_two_approvals(
    review_cycle,
):
    initial = _review(review_cycle, 'pass', 'initial-review-pass')
    team_id = review_cycle['team_id']
    snapshot = AgentTeamStore.get_team(team_id)
    first_artifact = AgentTeamStore.register_artifact(
        team_id,
        artifact_type='report',
        uri=f"runs/{review_cycle['run_id']}/{initial['candidate_path']}",
        expected_version=snapshot['team']['state_version'],
        idempotency_key='approval-cycle-1-candidate',
    )

    # A cycle cannot accumulate multiple pending candidates.
    with pytest.raises(TeamInvariantError, match='已有待审批报告'):
        AgentTeamStore.register_artifact(
            team_id,
            artifact_type='report',
            uri='runs/duplicate-pending.json',
            expected_version=AgentTeamStore.get_team(team_id)['team']['state_version'],
            idempotency_key='duplicate-pending-candidate',
        )

    AgentTeamStore.decide_approval(
        team_id,
        first_artifact['artifact_id'],
        'rejected',
        expected_version=AgentTeamStore.get_team(team_id)['team']['state_version'],
        idempotency_key='human-reject-cycle-1',
        authority='vue',
        actor='vue-user',
        reason='需要一次修订',
    )

    _rewrite(review_cycle, 1)
    post_human = _review(review_cycle, 'revise', 'post-human-review')
    assert post_human['decision'] == 'safe_fallback'
    assert post_human['human_rejection_count'] == 1
    assert post_human['safe_fallback_reason'] == (
        'post_human_rejection_cycle_limit'
    )

    # Neither role may open a second chain after the one post-rejection
    # Writer/Reviewer completion.
    with pytest.raises(TeamInvariantError, match='只允许一次'):
        _rewrite(review_cycle, 2)
    with pytest.raises(TeamInvariantError, match='只允许一次'):
        _review(review_cycle, 'pass', 'post-human-extra-review')

    snapshot = AgentTeamStore.get_team(team_id)
    second_artifact = AgentTeamStore.register_artifact(
        team_id,
        artifact_type='report',
        uri=f"runs/{review_cycle['run_id']}/{post_human['candidate_path']}",
        expected_version=snapshot['team']['state_version'],
        idempotency_key='approval-cycle-2-candidate',
    )
    second_rejection = AgentTeamStore.decide_approval(
        team_id,
        second_artifact['artifact_id'],
        'rejected',
        expected_version=AgentTeamStore.get_team(team_id)['team']['state_version'],
        idempotency_key='human-reject-cycle-2',
        authority='vue',
        actor='vue-user',
        reason='第二次仍不通过',
    )
    assert second_rejection['snapshot']['team']['status'] == 'rejected_terminal'
    assert len(second_rejection['snapshot']['approvals']) == 2

    with pytest.raises(TeamInvariantError, match='terminal'):
        AgentTeamStore.register_artifact(
            team_id,
            artifact_type='report',
            uri='runs/third-cycle.json',
            expected_version=second_rejection['snapshot']['team']['state_version'],
            idempotency_key='approval-cycle-3-candidate',
        )


def test_published_team_cannot_open_another_approval_cycle(review_cycle):
    team_id = review_cycle['team_id']
    snapshot = AgentTeamStore.get_team(team_id)
    artifact = AgentTeamStore.register_artifact(
        team_id,
        artifact_type='report',
        uri='runs/approved-cycle-1.json',
        expected_version=snapshot['team']['state_version'],
        idempotency_key='approved-cycle-1-candidate',
    )
    AgentTeamStore.decide_approval(
        team_id,
        artifact['artifact_id'],
        'approved',
        expected_version=AgentTeamStore.get_team(team_id)['team']['state_version'],
        idempotency_key='approved-cycle-1',
        authority='vue',
        actor='vue-user',
    )

    snapshot = AgentTeamStore.get_team(team_id)
    assert snapshot['team']['status'] == 'published'
    assert len(snapshot['approvals']) == 1
    with pytest.raises(TeamInvariantError, match='terminal'):
        AgentTeamStore.register_artifact(
            team_id,
            artifact_type='report',
            uri='runs/approved-cycle-2.json',
            expected_version=snapshot['team']['state_version'],
            idempotency_key='approved-cycle-2-candidate',
        )
