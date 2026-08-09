"""Stable Agent Team identifiers, state values and the default task DAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


APPROVAL_AUTHORITY = 'vue'
MAX_HUMAN_REJECTIONS = 2
SYSTEM_FREEZE_AGENT = 'chengzhu-backend'

TASK_CONTRACT_FIELDS = frozenset({
    'goal',
    'inputs',
    'expected_outputs',
    'acceptance_criteria',
    'deadline',
    'budget',
    'artifact_refs',
    'trace_id',
})


def build_task_contract(
    *,
    goal: str,
    inputs: Iterable[Any],
    expected_outputs: Iterable[Any],
    acceptance_criteria: Iterable[str],
    deadline: Mapping[str, Any],
    budget: Mapping[str, Any],
    artifact_refs: Sequence[Mapping[str, Any]],
    trace_id: str,
) -> Dict[str, Any]:
    """Build the one durable handoff envelope shared by every DAG node.

    Keeping the field set exact makes Matrix a disposable mirror: Workers can
    always recover their goal, bounded inputs and acceptance policy from
    SQLite without replaying room history.
    """

    return {
        'goal': str(goal),
        'inputs': list(inputs),
        'expected_outputs': list(expected_outputs),
        'acceptance_criteria': [str(item) for item in acceptance_criteria],
        'deadline': dict(deadline),
        'budget': dict(budget),
        'artifact_refs': [dict(item) for item in artifact_refs],
        'trace_id': str(trace_id),
    }

# Manager/orchestrator is deliberately not counted as an Agent role.
DEFAULT_AGENT_ROLE_IDS: Tuple[str, ...] = (
    'research-lead',
    'disclosure-researcher',
    'market-context-researcher',
    'quality-analyst',
    'growth-analyst',
    'evidence-judge',
    'report-writer',
    'compliance-reviewer',
)


@dataclass(frozen=True)
class TeamTaskTemplate:
    task_key: str
    title: str
    assigned_agent: str
    role_id: str
    depends_on: Tuple[str, ...] = ()


# Eight Agent roles plus one deterministic backend freeze task.  Dependencies
# are task keys and are materialised as team-scoped task IDs by the store.
DEFAULT_TEAM_DAG: Tuple[TeamTaskTemplate, ...] = (
    TeamTaskTemplate(
        'research-plan',
        '拆解研究问题与证据需求',
        'research-lead',
        'research-lead',
    ),
    TeamTaskTemplate(
        'disclosure-research',
        '采集公告、财报与公司披露',
        'disclosure-researcher',
        'disclosure-researcher',
        ('research-plan',),
    ),
    TeamTaskTemplate(
        'market-context-research',
        '采集新闻、研报与行业背景',
        'market-context-researcher',
        'market-context-researcher',
        ('research-plan',),
    ),
    TeamTaskTemplate(
        'evidence-freeze',
        '冻结并规范化本次运行证据',
        SYSTEM_FREEZE_AGENT,
        'system-freeze',
        ('disclosure-research', 'market-context-research'),
    ),
    TeamTaskTemplate(
        'quality-analysis',
        '质量与护城河分析',
        'quality-analyst',
        'quality-analyst',
        ('evidence-freeze',),
    ),
    TeamTaskTemplate(
        'growth-analysis',
        '增长与变化分析',
        'growth-analyst',
        'growth-analyst',
        ('evidence-freeze',),
    ),
    TeamTaskTemplate(
        'evidence-judgement',
        '证据审计与裁决',
        'evidence-judge',
        'evidence-judge',
        ('quality-analysis', 'growth-analysis'),
    ),
    TeamTaskTemplate(
        'report-draft',
        '撰写证据约束报告',
        'report-writer',
        'report-writer',
        ('evidence-judgement',),
    ),
    TeamTaskTemplate(
        'compliance-review',
        '合规与引用复核',
        'compliance-reviewer',
        'compliance-reviewer',
        ('report-draft',),
    ),
)

# Relative shares of the run-wide hard budget.  They sum to 1.0 and are
# persisted on every team_task so the UI and recovery path do not need to
# infer allocations from prompts or Matrix messages.
TEAM_TASK_BUDGET_WEIGHTS = {
    'research-plan': 0.05,
    'disclosure-research': 0.15,
    'market-context-research': 0.15,
    'evidence-freeze': 0.025,
    'quality-analysis': 0.15,
    'growth-analysis': 0.15,
    'evidence-judgement': 0.15,
    'report-draft': 0.125,
    'compliance-review': 0.05,
}


def team_task_budget_allocations(
    total_budget_cny: float,
    analysis_mode: str,
) -> Dict[str, float]:
    """Return allocations that sum to the run hard limit for either DAG.

    Direct mode retains the two analyst rows as durable ``skipped`` recovery
    records but assigns them no spend.  Their shares are proportionally
    redistributed across the seven executable nodes so Matrix contracts, the
    SQLite UI and backend admission policy expose one consistent budget map.
    """

    mode = str(analysis_mode or 'evidence_debate')
    active = {
        key: weight
        for key, weight in TEAM_TASK_BUDGET_WEIGHTS.items()
        if mode != 'direct' or key not in {'quality-analysis', 'growth-analysis'}
    }
    active_weight = sum(active.values())
    total = max(0.0, float(total_budget_cny))
    allocations = {
        key: (
            round(total * active.get(key, 0.0) / active_weight, 6)
            if active_weight else 0.0
        )
        for key in TEAM_TASK_BUDGET_WEIGHTS
    }
    # Absorb decimal rounding into the last active task.  This keeps the
    # persisted sum exactly equal to the configured ceiling at micro-yuan
    # precision without ever allocating budget to a skipped analyst.
    if active:
        final_key = next(reversed(active))
        difference = round(total - sum(allocations.values()), 6)
        allocations[final_key] = round(allocations[final_key] + difference, 6)
    return allocations

# A failed physical Worker execution may be retried once. Writer/Reviewer can
# each execute up to four logical cycles (initial draft/reviews plus bounded
# revisions and one post-human-rejection chain); eight physical starts retain
# that crash retry without conflating infrastructure attempts with policy
# rounds.
TEAM_TASK_MAX_ATTEMPTS = {
    **{key: 2 for key in TEAM_TASK_BUDGET_WEIGHTS},
    'report-draft': 8,
    'compliance-review': 8,
}


TEAM_TERMINAL_STATUSES = frozenset({'published', 'rejected_terminal', 'failed'})
TEAM_STATUSES = frozenset({
    'pending', 'running', 'awaiting_approval', 'approved', 'changes_requested',
    *TEAM_TERMINAL_STATUSES,
})
TASK_STATUSES = frozenset({'pending', 'ready', 'running', 'completed', 'failed', 'skipped'})
HANDOFF_STATUSES = frozenset({'pending', 'accepted', 'completed', 'rejected'})
ARTIFACT_STATUSES = frozenset({'draft', 'awaiting_approval', 'rejected', 'approved', 'published'})
