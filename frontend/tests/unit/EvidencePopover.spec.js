import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import EvidencePopover from '../../src/components/EvidencePopover.vue'

describe('EvidencePopover standalone evidence card', () => {
  it('renders a frozen evidence card without requiring an inline citation click', async () => {
    const wrapper = mount(EvidencePopover, {
      props: {
        id: '2',
        standalone: true,
        card: {
          display_id: 'E2',
          title: '比亚迪 2026 年一季报',
          source_name: '上市公司公告',
          excerpt: '一季报与半年报累计口径不可直接比较。',
        },
      },
    })

    expect(wrapper.find('.evidence-ref').exists()).toBe(false)
    expect(wrapper.find('.popover.is-standalone').exists()).toBe(true)
    expect(wrapper.text()).toContain('比亚迪 2026 年一季报')
    expect(wrapper.text()).toContain('一季报与半年报累计口径不可直接比较')

    await wrapper.get('button[aria-label="关闭证据详情"]').trigger('click')
    expect(wrapper.emitted('close')).toEqual([[]])
  })
})
