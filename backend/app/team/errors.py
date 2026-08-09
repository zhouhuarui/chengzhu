"""Domain errors mapped to stable HTTP responses by the team API."""

from __future__ import annotations


class TeamError(RuntimeError):
    code = 'team_error'


class TeamNotFoundError(TeamError):
    code = 'team_not_found'


class TeamConflictError(TeamError):
    code = 'state_version_conflict'

    def __init__(self, message: str, *, current_version: int | None = None):
        super().__init__(message)
        self.current_version = current_version


class TeamIdempotencyError(TeamConflictError):
    code = 'idempotency_key_reused'


class TeamInvariantError(TeamError):
    code = 'team_invariant_violation'
