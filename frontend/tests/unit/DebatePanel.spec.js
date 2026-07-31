import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import DebatePanel from '../../src/components/DebatePanel.vue'

describe('DebatePanel audit and withdrawal display', () => {
  it('surfaces withdrawn claims, hard audit failures and challenge evidence', async () => {
    const wrapper = mount(DebatePanel, {
      props: {
        debate: {
          status: 'adjudicating',
          progress: { current_round: 2, current_role: 'evidence_auditor' },
          claims: [
            {
              claim_id: 'C1', role: 'quality_agent', round: 1,
              assertion: '这一观点已被反证', status: 'withdrawn', evidence_refs: ['E1'],
            },
            {
              claim_id: 'C2', role: 'growth_agent', round: 2,
              assertion: '期间不可比的观点不得接受', status: 'audit_failed', evidence_refs: ['E2'],
            },
          ],
          challenges: [
            {
              challenge_id: 'CH1', target_claim_id: 'C1', challenge_type: 'period_mismatch',
              argument: 'H1 与 Q1 口径不可比', status: 'upheld', evidence_refs: ['E2'],
            },
          ],
          audit: [{ claim_id: 'C2', hard_pass: false, hard_failures: ['period_mismatch'] }],
          challenge_audit: [
            {
              audit_type: 'challenge',
              challenge_id: 'CH1',
              hard_pass: false,
              issues: ['challenge:unsupported_value:42'],
            },
          ],
        },
      },
    })

    expect(wrapper.text()).toContain('1 撤回')
    expect(wrapper.text()).toContain('2 审计失败')
    expect(wrapper.text()).toContain('已撤回')
    expect(wrapper.text()).toContain('审计失败')
    expect(wrapper.text()).toContain('审计失败 · 无效反证')
    expect(wrapper.text()).toContain('该反证不参与裁决')
    expect(wrapper.text()).toContain('问题码：challenge:unsupported_value:42')
    expect(wrapper.text()).not.toContain('反证成立')
    expect(wrapper.text()).toContain('H1 与 Q1 口径不可比')
    expect(wrapper.findAll('.state.failed')).toHaveLength(3)
    expect(wrapper.find('.challenge-card').classes()).toContain('audit-failed')
    expect(wrapper.find('.challenge-audit-warning').exists()).toBe(true)

    await wrapper.find('.challenge-card .references button').trigger('click')
    expect(wrapper.emitted('evidence')).toEqual([['E2']])
  })
})
