import { expect, test } from '@playwright/test'

const taskId = 'task_e2e'
const debateRunId = 'run_debate'
const directRunId = 'run_direct'

const evidenceItems = [
  {
    card_id: 1,
    evidence_uid: 'ev_h1_report',
    display_id: 'E1',
    source_type: 'financial_report',
    source_name: '上市公司公告',
    publish_time: '2026-07-01T09:00:00+08:00',
    title: '宁德时代 2026 年半年报',
    excerpt: '半年报披露营业收入及归母净利润。',
    url: 'https://example.test/evidence/E1',
  },
  {
    card_id: 2,
    evidence_uid: 'ev_period_mismatch',
    display_id: 'E2',
    source_type: 'financial_report',
    source_name: '上市公司公告',
    publish_time: '2026-07-02T09:00:00+08:00',
    title: '比亚迪 2026 年一季报',
    excerpt: '一季报口径不能与半年报累计口径直接比较。',
    url: 'https://example.test/evidence/E2',
  },
]

const debatePayload = {
  run_id: debateRunId,
  status: 'adjudicating',
  progress: { current_round: 2, current_role: 'evidence_auditor' },
  claims: [
    {
      claim_id: 'C1',
      role: 'quality_agent',
      round: 1,
      assertion: '半年度数据可以直接与一季度比较',
      status: 'withdrawn',
      evidence_refs: ['E1'],
    },
    {
      claim_id: 'C2',
      role: 'growth_agent',
      round: 2,
      assertion: '必须先统一期间口径再进行比较',
      status: 'accepted',
      evidence_refs: ['E1'],
    },
  ],
  challenges: [
    {
      challenge_id: 'CH1',
      target_claim_id: 'C1',
      challenge_type: 'period_mismatch',
      argument: 'H1 与 Q1 口径不可比',
      status: 'upheld',
      evidence_refs: ['E2'],
    },
  ],
  audit: [{ claim_id: 'C1', hard_pass: false, hard_failures: ['period_mismatch'] }],
  verdict: {
    consensus_facts: [{ statement: '公司已披露半年度财务数据', evidence_refs: ['E1'] }],
    withdrawn_claims: ['C1 因期间口径不一致而撤回'],
    unresolved_disagreements: [],
    evidence_gaps: [],
  },
}

function reportFor(runId) {
  const debate = runId !== directRunId
  const title = debate ? '证据辩论报告' : '直接分析报告'
  return {
    task_id: taskId,
    run_id: debate ? debateRunId : directRunId,
    title,
    summary: debate ? '经过两轮证据辩论与硬校验。' : '使用直接分析流程。',
    analysis_mode: debate ? 'evidence_debate' : 'direct',
    debate_status: debate ? 'completed' : null,
    sections: [
      {
        title: '共识事实',
        content: debate
          ? '宁德时代已披露半年度财务数据[E1]。'
          : '宁德时代已披露半年度财务数据[E1]。',
      },
    ],
    markdown: `# ${title}\n\n宁德时代已披露半年度财务数据[E1]。`,
    sources: evidenceItems,
  }
}

async function fulfillJson(route, payload, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(payload),
  })
}

test('debate selection, two rounds, challenge evidence and run switching', async ({ page }) => {
  let confirmedMode = null
  let statusCalls = 0
  let releaseRoundTwo
  const roundTwoGate = new Promise((resolve) => { releaseRoundTwo = resolve })

  await page.addInitScript(() => {
    localStorage.setItem('chengzhu_run_guide_seen', '1')
  })

  // 限定为站点根路径 /api，避免误拦截 Vite 的 /src/api/index.js 模块。
  await page.route('http://127.0.0.1:4173/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path === '/api/meta/disclaimer') {
      return fulfillJson(route, { success: true, data: { disclaimer: '仅做信息整理。' } })
    }
    if (path === '/api/tracking/notifications') {
      return fulfillJson(route, { success: true, data: { count: 0 } })
    }
    if (path === `/api/task/${taskId}` && request.method() === 'GET') {
      return fulfillJson(route, {
        success: true,
        data: {
          task_id: taskId,
          task_card: {
            deliverable: 'summary',
            analysis_mode: 'direct',
            symbols: [{ code: '300750', name: '宁德时代' }],
            time_window: { start: '2026-01-01', end: '2026-07-31' },
            info_types: ['financial_report'],
            focus_points: ['财务表现'],
          },
        },
      })
    }
    if (path === `/api/task/${taskId}/confirm` && request.method() === 'POST') {
      const body = request.postDataJSON()
      confirmedMode = body?.task_card?.analysis_mode
      return fulfillJson(route, {
        success: true,
        data: { task_id: taskId, run_id: debateRunId, status: 'collecting' },
      })
    }
    if (path === `/api/task/${taskId}/status`) {
      statusCalls += 1
      const round = statusCalls === 1 ? 1 : 2
      if (round === 2) await roundTwoGate
      return fulfillJson(route, {
        success: true,
        data: {
          status: 'debating',
          progress: round === 1 ? 72 : 78,
          run_id: debateRunId,
          message: `第 ${round} 轮辩论`,
          progress_detail: {
            stage: 'debating',
            analysis_mode: 'evidence_debate',
            debate: {
              current_round: round,
              current_role: round === 1 ? 'quality_agent' : 'growth_agent',
              claim_count: round === 1 ? 1 : 2,
              challenge_count: round === 1 ? 0 : 1,
              withdrawn_count: round === 1 ? 0 : 1,
              audit_failures: round === 1 ? 0 : 1,
            },
          },
        },
      })
    }
    if (path === `/api/task/${taskId}/agent-log`) {
      return fulfillJson(route, { success: true, data: { lines: [], next_line: 0 } })
    }
    if (path === `/api/task/${taskId}/debate`) {
      return fulfillJson(route, { success: true, data: debatePayload })
    }
    if (path === `/api/task/${taskId}/evidence`) {
      return fulfillJson(route, { success: true, data: { run_id: url.searchParams.get('run_id'), items: evidenceItems } })
    }
    if (path === `/api/task/${taskId}/graph`) {
      return fulfillJson(route, { success: true, data: { nodes: [], edges: [] } })
    }
    if (path === `/api/task/${taskId}/runs`) {
      return fulfillJson(route, {
        success: true,
        data: [
          {
            run_id: debateRunId,
            analysis_mode: 'evidence_debate',
            status: 'completed',
            created_at: '2026-07-31T20:00:00+08:00',
          },
          {
            run_id: directRunId,
            analysis_mode: 'direct',
            status: 'completed',
            created_at: '2026-07-31T19:00:00+08:00',
          },
        ],
      })
    }
    if (path === `/api/report/${taskId}` && request.method() === 'GET') {
      const runId = url.searchParams.get('run_id') || debateRunId
      return fulfillJson(route, { success: true, data: reportFor(runId) })
    }
    if (path === `/api/report/${taskId}/review-log`) {
      return fulfillJson(route, { success: true, data: [] })
    }
    if (path === `/api/feedback/${taskId}`) {
      return fulfillJson(route, { success: true, data: [] })
    }

    return fulfillJson(route, { success: true, data: {} })
  })

  await page.goto(`/task/${taskId}/confirm`)
  await expect(page.getByRole('heading', { name: '确认任务卡' })).toBeVisible()
  await page.getByText('多视角证据辩论', { exact: true }).click()
  await expect(page.getByText(/DeepSeek/)).toBeVisible()
  await page.getByRole('button', { name: '开始研究' }).click()

  await expect.poll(() => confirmedMode).toBe('evidence_debate')
  await expect(page).toHaveURL(new RegExp(`/task/${taskId}\\?run_id=${debateRunId}`))
  await expect(page.getByText('R1', { exact: true })).toBeVisible()
  releaseRoundTwo()
  await expect(page.getByText('R2', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '辩论面板' }).click()
  await expect(page.getByText('H1 与 Q1 口径不可比')).toBeVisible()
  await expect(page.getByText('第 2 回合 · 证据审计')).toBeVisible()
  await page.locator('.debate-scroll .challenge-card .references button').click()
  await expect(page.locator('.evidence-overlay .popover h4')).toHaveText('比亚迪 2026 年一季报')
  await page.locator('.evidence-overlay').getByRole('button', { name: '关闭证据详情' }).click()

  await page.goto(`/report/${taskId}?run_id=${debateRunId}`)
  await expect(page.locator('.toolbar').getByRole('heading', { name: '证据辩论报告' })).toBeVisible()
  await page.getByRole('button', { name: '辩论记录' }).click()
  const debateDialog = page.locator('.debate-dialog')
  await expect(debateDialog.getByText('H1 与 Q1 口径不可比')).toBeVisible()
  await debateDialog.locator('.challenge-card .references button').click()

  // E2 不在最终报告正文中，仍应从冻结证据索引直接展示证据卡。
  await expect(page.locator('.evidence-card-overlay .popover h4')).toHaveText('比亚迪 2026 年一季报')
  await page.locator('.evidence-card-overlay').getByRole('button', { name: '关闭证据详情' }).click()

  const runSelector = page.getByLabel('历史 Run')
  await runSelector.selectOption(directRunId)
  await expect(page.locator('.toolbar').getByRole('heading', { name: '直接分析报告' })).toBeVisible()
  await expect(page.getByText('直接分析', { exact: true })).toBeVisible()

  await runSelector.selectOption(debateRunId)
  await expect(page.locator('.toolbar').getByRole('heading', { name: '证据辩论报告' })).toBeVisible()
  await expect(page.getByText('证据辩论', { exact: true })).toBeVisible()
})

test('direct normalizing run does not poll or expose debate panel', async ({ page }) => {
  const directTaskId = 'task_direct_normalizing'
  const directRunId = 'run_direct_normalizing'
  let debateCalls = 0

  await page.addInitScript(() => {
    localStorage.setItem('chengzhu_run_guide_seen', '1')
  })

  await page.route('http://127.0.0.1:4173/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path === '/api/meta/disclaimer') {
      return fulfillJson(route, { success: true, data: { disclaimer: '仅做信息整理。' } })
    }
    if (path === '/api/tracking/notifications') {
      return fulfillJson(route, { success: true, data: { count: 0 } })
    }
    if (path === `/api/task/${directTaskId}/status`) {
      return fulfillJson(route, {
        success: true,
        data: {
          status: 'normalizing',
          progress: 63,
          run_id: directRunId,
          message: '财务事实标准化中',
          progress_detail: {
            stage: 'normalizing',
            analysis_mode: 'direct',
            run_id: directRunId,
          },
        },
      })
    }
    if (path === `/api/task/${directTaskId}/debate`) {
      debateCalls += 1
      return fulfillJson(route, {
        success: true,
        data: {
          run_id: directRunId,
          status: null,
          progress: { status: null, current_round: 0, current_role: null },
          claims: [],
          challenges: [],
          audit: [],
          verdict: null,
        },
      })
    }
    if (path === `/api/task/${directTaskId}/agent-log`) {
      return fulfillJson(route, { success: true, data: { lines: [], next_line: 0 } })
    }
    if (path === `/api/task/${directTaskId}/evidence`) {
      return fulfillJson(route, { success: true, data: { run_id: directRunId, items: [] } })
    }
    if (path === `/api/task/${directTaskId}/graph`) {
      return fulfillJson(route, { success: true, data: { nodes: [], edges: [] } })
    }
    return fulfillJson(route, { success: true, data: {} })
  })

  await page.goto(`/task/${directTaskId}?run_id=${directRunId}`)
  await expect(page.getByText('财务事实标准化中')).toBeVisible()
  await expect(page.getByRole('button', { name: '辩论面板' })).toHaveCount(0)
  await page.waitForTimeout(2200)
  expect(debateCalls).toBe(0)
  await expect(page.getByRole('button', { name: '辩论面板' })).toHaveCount(0)
})
