import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  search: vi.fn(),
}))

vi.mock('../../src/api/index.js', () => ({
  securityApi: {
    search: mocks.search,
  },
}))

import SecurityCombobox from '../../src/components/SecurityCombobox.vue'

const KODE = {
  sec_id: '688305.XSHG',
  code: '688305',
  name: '科德数控',
  exchange: 'XSHG',
  list_status: 'L',
}

describe('SecurityCombobox', () => {
  beforeEach(() => {
    mocks.search.mockResolvedValue({ data: { items: [KODE] } })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it.each(['688305', '科德', 'KDSK'])(
    'forwards code, short-name or pinyin query %s without rewriting it',
    async (searchQuery) => {
      vi.useFakeTimers()
      const wrapper = mount(SecurityCombobox, {
        props: { modelValue: null, rowId: `query-${searchQuery}` },
      })

      await wrapper.get('[role="combobox"]').setValue(searchQuery)
      vi.advanceTimersByTime(200)
      await flushPromises()

      expect(mocks.search).toHaveBeenCalledWith(searchQuery, 10)
    },
  )

  it('debounces queries and atomically selects with the keyboard', async () => {
    vi.useFakeTimers()
    const wrapper = mount(SecurityCombobox, {
      props: { modelValue: null, rowId: 'row-one' },
    })
    const input = wrapper.get('[role="combobox"]')

    await input.setValue('K')
    await input.setValue('KD')
    await input.setValue('KDSK')
    vi.advanceTimersByTime(199)
    expect(mocks.search).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(mocks.search).toHaveBeenCalledTimes(1)
    expect(mocks.search).toHaveBeenCalledWith('KDSK', 10)
    expect(input.attributes('aria-expanded')).toBe('true')
    expect(input.attributes('aria-activedescendant')).toContain('security-options-row-one-option-0')

    await input.trigger('keydown', { key: 'Enter' })
    const selected = wrapper.emitted('update:modelValue').at(-1)[0]
    expect(selected).toEqual(KODE)
    expect(input.element.value).toBe('科德数控 · 688305')
    expect(input.attributes('aria-expanded')).toBe('false')
  })

  it('ignores an older response that arrives after a newer search', async () => {
    vi.useFakeTimers()
    const pending = new Map()
    mocks.search.mockImplementation((q) => new Promise((resolve) => pending.set(q, resolve)))
    const wrapper = mount(SecurityCombobox, {
      props: { modelValue: null, rowId: 'race-row' },
    })
    const input = wrapper.get('[role="combobox"]')

    await input.setValue('科')
    vi.advanceTimersByTime(200)
    await flushPromises()
    expect(pending.has('科')).toBe(true)

    await input.setValue('科德')
    vi.advanceTimersByTime(200)
    await flushPromises()
    expect(pending.has('科德')).toBe(true)

    pending.get('科德')({ data: { items: [KODE] } })
    await flushPromises()
    expect(wrapper.text()).toContain('科德数控')

    pending.get('科')({
      data: {
        items: [{ ...KODE, sec_id: '000001.XSHE', code: '000001', name: '过期结果' }],
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('科德数控')
    expect(wrapper.text()).not.toContain('过期结果')
  })

  it('keeps Escape closed when an in-flight response arrives later', async () => {
    vi.useFakeTimers()
    let resolveSearch
    mocks.search.mockReturnValue(new Promise((resolve) => { resolveSearch = resolve }))
    const wrapper = mount(SecurityCombobox, {
      props: { modelValue: null, rowId: 'escape-row' },
    })
    const input = wrapper.get('[role="combobox"]')

    await input.setValue('688305')
    vi.advanceTimersByTime(200)
    await flushPromises()
    await input.trigger('keydown', { key: 'Escape' })
    expect(input.attributes('aria-expanded')).toBe('false')

    resolveSearch({ data: { items: [KODE] } })
    await flushPromises()
    expect(input.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.dropdown').exists()).toBe(false)
  })

  it('clears a selected value and prevents selecting an excluded code', async () => {
    vi.useFakeTimers()
    const wrapper = mount(SecurityCombobox, {
      props: {
        modelValue: { ...KODE, _resolved: true },
        excludedCodes: ['688305'],
        rowId: 'excluded-row',
      },
    })

    expect(wrapper.get('[role="combobox"]').element.value).toBe('科德数控 · 688305')
    await wrapper.get('[aria-label="清除已选证券"]').trigger('click')
    expect(wrapper.emitted('update:modelValue').at(-1)[0]).toBeNull()

    const input = wrapper.get('[role="combobox"]')
    await input.setValue('688305')
    vi.advanceTimersByTime(200)
    await flushPromises()

    const option = wrapper.get('.security-option')
    expect(option.attributes('aria-disabled')).toBe('true')
    const emissionsBeforeEnter = wrapper.emitted('update:modelValue').length
    await input.trigger('keydown', { key: 'ArrowDown' })
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('update:modelValue')).toHaveLength(emissionsBeforeEnter)
  })

  it('does not treat an arbitrary code/name pair as a verified selection', () => {
    const wrapper = mount(SecurityCombobox, {
      props: {
        modelValue: { code: '000001', name: '科德数控' },
        rowId: 'unverified-row',
      },
    })

    expect(wrapper.get('[role="combobox"]').element.value).toBe('科德数控')
    expect(wrapper.find('.combobox-control').classes()).not.toContain('selected')
  })
})
