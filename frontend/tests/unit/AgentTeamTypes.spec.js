import { describe, expect, it } from 'vitest'
import { normalizeTeamSnapshot } from '../../src/types/agentTeam.js'

describe('AgentTeams compatibility normalization', () => {
  it('normalizes durable nested snapshots and infers worker state from tasks', () => {
    const snapshot = normalizeTeamSnapshot({
      data: {
        team: {
          team_id: 'team-1',
          status: 'awaiting_approval',
          state_version: 11,
          config: {
            element_url: 'https://element.example/#/room/team-1',
          },
        },
        agent_roles: [
          'research-lead',
          'disclosure-researcher',
          'market-context-researcher',
          'quality-analyst',
          'growth-analyst',
          'evidence-judge',
          'report-writer',
          'compliance-reviewer',
        ],
        tasks: [
          {
            team_task_id: 'team-1:research-plan',
            task_key: 'research-plan',
            title: '拆解研究问题与证据需求',
            assigned_agent: 'research-lead',
            status: 'completed',
            depends_on: [],
            attempt_count: 2,
            budget_cny: 0.18,
          },
          {
            team_task_id: 'team-1:disclosure-research',
            task_key: 'disclosure-research',
            title: '采集公告与财报',
            assigned_agent: 'disclosure-researcher',
            status: 'running',
            depends_on: ['team-1:research-plan'],
          },
        ],
        handoffs: [
          {
            handoff_id: 'handoff-1',
            from_agent: 'research-lead',
            to_agent: 'disclosure-researcher',
            target_task_id: 'team-1:disclosure-research',
            status: 'accepted',
            payload: { summary: '证据需求已交接' },
          },
        ],
        artifacts: [
          {
            artifact_id: 'artifact-2',
            artifact_type: 'report',
            requires_approval: true,
            status: 'awaiting_approval',
            metadata: { summary: '报告等待发布' },
          },
        ],
        event_cursor: 19,
      },
    }, { status: 'collecting' })

    expect(snapshot.stateVersion).toBe(11)
    expect(snapshot.managerStatus).toBe('waiting_approval')
    expect(snapshot.tasks[0].id).toBe('team-1:research-plan')
    expect(snapshot.tasks[0]).toMatchObject({ attemptCount: 2, budgetCny: 0.18 })
    expect(snapshot.members.find((member) => member.id === 'disclosure-researcher')?.status).toBe('running')
    expect(snapshot.events[0]).toMatchObject({
      actor: 'research-lead',
      target: 'disclosure-researcher',
      message: '证据需求已交接',
    })
    expect(snapshot.approval).toMatchObject({
      required: true,
      artifactId: 'artifact-2',
      expectedVersion: 11,
    })
    expect(snapshot.elementUrl).toBe('https://element.example/#/room/team-1')
    expect(snapshot.nextCursor).toBe(19)
  })
})
