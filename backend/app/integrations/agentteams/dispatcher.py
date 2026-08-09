"""Dispatch one admitted Chengzhu run to the AgentTeams Manager."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, Optional

from ...config import Config
from ...observability import traced_span
from ...services.agent_logger import AgentLogger
from ...team import (
    DEFAULT_TEAM_DAG,
    build_task_contract,
    team_task_budget_allocations,
)
from .client import AgentTeamsControllerClient, MatrixClient


class AgentTeamsDispatcher:
    def __init__(
        self,
        controller: Optional[AgentTeamsControllerClient] = None,
        matrix: Optional[MatrixClient] = None,
    ):
        self.controller = controller or AgentTeamsControllerClient()
        self.matrix = matrix or MatrixClient()

    @staticmethod
    def _safe_project_request(
        task_id: str,
        run_id: str,
        task_card: Dict[str, Any],
        *,
        trace_id: Optional[str] = None,
        expected_version: int = 0,
    ) -> str:
        symbols = [
            {
                'code': str(item.get('code') or '')[:12],
                'name': str(item.get('name') or '')[:80],
            }
            for item in (task_card.get('symbols') or [])[:8]
            if isinstance(item, dict)
        ]
        analysis_mode = str(task_card.get('analysis_mode') or 'direct')
        if analysis_mode not in {'direct', 'evidence_debate'}:
            raise ValueError('invalid_analysis_mode')
        allocations = team_task_budget_allocations(
            Config.LLM_COST_BUDGET_CNY,
            analysis_mode,
        )
        omitted = {'quality-analysis', 'growth-analysis'} if analysis_mode == 'direct' else set()
        workflow = []
        for template in DEFAULT_TEAM_DAG:
            if template.task_key in omitted:
                continue
            dependencies = [
                dependency for dependency in template.depends_on
                if dependency not in omitted
            ]
            # In direct mode the deterministic Judge consumes the frozen
            # context immediately; TeamHarness v1.2.0 has no ``skipped`` node
            # state, so the omitted analysts must not appear in its DAG.
            if analysis_mode == 'direct' and template.task_key == 'evidence-judgement':
                dependencies = ['evidence-freeze']
            workflow.append({
                'key': template.task_key,
                'assignee': template.assigned_agent,
                'depends_on': dependencies,
                'budget_cny': allocations[template.task_key],
            })
        contract = {
            'type': 'CHENGZHU_PROJECT_REQUESTED',
            'schema_version': 1,
            'project_id': f'chengzhu-{run_id}',
            'task_id': task_id,
            'run_id': run_id,
            'team': Config.AGENTTEAMS_TEAM_NAME,
            'analysis_mode': analysis_mode,
            'symbols': symbols,
            'time_window': task_card.get('time_window') or {},
            'mcp_server': 'chengzhu',
            'workflow': workflow,
            'mode_policy': (
                'use_dag_plan_evidence_debate'
                if analysis_mode == 'evidence_debate'
                else 'use_dag_plan_direct_without_analyst_nodes'
            ),
            'max_active_workers': Config.AGENTTEAMS_MAX_ACTIVE_WORKERS,
            'approval_authority': 'chengzhu-vue',
            'artifact_policy': 'matrix-references-only',
            'expected_version': int(expected_version),
            'task_contract': build_task_contract(
                goal='produce an evidence-bound financial information report',
                inputs=['confirmed_task_card', 'task_id', 'run_id'],
                expected_outputs=[
                    'frozen_evidence_manifest',
                    'audited_claim_verdict',
                    'reviewed_report_candidate',
                ],
                acceptance_criteria=[
                    'all factual claims resolve to frozen EvidenceCards',
                    'zero audit-failed claims enter the report',
                    'publication stops at the Vue approval gate',
                ],
                deadline={'timeout_seconds': Config.PIPELINE_TIMEOUT_SECONDS},
                budget={
                    'currency': 'CNY',
                    'limit_cny': Config.LLM_COST_BUDGET_CNY,
                    'task_allocations_cny': allocations,
                },
                artifact_refs=[],
                trace_id=str(trace_id or 'trace-unassigned'),
            ),
        }
        return (
            'PROJECT_REQUESTED: Chengzhu evidence-bound financial research\n\n'
            + json.dumps(contract, ensure_ascii=False, separators=(',', ':'))
            + f'\n\nUse the existing {Config.AGENTTEAMS_TEAM_NAME} team and its packaged DAG. '
              'Delegate only ready nodes, call the Chengzhu MCP tools under each '
              "role's allowlist, bridge evidence-freeze with the Leader MCP call plus "
              'TeamHarness accept_task_result, and stop at request_publish_approval. Never publish '
              'from Matrix; Vue approval is authoritative.'
        )

    def dispatch(
        self,
        task_id: str,
        run_id: str,
        task_card: Dict[str, Any],
        *,
        trace_id: Optional[str] = None,
        expected_version: int = 0,
    ) -> Dict[str, Any]:
        if not Config.AGENTTEAMS_ENABLED:
            raise RuntimeError('agentteams_disabled_for_live_run')
        logger = AgentLogger(task_id, agent='agentteams-dispatcher', run_id=run_id)
        with traced_span(
            'agentteams.dispatch',
            trace_id=trace_id,
            attributes={'task_id': task_id, 'run_id': run_id},
        ) as trace:
            state = self.controller.preflight()
            manager = state['manager']
            event_id = self.matrix.send_message(
                str(manager['roomID']),
                self._safe_project_request(
                    task_id,
                    run_id,
                    task_card,
                    trace_id=trace.trace_id,
                    expected_version=expected_version,
                ),
                mention_user_id=str(manager['matrixUserID']),
                transaction_id=f'chengzhu-{run_id}',
            )
            room_id = str(manager['roomID'])
            element_url = (
                f'{Config.AGENTTEAMS_ELEMENT_URL}/#/room/'
                f'{urllib.parse.quote(room_id, safe="")}'
            )
            logger.log(
                'team_dispatched',
                'dispatched',
                {
                    'team': Config.AGENTTEAMS_TEAM_NAME,
                    'manager': Config.AGENTTEAMS_MANAGER_NAME,
                    'room_id': room_id,
                    'element_url': element_url,
                },
                matrix_event_id=event_id,
                trace_id=trace.trace_id,
                span_id=trace.span_id,
            )
            return {
                'matrix_event_id': event_id,
                'matrix_room_id': room_id,
                'element_url': element_url,
                'trace_id': trace.trace_id,
                'span_id': trace.span_id,
                'team_phase': state['team'].get('phase'),
            }
