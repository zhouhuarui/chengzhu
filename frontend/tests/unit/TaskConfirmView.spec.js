import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  confirm: vi.fn(),
  search: vi.fn(),
  push: vi.fn(),
}))

vi.mock('../../src/api/index.js', () => ({
  taskApi: {
    get: mocks.get,
    confirm: mocks.confirm,
  },
  securityApi: {
    search: mocks.search,
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { taskId: 'task_component' }, query: {} }),
  useRouter: () => ({ push: mocks.push }),
}))

import TaskConfirmView from '../../src/views/TaskConfirmView.vue'
import SecurityCombobox from '../../src/components/SecurityCombobox.vue'

const KODE = {
  sec_id: '688305.XSHG',
  code: '688305',
  name: '科德数控',
  exchange: 'XSHG',
  list_status: 'L',
}

const CATL = {
  sec_id: '300750.XSHE',
  code: '300750',
  name: '宁德时代',
  exchange: 'XSHE',
  list_status: 'L',
}

function taskPayload(overrides = {}) {
  return {
    deliverable: 'summary',
    analysis_mode: 'direct',
    symbols: [{
      sec_id: '300750.XSHE',
      code: '300750',
      name: '宁德时代',
      exchange: 'XSHE',
      list_status: 'L',
    }],
    time_window: { start: '2026-01-01', end: '2026-07-31' },
    info_types: ['financial_report'],
    focus_points: [],
    compare_dimensions: ['盈利质量', '现金流'],
    output_language_style: 'evidence_first',
    ...overrides,
  }
}

describe('TaskConfirmView analysis mode gating', () => {
  beforeEach(() => {
    mocks.get.mockResolvedValue({
      data: {
        task_card: taskPayload(),
      },
    })
    mocks.confirm.mockResolvedValue({ data: { run_id: 'run_component_debate' } })
    mocks.search.mockResolvedValue({ data: { items: [KODE] } })
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
    const submittedCard = mocks.confirm.mock.calls[0][1]
    expect(submittedCard.symbols[0]).toMatchObject({
      sec_id: '300750.XSHE',
      code: '300750',
      name: '宁德时代',
    })
    expect(submittedCard.symbols[0]).not.toHaveProperty('_row_id')
    expect(submittedCard.symbols[0]).not.toHaveProperty('_resolved')
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'TaskRun',
      params: { taskId: 'task_component' },
      query: { run_id: 'run_component_debate' },
    })
  })

  it('requires a dropdown selection and submits canonical security fields', async () => {
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [{ code: '688305', name: '科德数控' }] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    const submit = wrapper.get('button[type="submit"]')
    expect(submit.attributes()).toHaveProperty('disabled')
    expect(wrapper.text()).toContain('请从搜索结果中选择全部标的')
    expect(wrapper.find('.symbol-summary').exists()).toBe(false)
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(1)
    await wrapper.get('form').trigger('submit')
    expect(mocks.confirm).not.toHaveBeenCalled()

    wrapper.getComponent(SecurityCombobox).vm.$emit('update:modelValue', KODE)
    await wrapper.vm.$nextTick()
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(0)
    expect(wrapper.get('.symbol-summary').text()).toContain('科德数控')
    expect(wrapper.get('.symbol-summary').text()).toContain('688305')
    expect(submit.attributes()).not.toHaveProperty('disabled')

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.confirm).toHaveBeenCalledWith(
      'task_component',
      expect.objectContaining({ symbols: [KODE] }),
    )
  })

  it('shows a canonical target as a read-only summary until the user explicitly edits it', async () => {
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [KODE] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    const summary = wrapper.get('.symbol-summary')
    expect(summary.text()).toContain('科德数控')
    expect(summary.text()).toContain('688305')
    expect(summary.text()).toMatch(/上交所|XSHG/)
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(0)

    await wrapper.get('[aria-label="修改第 1 个标的"]').trigger('click')
    expect(wrapper.find('.symbol-summary').exists()).toBe(false)
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(1)
    expect(wrapper.getComponent(SecurityCombobox).props('modelValue')).toMatchObject(KODE)
  })

  it('cancels an in-progress target edit and restores the original canonical summary', async () => {
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [KODE] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    await wrapper.get('[aria-label="修改第 1 个标的"]').trigger('click')
    wrapper.getComponent(SecurityCombobox).vm.$emit('update:modelValue', null)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('请从搜索结果中选择全部标的')

    await wrapper.get('[aria-label="取消修改第 1 个标的"]').trigger('click')
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(0)
    expect(wrapper.get('.symbol-summary').text()).toContain('科德数控')
    expect(wrapper.get('.symbol-summary').text()).toContain('688305')
    expect(wrapper.text()).not.toContain('请从搜索结果中选择全部标的')
    expect(wrapper.get('button[type="submit"]').attributes()).not.toHaveProperty('disabled')
  })

  it('keeps canonical summaries intact while an added target is unresolved, then submits both', async () => {
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [KODE] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    const addButton = wrapper.findAll('button').find((button) => button.text().includes('添加标的'))
    expect(addButton).toBeTruthy()
    await addButton.trigger('click')

    expect(wrapper.findAll('.symbol-summary')).toHaveLength(1)
    expect(wrapper.get('.symbol-summary').text()).toContain('科德数控')
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(1)
    expect(wrapper.text()).toContain('请从搜索结果中选择全部标的')
    expect(wrapper.get('button[type="submit"]').attributes()).toHaveProperty('disabled')

    wrapper.getComponent(SecurityCombobox).vm.$emit('update:modelValue', CATL)
    await wrapper.vm.$nextTick()

    const summaries = wrapper.findAll('.symbol-summary')
    expect(summaries).toHaveLength(2)
    expect(summaries[0].text()).toContain('科德数控')
    expect(summaries[1].text()).toContain('宁德时代')
    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(0)

    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.confirm).toHaveBeenCalledWith(
      'task_component',
      expect.objectContaining({ symbols: [KODE, CATL] }),
    )
  })

  it('blocks duplicate securities already present in a task card', async () => {
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [KODE, { ...KODE }] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    expect(wrapper.text()).toContain('请勿重复添加同一标的')
    expect(wrapper.get('button[type="submit"]').attributes()).toHaveProperty('disabled')
    await wrapper.get('form').trigger('submit')
    expect(mocks.confirm).not.toHaveBeenCalled()
  })

  it('keeps the surviving canonical summary when an earlier row is removed', async () => {
    const second = {
      sec_id: '300750.XSHE',
      code: '300750',
      name: '宁德时代',
      exchange: 'XSHE',
      list_status: 'L',
    }
    mocks.get.mockResolvedValue({
      data: { task_card: taskPayload({ symbols: [KODE, second] }) },
    })
    const wrapper = mount(TaskConfirmView)
    await flushPromises()

    expect(wrapper.findAllComponents(SecurityCombobox)).toHaveLength(0)
    expect(wrapper.findAll('.symbol-summary')).toHaveLength(2)
    await wrapper.get('[aria-label="删除第 1 个标的"]').trigger('click')

    const summaries = wrapper.findAll('.symbol-summary')
    expect(summaries).toHaveLength(1)
    expect(summaries[0].text()).toContain('宁德时代')
    expect(summaries[0].text()).toContain('300750')
  })
})
