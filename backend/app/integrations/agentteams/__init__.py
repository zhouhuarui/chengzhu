"""AgentTeams v1.2.0 controller and Matrix integration."""

from .client import AgentTeamsClientError, AgentTeamsControllerClient, MatrixClient
from .dispatcher import AgentTeamsDispatcher

__all__ = [
    'AgentTeamsClientError',
    'AgentTeamsControllerClient',
    'AgentTeamsDispatcher',
    'MatrixClient',
]

