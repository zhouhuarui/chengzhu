import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  confirm: vi.fn(),
  push: vi.fn(),
}))

vi.mock('../../src/api/index.js', () => ({
  taskApi: {
    get: mocks.get,
    confirm: mocks.confirm,
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { taskId: 'task_component' }, query: {} }),
  useRouter: () => ({ push: mocks.push }),
}))

import TaskConfirmView from '../../src/views/TaskConfirmView.vue'

describe('TaskConfirmView analysis mode gating', () => {
  beforeEach(() => {
    mocks.get.mockResolvedValue({
      data: {
        task_card: {
          deliverable: 'summary',
          analysis_mode: 'direct',
          symbols: [{ code: '300750', name: '宁德时代' }],
          time_window: { start: '2026-01-01', end: '2026-07-31' },
          info_types: ['financial_report'],
          focus_points: [],
          compare_dimensions: ['盈利质量', '现金流'],
          output_language_style: 'evidence_first',
        },
      },
    })
    mocks.confirm.mockResolvedValue({ data: { run_id: 'run_component_debate' } })
  })

  it('offers debate for summary/compare and forces tracking back to direct', async () => {
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    expect(wrapper.text()).toContain('分析方式')
    expect(wrapper.text()).toContain('多视角证据辩论')

    const debate = wrapper.get('input[value="evidence_debate"]')
    await debate.setValue(true)
    expect(debate.element.checked).toBe(true)
    expect(wrapper.text()).toContain('图片页可能发送给百炼 Qwen-VL')

    await wrapper.get('input[value="tracking"]').setValue(true)
    expect(wrapper.find('input[value="evidence_debate"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('追踪任务首版仅支持直接分析')
    expect(wrapper.text()).toContain('候选图片页可能发送给百炼 Qwen-VL')

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledWith(
      'task_component',
      expect.objectContaining({
        deliverable: 'tracking',
        analysis_mode: 'direct',
        compare_dimensions: ['盈利质量', '现金流'],
        output_language_style: 'evidence_first',
      }),
    )
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'TaskRun',
      params: { taskId: 'task_component' },
      query: { run_id: 'run_component_debate' },
    })
  })
})
