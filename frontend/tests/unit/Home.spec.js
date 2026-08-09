import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  prefill: vi.fn(),
  push: vi.fn(),
}))

vi.mock('../../src/api/index.js', () => ({
  taskApi: {
    list: mocks.list,
    create: mocks.create,
  },
  memoryApi: {
    prefill: mocks.prefill,
  },
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
}))

import Home from '../../src/views/Home.vue'

describe('Home task intent entry', () => {
  beforeEach(() => {
    mocks.list.mockResolvedValue({ data: [] })
    mocks.prefill.mockResolvedValue({ data: { watch_symbols: ['300750'] } })
  })

  it('explains that the next step is review-only and renders string watch symbols', async () => {
    const wrapper = mount(Home)
    await flushPromises()

    expect(wrapper.text()).toContain('下一步只核对系统识别结果，无需重复录入')
    expect(wrapper.get('.actions .btn').text()).toContain('下一步：核对任务卡')

    const watchSymbol = wrapper.get('.prefill .chip')
    expect(watchSymbol.text()).toBe('300750')
    await watchSymbol.trigger('click')
    expect(wrapper.get('textarea').element.value).toBe('300750')
  })
})
