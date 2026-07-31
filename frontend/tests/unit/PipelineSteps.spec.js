import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import PipelineSteps from '../../src/components/PipelineSteps.vue'

describe('PipelineSteps debate stages', () => {
  it('always shows normalization and only inserts debate/adjudication for debate runs', async () => {
    const wrapper = mount(PipelineSteps, {
      props: {
        status: 'analyzing',
        progress: 70,
        progressDetail: { stage: 'analyzing', analysis_mode: 'direct' },
      },
    })

    expect(wrapper.findAll('.step-title').map((item) => item.text())).toEqual([
      '解析', '采集', '建图', '标准化', '分析', '审校', '装配',
    ])

    await wrapper.setProps({
      status: 'debating',
      progress: 76,
      progressDetail: {
        stage: 'debating',
        analysis_mode: 'evidence_debate',
        debate: {
          current_round: 2,
          current_role: 'growth_agent',
          claim_count: 4,
          challenge_count: 2,
          withdrawn_count: 1,
          audit_failures: 1,
        },
      },
    })

    expect(wrapper.findAll('.step-title').map((item) => item.text())).toEqual([
      '解析', '采集', '建图', '标准化', '辩论', '裁决', '分析', '审校', '装配',
    ])
    expect(wrapper.text()).toContain('R2')
    expect(wrapper.text()).toContain('成长与变化视角')
    expect(wrapper.text()).toContain('观点 4')
    expect(wrapper.text()).toContain('反证 2')
    expect(wrapper.text()).toContain('撤回 1')
    expect(wrapper.text()).toContain('审计失败 1')
  })
})
