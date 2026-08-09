"""Persistent Agent Team contracts and state-store facade."""

from .contracts import (
    APPROVAL_AUTHORITY,
    DEFAULT_AGENT_ROLE_IDS,
    DEFAULT_TEAM_DAG,
    MAX_HUMAN_REJECTIONS,
    TASK_CONTRACT_FIELDS,
    TEAM_TASK_MAX_ATTEMPTS,
    SYSTEM_FREEZE_AGENT,
    TeamTaskTemplate,
    build_task_contract,
    team_task_budget_allocations,
)
from .errors import (
    TeamConflictError,
    TeamIdempotencyError,
    TeamInvariantError,
    TeamNotFoundError,
)
from .store import AgentTeamStore

__all__ = [
    'APPROVAL_AUTHORITY',
    'AgentTeamStore',
    'DEFAULT_AGENT_ROLE_IDS',
    'DEFAULT_TEAM_DAG',
    'MAX_HUMAN_REJECTIONS',
    'TASK_CONTRACT_FIELDS',
    'TEAM_TASK_MAX_ATTEMPTS',
    'SYSTEM_FREEZE_AGENT',
    'TeamConflictError',
    'TeamIdempotencyError',
    'TeamInvariantError',
    'TeamNotFoundError',
    'TeamTaskTemplate',
    'build_task_contract',
    'team_task_budget_allocations',
]
