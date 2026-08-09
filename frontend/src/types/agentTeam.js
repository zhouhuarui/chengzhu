/**
 * AgentTeams UI contracts and compatibility normalizers.
 *
 * The backend contract is intentionally permissive while older runs are still
 * readable.  Everything exported from this module returns one stable shape to
 * the Vue layer, regardless of whether data came from the live controller or
 * from a persisted replay.
 */

export const AGENT_TEAM_ROLES = Object.freeze([
  {
    id: 'research-lead',
    label: '研究负责人',
    shortLabel: '负责人',
    description: '拆解任务、编排协作与控制交接',
  },
  {
    id: 'disclosure-researcher',
    label: '披露研究员',
    shortLabel: '披露',
    description: '公告、财报与公司原始披露取证',
  },
  {
    id: 'market-context-researcher',
    label: '市场语境研究员',
    shortLabel: '市场语境',
    description: '新闻、研报与行业语境取证',
  },
  {
    id: 'quality-analyst',
    label: '稳健 / 质量分析师',
    shortLabel: '稳健质量',
    description: '现金流、盈利质量与经营稳健性',
  },
  {
    id: 'growth-analyst',
    label: '成长 / 变化分析师',
    shortLabel: '成长变化',
    description: '增长驱动、业务变化与可持续性',
  },
  {
    id: 'evidence-judge',
    label: '证据裁决员',
    shortLabel: '证据裁决',
    description: '硬审计、分歧裁决与证据缺口确认',
  },
  {
    id: 'report-writer',
    label: '报告撰写员',
    shortLabel: '报告撰写',
    description: '将冻结证据与裁决结果装配成稿',
  },
  {
    id: 'compliance-reviewer',
    label: '合规审校员',
    shortLabel: '合规审校',
    description: '引用、数字、措辞与交付边界审校',
  },
])

export const DEFAULT_TEAM_TASKS = Object.freeze([
  { id: 'research-plan', title: '拆解研究问题与证据需求', assignee: 'research-lead', dependsOn: [] },
  { id: 'disclosure-research', title: '采集公告、财报与公司披露', assignee: 'disclosure-researcher', dependsOn: ['research-plan'] },
  { id: 'market-context-research', title: '采集新闻、研报与行业背景', assignee: 'market-context-researcher', dependsOn: ['research-plan'] },
  { id: 'evidence-freeze', title: '冻结并规范化本次运行证据', assignee: 'system-freeze', dependsOn: ['disclosure-research', 'market-context-research'] },
  { id: 'quality-analysis', title: '质量与护城河分析', assignee: 'quality-analyst', dependsOn: ['evidence-freeze'] },
  { id: 'growth-analysis', title: '增长与变化分析', assignee: 'growth-analyst', dependsOn: ['evidence-freeze'] },
  { id: 'evidence-judgement', title: '证据审计与裁决', assignee: 'evidence-judge', dependsOn: ['quality-analysis', 'growth-analysis'] },
  { id: 'report-draft', title: '撰写证据约束报告', assignee: 'report-writer', dependsOn: ['evidence-judgement'] },
  { id: 'compliance-review', title: '合规与引用复核', assignee: 'compliance-reviewer', dependsOn: ['report-draft'] },
])

const ROLE_ALIASES = Object.freeze({
  planner: 'research-lead',
  research_lead: 'research-lead',
  collector_announcement: 'disclosure-researcher',
  collector_financial: 'disclosure-researcher',
  announcement_collector: 'disclosure-researcher',
  financial_collector: 'disclosure-researcher',
  collector_news: 'market-context-researcher',
  collector_research: 'market-context-researcher',
  collector_industry: 'market-context-researcher',
  news_collector: 'market-context-researcher',
  research_collector: 'market-context-researcher',
  industry_collector: 'market-context-researcher',
  quality: 'quality-analyst',
  quality_agent: 'quality-analyst',
  growth: 'growth-analyst',
  growth_agent: 'growth-analyst',
  evidence_auditor: 'evidence-judge',
  auditor: 'evidence-judge',
  judge: 'evidence-judge',
  analyst: 'report-writer',
  synthesizer: 'report-writer',
  reviewer: 'compliance-reviewer',
  compliance_reviewer: 'compliance-reviewer',
})

const STATUS_ALIASES = Object.freeze({
  active: 'running',
  processing: 'running',
  in_progress: 'running',
  working: 'running',
  done: 'completed',
  complete: 'completed',
  succeeded: 'completed',
  success: 'completed',
  error: 'failed',
  stopped: 'paused',
  awaiting_approval: 'waiting_approval',
  approval_required: 'waiting_approval',
})

const TERMINAL_SUCCESS = new Set(['completed', 'completed_partial'])

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null)
}

function asArray(value) {
  if (Array.isArray(value)) return value
  if (value === undefined || value === null || value === '') return []
  return [value]
}

function finiteNumber(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function objectValues(value) {
  if (Array.isArray(value)) return value
  if (!value || typeof value !== 'object') return []
  return Object.entries(value).map(([id, item]) => (
    item && typeof item === 'object' ? { id, ...item } : { id, status: item }
  ))
}

export function unwrapTeamData(value) {
  let data = value?.data ?? value ?? {}
  if (data?.data && typeof data.data === 'object' && !Array.isArray(data.data)) {
    data = data.data
  }
  if (data?.team && typeof data.team === 'object' && !Array.isArray(data.team)) {
    data = { ...data, ...data.team }
  }
  return data && typeof data === 'object' ? data : {}
}

export function canonicalRoleId(value) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  const slug = raw.toLowerCase().replace(/\s+/g, '-').replace(/_/g, '-')
  return ROLE_ALIASES[raw] || ROLE_ALIASES[raw.toLowerCase()] || ROLE_ALIASES[raw.replace(/-/g, '_')] || slug
}

export function normalizeTeamStatus(value, fallback = 'pending') {
  const raw = String(value || fallback).trim().toLowerCase().replace(/[\s-]+/g, '_')
  return STATUS_ALIASES[raw] || raw || fallback
}

function fallbackRoleStatus(roleId, context) {
  const runStatus = normalizeTeamStatus(context.status || '', 'pending')
  if (TERMINAL_SUCCESS.has(context.status) || runStatus === 'completed') return 'completed'
  if (runStatus === 'failed') return 'failed'

  const stage = String(context.progressDetail?.stage || context.status || 'parsing')
  const debateMode = context.progressDetail?.analysis_mode === 'evidence_debate' || Boolean(context.progressDetail?.debate)
  const ranks = {
    parsing: 0,
    awaiting_confirm: 0,
    collecting: 1,
    ingesting: 2,
    freezing: 2,
    normalizing: 2,
    debating: 3,
    adjudicating: 4,
    analyzing: 5,
    assembling: 5,
    reviewing: 6,
  }
  const roleRanks = {
    'research-lead': 0,
    'disclosure-researcher': 1,
    'market-context-researcher': 1,
    'quality-analyst': 3,
    'growth-analyst': 3,
    'evidence-judge': 4,
    'report-writer': 5,
    'compliance-reviewer': 6,
  }
  if (!debateMode && ['quality-analyst', 'growth-analyst', 'evidence-judge'].includes(roleId) && (ranks[stage] ?? 0) >= 5) {
    return 'skipped'
  }
  const currentRank = ranks[stage] ?? 0
  const roleRank = roleRanks[roleId] ?? 0
  if (roleRank < currentRank) return 'completed'
  if (roleRank === currentRank) return 'running'
  return 'pending'
}

function normalizeMember(raw, role, context) {
  const status = normalizeTeamStatus(
    firstDefined(raw?.status, raw?.state, raw?.phase),
    fallbackRoleStatus(role.id, context),
  )
  return {
    id: role.id,
    label: raw?.label || raw?.display_name || raw?.name || role.label,
    shortLabel: role.shortLabel,
    description: raw?.description || raw?.responsibility || role.description,
    status,
    currentTask: firstDefined(raw?.current_task, raw?.task, raw?.task_title, raw?.active_task, ''),
    progress: finiteNumber(firstDefined(raw?.progress, raw?.progress_percent)),
    heartbeatAt: firstDefined(raw?.heartbeat_at, raw?.last_seen_at, raw?.updated_at, ''),
    degraded: Boolean(raw?.degraded || status === 'degraded'),
    message: firstDefined(raw?.message, raw?.status_message, raw?.error, ''),
  }
}

function normalizeEdge(raw, index) {
  if (Array.isArray(raw)) {
    return { id: `edge-${index}`, from: String(raw[0] || ''), to: String(raw[1] || ''), type: String(raw[2] || 'depends_on') }
  }
  return {
    id: String(raw?.id || `edge-${index}`),
    from: String(firstDefined(raw?.from, raw?.source, raw?.upstream, raw?.parent, '')),
    to: String(firstDefined(raw?.to, raw?.target, raw?.downstream, raw?.child, '')),
    type: String(firstDefined(raw?.type, raw?.kind, 'depends_on')),
  }
}

function fallbackTaskStatus(task, context) {
  const memberStatus = context.members.find((member) => member.id === task.assignee)?.status
  if (task.id === 'evidence-freeze') {
    const stage = String(context.progressDetail?.stage || context.status || '')
    if (['ingesting', 'freezing', 'normalizing'].includes(stage)) return 'running'
    if (['debating', 'adjudicating', 'analyzing', 'reviewing', 'assembling', 'completed', 'completed_partial'].includes(stage)) return 'completed'
    return 'pending'
  }
  return memberStatus || 'pending'
}

function normalizeTasks(tasksValue, edges, context) {
  const rawTasks = objectValues(tasksValue)
  const tasks = rawTasks.length ? rawTasks.map((task, index) => {
    const id = String(firstDefined(task.id, task.team_task_id, task.task_id, task.node_id, task.task_key, `task-${index}`))
    const edgeDependencies = edges.filter((edge) => edge.to === id).map((edge) => edge.from)
    return {
      id,
      title: String(firstDefined(task.title, task.name, task.label, task.description, id)),
      assignee: canonicalRoleId(firstDefined(task.assignee, task.assigned_agent, task.member_id, task.agent_id, task.owner, task.role_id, '')),
      status: normalizeTeamStatus(firstDefined(task.status, task.state, task.phase), 'pending'),
      dependsOn: asArray(firstDefined(task.depends_on, task.dependencies, task.needs, edgeDependencies)).map(String),
      message: String(firstDefined(task.message, task.summary, task.result_summary, task.error, '')),
      progress: finiteNumber(firstDefined(task.progress, task.progress_percent)),
      approvalRequired: Boolean(firstDefined(task.approval_required, task.requires_approval, false)),
      attemptCount: finiteNumber(firstDefined(task.attempt_count, task.attempts, task.attemptCount)),
      budgetCny: finiteNumber(firstDefined(task.budget_cny, task.budget?.limit_cny, task.budgetCny)),
    }
  }) : DEFAULT_TEAM_TASKS.map((task) => ({
    ...task,
    status: fallbackTaskStatus(task, context),
    message: '',
    progress: null,
    approvalRequired: false,
    attemptCount: null,
    budgetCny: null,
  }))
  return tasks
}

export function normalizeTeamEvent(raw, index = 0) {
  const details = raw?.details && typeof raw.details === 'object' ? raw.details : {}
  const payload = raw?.payload && typeof raw.payload === 'object' ? raw.payload : {}
  const kind = String(firstDefined(raw?.type, raw?.kind, raw?.event, raw?.event_type, raw?.action, 'update'))
  const actor = canonicalRoleId(firstDefined(raw?.actor, raw?.from, raw?.source, raw?.agent_id, details.actor, details.from, payload.actor, payload.from_agent, ''))
  const target = canonicalRoleId(firstDefined(raw?.target, raw?.to, raw?.assignee, details.target, details.to, payload.target, payload.to_agent, ''))
  const cursor = firstDefined(raw?.cursor, raw?.sequence, raw?.seq, raw?.offset, index)
  return {
    id: String(firstDefined(raw?.id, raw?.event_id, `${cursor}-${kind}-${index}`)),
    cursor,
    kind,
    actor,
    target,
    title: String(firstDefined(raw?.title, raw?.label, '')),
    message: String(firstDefined(raw?.message, raw?.summary, raw?.description, details.message, details.summary, payload.message, payload.summary, payload.reason, payload.payload?.message, payload.payload?.summary, '')),
    timestamp: String(firstDefined(raw?.timestamp, raw?.created_at, raw?.at, raw?.time, '')),
    status: normalizeTeamStatus(firstDefined(raw?.status, details.status, payload.to_status, payload.status), ''),
    taskId: String(firstDefined(raw?.task_id, raw?.team_task_id, raw?.task, details.task_id, payload.target_task_id, '')),
    replay: Boolean(firstDefined(raw?.replay, raw?.is_replay, false)),
  }
}

export function eventsFromLegacyLogs(lines = []) {
  return asArray(lines).slice(-80).map((line, index) => normalizeTeamEvent({
    id: `legacy-${index}-${line?.elapsed_seconds ?? ''}`,
    cursor: index,
    type: line?.action || 'agent_log',
    actor: line?.agent,
    target: firstDefined(line?.details?.target, line?.details?.to, ''),
    message: firstDefined(line?.details?.message, line?.details?.issue, line?.details?.error, line?.action, ''),
    timestamp: firstDefined(line?.timestamp, line?.created_at, ''),
    replay: true,
  }, index))
}

export function mergeTeamEvents(current = [], incoming = []) {
  const merged = []
  const seen = new Set()
  for (const item of [...current, ...incoming]) {
    const event = normalizeTeamEvent(item, merged.length)
    const key = event.id || `${event.cursor}|${event.kind}|${event.timestamp}|${event.message}`
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(event)
  }
  return merged.slice(-160)
}

function normalizeMetrics(data, members) {
  const metrics = data.metrics && typeof data.metrics === 'object' ? data.metrics : {}
  const budget = firstDefined(metrics.budget, data.budget, {}) || {}
  const spentCny = finiteNumber(firstDefined(
    budget.spent_cny,
    budget.committed_cny,
    metrics.budget_spent_cny,
    metrics.llm_cost_cny,
    data.budget_spent_cny,
  ))
  const limitCny = finiteNumber(firstDefined(
    budget.limit_cny,
    budget.budget_cny,
    metrics.budget_limit_cny,
    data.budget_limit_cny,
  ))
  const remainingCny = finiteNumber(firstDefined(
    budget.remaining_cny,
    metrics.budget_remaining_cny,
    limitCny != null && spentCny != null ? Math.max(0, limitCny - spentCny) : null,
  ))
  const ratioValue = firstDefined(budget.ratio, metrics.budget_ratio)
  let budgetPercent = finiteNumber(firstDefined(budget.percent, budget.usage_percent, metrics.budget_percent, ratioValue))
  if (budgetPercent == null && spentCny != null && limitCny) budgetPercent = (spentCny / limitCny) * 100
  if (budgetPercent != null && budgetPercent <= 1 && ratioValue != null && Number(ratioValue) === budgetPercent) {
    budgetPercent *= 100
  }

  const degradation = firstDefined(metrics.degradation, data.degradation, {}) || {}
  const degradationReasons = asArray(firstDefined(
    degradation.reasons,
    metrics.degradation_reasons,
    metrics.degraded_reasons,
    data.degradation_reasons,
    data.degraded_reason,
  )).filter(Boolean).map(String)
  const degraded = Boolean(
    firstDefined(degradation.active, degradation.degraded, metrics.degraded, data.degraded, false) ||
    degradationReasons.length ||
    members.some((member) => member.degraded)
  )
  return {
    spentCny,
    limitCny,
    remainingCny,
    budgetPercent,
    budgetScope: String(firstDefined(budget.scope, metrics.budget_scope, '')),
    includesWorkerUsage: Boolean(firstDefined(
      budget.includes_agentteams_worker_usage,
      metrics.includes_agentteams_worker_usage,
      false,
    )),
    degraded,
    degradationReasons,
    completedTasks: finiteNumber(firstDefined(metrics.completed_tasks, data.completed_tasks)),
    totalTasks: finiteNumber(firstDefined(metrics.total_tasks, data.total_tasks)),
    successRate: finiteNumber(firstDefined(metrics.success_rate, data.success_rate)),
    durationSeconds: finiteNumber(firstDefined(metrics.duration_seconds, data.duration_seconds)),
    llmTokens: finiteNumber(firstDefined(metrics.llm_tokens, data.llm_tokens)),
    llmCalls: finiteNumber(firstDefined(metrics.llm_calls, data.llm_calls)),
    toolFailureRate: finiteNumber(firstDefined(metrics.tool_failure_rate, data.tool_failure_rate)),
    auditRejectionRate: finiteNumber(firstDefined(metrics.audit_rejection_rate, data.audit_rejection_rate)),
    retryCount: finiteNumber(firstDefined(metrics.retry_count, data.retry_count)),
    approvalDurationSeconds: finiteNumber(firstDefined(metrics.approval_duration_seconds, data.approval_duration_seconds)),
    stageDurationsSeconds: firstDefined(metrics.stage_durations_seconds, data.stage_durations_seconds, {}),
  }
}

function normalizeApproval(data, stateVersion) {
  const approvals = objectValues(data.approvals)
  const awaitingArtifact = objectValues(data.artifacts).find((artifact) => (
    artifact?.requires_approval && normalizeTeamStatus(artifact?.status, '') === 'waiting_approval'
  ))
  const value = firstDefined(
    data.approval,
    data.pending_approval,
    data.approval_gate,
    awaitingArtifact ? {
      required: true,
      status: 'waiting_approval',
      title: awaitingArtifact.title || `审批 ${awaitingArtifact.artifact_type || '报告产物'}`,
      summary: awaitingArtifact.summary || awaitingArtifact.metadata?.summary || '',
      artifact_id: awaitingArtifact.artifact_id,
    } : null,
    approvals.find((approval) => normalizeTeamStatus(approval?.status || approval?.decision, '') === 'waiting_approval'),
    null,
  )
  if (!value || typeof value !== 'object') return null
  const status = normalizeTeamStatus(firstDefined(value.status, value.state), value.required ? 'waiting_approval' : 'pending')
  const required = Boolean(firstDefined(
    value.required,
    value.pending,
    ['waiting_approval', 'pending', 'requested', 'required'].includes(status),
  ))
  return {
    required,
    status,
    title: String(firstDefined(value.title, value.label, value.action, '高风险动作待审批')),
    summary: String(firstDefined(value.summary, value.message, value.description, '')),
    risk: String(firstDefined(value.risk, value.risk_level, value.severity, '')),
    requestedBy: canonicalRoleId(firstDefined(value.requested_by, value.actor, value.agent_id, '')),
    expectedVersion: finiteNumber(firstDefined(value.expected_version, value.state_version, stateVersion)) ?? 0,
    artifactId: String(firstDefined(value.artifact_id, value.id, '')),
  }
}

function normalizeRollback(data) {
  const value = data.rollback && typeof data.rollback === 'object' ? data.rollback : {}
  const targets = objectValues(firstDefined(value.targets, data.rollback_targets, [])).map((item) => ({
    runId: String(firstDefined(item.run_id, item.target_run_id, item.id, '')),
    label: String(firstDefined(item.label, item.title, item.run_id, item.id, '')),
  })).filter((item) => item.runId)
  const targetRunId = String(firstDefined(
    value.target_run_id,
    data.rollback_target_run_id,
    data.previous_run_id,
    targets[0]?.runId,
    '',
  ))
  return {
    allowed: firstDefined(value.allowed, data.rollback_allowed, true) !== false,
    targetRunId,
    targets,
  }
}

export function safeElementUrl(value) {
  const url = String(value || '').trim()
  if (!url) return ''
  return /^(https?:|matrix:|element:)/i.test(url) ? url : ''
}

/**
 * @param {unknown} value API response or team payload
 * @param {{status?: string, progressDetail?: object, logLines?: Array, fallback?: boolean}} context
 */
export function normalizeTeamSnapshot(value, context = {}) {
  const data = unwrapTeamData(value)
  const rawTasks = objectValues(firstDefined(data.tasks, data.dag?.tasks, data.dag?.nodes, data.nodes, []))
  const rawMembers = [
    ...objectValues(firstDefined(data.members, data.agents, data.workers, data.roles, [])),
    ...asArray(data.agent_roles).map((role) => (
      typeof role === 'string' ? { id: role } : role
    )),
  ]
  const memberMap = new Map()
  for (const member of rawMembers) {
    const id = canonicalRoleId(firstDefined(member.id, member.member_id, member.agent_id, member.role_id, member.role, member.name))
    if (id) memberMap.set(id, member)
  }
  const memberStatusPriority = ['failed', 'blocked', 'waiting_approval', 'running', 'ready', 'pending', 'completed', 'skipped']
  for (const role of AGENT_TEAM_ROLES) {
    const current = memberMap.get(role.id) || { id: role.id }
    if (firstDefined(current.status, current.state, current.phase) != null) continue
    const roleTasks = rawTasks.filter((task) => canonicalRoleId(firstDefined(
      task.assignee,
      task.assigned_agent,
      task.member_id,
      task.agent_id,
      task.owner,
      task.role_id,
    )) === role.id)
    const activeTask = roleTasks.find((task) => ['running', 'waiting_approval', 'failed', 'blocked'].includes(normalizeTeamStatus(task.status || task.state, '')))
    const statuses = roleTasks.map((task) => normalizeTeamStatus(task.status || task.state, 'pending'))
    const inferred = memberStatusPriority.find((status) => statuses.includes(status))
    memberMap.set(role.id, {
      ...current,
      ...(inferred ? { status: inferred === 'ready' ? 'pending' : inferred } : {}),
      ...(activeTask ? { current_task: activeTask.title || activeTask.task_key } : {}),
    })
  }
  const members = AGENT_TEAM_ROLES.map((role) => normalizeMember(memberMap.get(role.id), role, context))
  const edges = objectValues(firstDefined(data.edges, data.dag?.edges, data.graph?.edges, [])).map(normalizeEdge).filter((edge) => edge.from && edge.to)
  const tasks = normalizeTasks(rawTasks, edges, {
    ...context,
    members,
  })
  const handoffEvents = objectValues(data.handoffs).map((handoff, index) => normalizeTeamEvent({
    id: handoff.handoff_id || `handoff-${index}`,
    event_type: `handoff_${handoff.status || 'created'}`,
    actor: handoff.from_agent,
    target: handoff.to_agent,
    team_task_id: handoff.target_task_id,
    payload: handoff.payload,
    created_at: handoff.created_at,
    status: handoff.status,
  }, index))
  const payloadEvents = mergeTeamEvents(
    objectValues(firstDefined(data.events, data.timeline, [])).map(normalizeTeamEvent),
    handoffEvents,
  )
  const stateVersion = finiteNumber(firstDefined(data.state_version, data.version, data.approval?.state_version)) ?? 0
  const sourceValue = String(firstDefined(data.source, data.mode, data.runtime_mode, '')).toLowerCase()
  const replay = Boolean(data.replay || data.is_replay || ['replay', 'archive', 'archived'].includes(sourceValue))
  const source = context.fallback ? 'fallback' : replay ? 'replay' : 'live'
  return {
    source,
    replay,
    stateVersion,
    managerStatus: normalizeTeamStatus(firstDefined(data.manager?.status, data.manager_status, data.status), 'running'),
    members,
    tasks,
    edges: edges.length ? edges : tasks.flatMap((task) => task.dependsOn.map((from) => ({
      id: `${from}-${task.id}`,
      from,
      to: task.id,
      type: 'depends_on',
    }))),
    events: mergeTeamEvents(
      payloadEvents,
      context.fallback ? eventsFromLegacyLogs(context.logLines) : [],
    ),
    metrics: normalizeMetrics(data, members),
    approval: normalizeApproval(data, stateVersion),
    rollback: normalizeRollback(data),
    elementUrl: safeElementUrl(firstDefined(data.element_url, data.element?.url, data.links?.element, data.matrix_room_url, data.config?.element_url, '')),
    roomName: String(firstDefined(data.room_name, data.element?.room_name, data.matrix_room_name, '')),
    nextCursor: firstDefined(data.next_cursor, data.events_cursor, data.event_cursor, data.cursor, ''),
  }
}

export function extractTeamEvents(value) {
  const data = unwrapTeamData(value)
  const events = objectValues(firstDefined(data.events, data.items, data.timeline, Array.isArray(data) ? data : []))
  return {
    events: events.map(normalizeTeamEvent),
    nextCursor: firstDefined(data.next_cursor, data.cursor, data.from_cursor, ''),
  }
}
