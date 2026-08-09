import { afterEach, describe, expect, it, vi } from 'vitest'
import { service, taskApi } from '../../src/api/index.js'

describe('AgentTeams task API', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uses run-scoped team snapshot and cursor event endpoints', async () => {
    const get = vi.spyOn(service, 'get').mockResolvedValue({ data: {} })

    await taskApi.team('task-1', 'run-1')
    await taskApi.teamEvents('task-1', 'run-1', 'cursor-8')

    expect(get).toHaveBeenNthCalledWith(1, '/api/task/task-1/team', {
      params: { run_id: 'run-1' },
    })
    expect(get).toHaveBeenNthCalledWith(2, '/api/task/task-1/team/events', {
      params: { run_id: 'run-1', from_cursor: 'cursor-8' },
    })
  })

  it('posts approval and rollback payloads to the selected run', async () => {
    const post = vi.spyOn(service, 'post').mockResolvedValue({ data: {} })
    const approval = { decision: 'reject', reason: '证据不足', expected_version: 5 }
    const rollback = { target_run_id: 'run-0', reason: '恢复快照', expected_version: 6 }

    await taskApi.approval('task-1', 'run-1', approval)
    await taskApi.rollback('task-1', 'run-1', rollback)

    expect(post).toHaveBeenNthCalledWith(1, '/api/task/task-1/runs/run-1/approval', approval)
    expect(post).toHaveBeenNthCalledWith(2, '/api/task/task-1/runs/run-1/rollback', rollback)
  })
})
