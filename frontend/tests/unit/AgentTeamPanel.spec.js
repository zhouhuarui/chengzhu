import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  team: vi.fn(),
  teamEvents: vi.fn(),
  approval: vi.fn(),
  rollback: vi.fn(),
}))

vi.mock('../../src/api/index.js', () => ({
  taskApi: mocks,
}))

import AgentTeamPanel from '../../src/components/AgentTeamPanel.vue'

const MEMBERS = [
  'research-lead',
  'disclosure-researcher',
  'market-context-researcher',
  'quality-analyst',
  'growth-analyst',
  'evidence-judge',
  'report-writer',
  'compliance-reviewer',
].map((id, index) => ({
  id,
  status: index < 3 ? 'completed' : index === 3 ? 'running' : 'pending',
}))

function liveTeamPayload() {
  return {
    data: {
      state_version: 7,
      members: MEMBERS,
      events: [
        {
          id: 'event-1',
          cursor: '1',
          type: 'handoff',
          actor: 'research-lead',
          target: 'quality-analyst',
          message: '冻结证据已交接',
          timestamp: '2026-08-04T08:00:00Z',
        },
      ],
      approval: {
        required: true,
        status: 'waiting_approval',
        title: '批准使用降级数据',
        summary: '结构化数据源不可用，请确认是否继续。',
        requested_by: 'research-lead',
        expected_version: 7,
      },
      metrics: {
        budget: { spent_cny: 0.42, limit_cny: 2 },
        degraded: true,
        degradation_reasons: ['datayes_unavailable'],
      },
      rollback: {
        allowed: true,
        target_run_id: 'run_previous',
      },
      element_url: 'https://app.element.io/#/room/room-id:example.org',
    },
  }
}

describe('AgentTeamPanel', () => {
  beforeEach(() => {
    mocks.team.mockResolvedValue(liveTeamPayload())
    mocks.teamEvents.mockResolvedValue({
      data: {
        events: [
          {
            id: 'event-2',
            cursor: '2',
            type: 'task_started',
            actor: 'quality-analyst',
            message: '开始稳健性分析',
          },
        ],
        next_cursor: '2',
      },
    })
    mocks.approval.mockResolvedValue({ data: { state_version: 8 } })
    mocks.rollback.mockResolvedValue({ data: { state_version: 8 } })
  })

  it('renders the fixed eight workers, DAG, audit timeline, badges and Element deep link', async () => {
    const wrapper = mount(AgentTeamPanel, {
      props: {
        taskId: 'task_team',
        runId: 'run_current',
        status: 'debating',
        progressDetail: { stage: 'debating', analysis_mode: 'evidence_debate' },
        pollInterval: 60_000,
      },
    })
    await flushPromises()

    expect(wrapper.findAll('[data-role-id]')).toHaveLength(8)
    expect(wrapper.text()).toContain('研究负责人')
    expect(wrapper.text()).toContain('合规审校员')
    expect(wrapper.findAll('.task-row')).toHaveLength(9)
    expect(wrapper.text()).toContain('采集公告、财报与公司披露')
    expect(wrapper.text()).toContain('冻结证据已交接')
    expect(wrapper.text()).toContain('开始稳健性分析')
    expect(wrapper.text()).toContain('已降级 · datayes_unavailable')
    expect(wrapper.text()).toContain('后端账本 ¥0.42 / ¥2.00')
    expect(wrapper.get('.element-link').attributes('href')).toBe('https://app.element.io/#/room/room-id:example.org')
    expect(mocks.team).toHaveBeenCalledWith('task_team', 'run_current')
    expect(mocks.teamEvents).toHaveBeenCalledWith('task_team', 'run_current', '')
  })

  it('submits versioned approval and rollback contracts', async () => {
    const wrapper = mount(AgentTeamPanel, {
      props: {
        taskId: 'task_actions',
        runId: 'run_current',
        status: 'debating',
        pollInterval: 60_000,
      },
    })
    await flushPromises()

    await wrapper.get('[data-testid="approval-reason"]').setValue('证据缺口已知悉')
    await wrapper.get('[data-testid="approve-button"]').trigger('click')
    await flushPromises()

    expect(mocks.approval).toHaveBeenCalledWith('task_actions', 'run_current', {
      decision: 'approve',
      reason: '证据缺口已知悉',
      expected_version: 7,
    })

    await wrapper.get('[data-testid="rollback-toggle"]').trigger('click')
    await wrapper.get('[data-testid="rollback-reason"]').setValue('回到未使用降级数据的版本')
    await wrapper.get('[data-testid="rollback-submit"]').trigger('submit')
    await flushPromises()

    expect(mocks.rollback).toHaveBeenCalledWith('task_actions', 'run_current', {
      target_run_id: 'run_previous',
      reason: '回到未使用降级数据的版本',
      expected_version: 7,
    })
  })

  it('filters task, handoff, Skill/MCP and approval events and shows task runtime limits', async () => {
    const payload = liveTeamPayload()
    payload.data.tasks = [
      {
        id: 'research-plan',
        title: '拆解研究问题与证据需求',
        assignee: 'research-lead',
        status: 'running',
        depends_on: [],
        attempt_count: 2,
        budget_cny: 0.24,
      },
    ]
    payload.data.events = [
      { id: 'event-task', cursor: '1', type: 'task_started', actor: 'research-lead', message: '开始拆解任务' },
      { id: 'event-handoff', cursor: '2', type: 'handoff_accepted', actor: 'research-lead', target: 'quality-analyst', message: '交接证据' },
      { id: 'event-mcp', cursor: '3', type: 'mcp_tool_result', actor: 'quality-analyst', message: '工具返回成功' },
      { id: 'event-approval', cursor: '4', type: 'human.approved', actor: 'vue-user', message: '人工批准' },
    ]
    mocks.team.mockResolvedValue(payload)
    mocks.teamEvents.mockResolvedValue({ data: { events: [], next_cursor: '4' } })

    const wrapper = mount(AgentTeamPanel, {
      props: {
        taskId: 'task_filters',
        runId: 'run_current',
        status: 'running',
        pollInterval: 60_000,
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('尝试 2 次')
    expect(wrapper.text()).toContain('预算 ¥0.24')
    expect(wrapper.findAll('.event-row')).toHaveLength(4)

    const filter = wrapper.get('[data-testid="event-filter"]')
    await filter.setValue('task')
    expect(wrapper.findAll('.event-row')).toHaveLength(1)
    expect(wrapper.get('.event-row').attributes('data-event-category')).toBe('task')
    expect(wrapper.text()).toContain('开始拆解任务')

    await filter.setValue('handoff')
    expect(wrapper.get('.event-row').attributes('data-event-category')).toBe('handoff')
    expect(wrapper.text()).toContain('交接证据')

    await filter.setValue('skill-mcp')
    expect(wrapper.get('.event-row').attributes('data-event-category')).toBe('skill-mcp')
    expect(wrapper.text()).toContain('工具返回成功')

    await filter.setValue('approval')
    expect(wrapper.get('.event-row').attributes('data-event-category')).toBe('approval')
    expect(wrapper.text()).toContain('人工批准')

    await filter.setValue('all')
    expect(wrapper.findAll('.event-row')).toHaveLength(4)
  })

  it('turns a missing team endpoint into a read-only legacy replay', async () => {
    mocks.team.mockRejectedValue({ response: { status: 404 } })

    const wrapper = mount(AgentTeamPanel, {
      props: {
        taskId: 'task_legacy',
        runId: 'run_legacy',
        status: 'collecting',
        progressDetail: { stage: 'collecting' },
        logLines: [
          {
            agent: 'collector_news',
            action: 'handoff',
            elapsed_seconds: 4,
            details: { message: '旧日志交接事件' },
          },
        ],
        pollInterval: 60_000,
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('兼容回放')
    expect(wrapper.text()).toContain('旧日志交接事件')
    expect(wrapper.findAll('[data-role-id]')).toHaveLength(8)
    expect(wrapper.get('[data-testid="rollback-toggle"]').attributes('disabled')).toBeDefined()
    expect(mocks.teamEvents).not.toHaveBeenCalled()
  })
})
