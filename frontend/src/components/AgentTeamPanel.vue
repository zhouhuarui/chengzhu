<template>
  <section class="agent-team-panel" aria-label="AgentTeams 团队视图">
    <header class="team-header">
      <div>
        <div class="title-row">
          <h2>AgentTeams 协同台</h2>
          <span class="source-badge" :class="`source-${snapshot.source}`">{{ sourceLabel }}</span>
        </div>
        <p>Manager 作为外部控制面，编排 8 个可审计 Worker 的任务与交接。</p>
      </div>
      <div class="team-header-actions">
        <a
          v-if="snapshot.elementUrl"
          class="element-link"
          :href="snapshot.elementUrl"
          target="_blank"
          rel="noopener noreferrer"
        >
          打开 Element 协作室 ↗
        </a>
        <span v-else class="element-link is-disabled" title="后端尚未返回 Element 房间链接">
          Element 未关联
        </span>
        <button
          type="button"
          class="icon-button"
          :disabled="loading || mutating"
          aria-label="刷新团队状态"
          @click="refreshTeam({ showLoading: true, force: true })"
        >
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <div class="metric-strip" aria-label="运行指标">
      <span class="metric-badge manager-badge">
        Manager 控制面 · {{ statusLabel(snapshot.managerStatus) }}
      </span>
      <span class="metric-badge" :class="snapshot.metrics.degraded ? 'is-degraded' : 'is-healthy'">
        {{ degradationLabel }}
      </span>
      <span
        class="metric-badge"
        :class="budgetTone"
        :title="snapshot.metrics.includesWorkerUsage ? '包含 AgentTeams Worker 调用' : '当前只统计 Chengzhu 后端与 MCP 可观测调用；Worker 累计费用需 run-aware 网关'"
      >
        {{ budgetLabel }}
      </span>
      <span class="metric-badge">
        任务 {{ completedTaskCount }}/{{ snapshot.tasks.length }}
      </span>
      <span v-if="snapshot.metrics.successRate != null" class="metric-badge">
        成功率 {{ percentLabel(snapshot.metrics.successRate) }}
      </span>
      <span v-if="snapshot.metrics.durationSeconds != null" class="metric-badge">
        耗时 {{ durationLabel(snapshot.metrics.durationSeconds) }}
      </span>
      <span v-if="snapshot.metrics.llmTokens != null" class="metric-badge">
        Token {{ integerLabel(snapshot.metrics.llmTokens) }}
      </span>
      <span v-if="snapshot.metrics.toolFailureRate != null" class="metric-badge">
        工具失败 {{ percentLabel(snapshot.metrics.toolFailureRate) }}
      </span>
      <span v-if="snapshot.metrics.auditRejectionRate != null" class="metric-badge">
        审计驳回 {{ percentLabel(snapshot.metrics.auditRejectionRate) }}
      </span>
      <span v-if="snapshot.metrics.retryCount != null" class="metric-badge">
        重试 {{ integerLabel(snapshot.metrics.retryCount) }}
      </span>
      <span v-if="snapshot.metrics.approvalDurationSeconds != null" class="metric-badge">
        审批 {{ durationLabel(snapshot.metrics.approvalDurationSeconds) }}
      </span>
    </div>

    <div v-if="snapshot.source === 'fallback'" class="compat-notice" role="status">
      该历史运行没有 AgentTeams 快照，已用持久化任务状态与 Agent 日志生成兼容只读视图。
    </div>
    <div v-else-if="snapshot.source === 'replay'" class="compat-notice" role="status">
      当前展示持久化回放数据；审批与回滚操作仅在实时运行中开放。
    </div>
    <div v-if="loadError" class="inline-error" role="alert">{{ loadError }}</div>
    <div v-if="actionMessage" class="action-message" :class="{ error: actionError }" role="status">
      {{ actionMessage }}
    </div>

    <section class="team-section roles-section">
      <div class="section-heading">
        <h3>八角色状态</h3>
        <span>{{ runningMemberCount }} 执行中 · {{ completedMemberCount }} 已完成</span>
      </div>
      <div class="role-grid">
        <article
          v-for="member in snapshot.members"
          :key="member.id"
          class="role-card"
          :class="`status-${member.status}`"
          :data-role-id="member.id"
        >
          <div class="role-card-top">
            <span class="status-dot" aria-hidden="true" />
            <strong>{{ member.label }}</strong>
            <span class="role-status">{{ statusLabel(member.status) }}</span>
          </div>
          <p>{{ member.currentTask || member.description }}</p>
          <div v-if="member.progress != null" class="member-progress" :aria-label="`进度 ${member.progress}%`">
            <span :style="{ width: `${clampPercent(member.progress)}%` }" />
          </div>
          <small v-if="member.message">{{ member.message }}</small>
        </article>
      </div>
    </section>

    <div class="detail-grid">
      <section class="team-section task-section">
        <div class="section-heading">
          <h3>任务 DAG</h3>
          <span>依赖→交接</span>
        </div>
        <ol class="task-list" aria-label="AgentTeams 任务 DAG 列表">
          <li
            v-for="task in snapshot.tasks"
            :key="task.id"
            class="task-row"
            :class="`status-${task.status}`"
          >
            <span class="task-state" aria-hidden="true">{{ taskIcon(task.status) }}</span>
            <div class="task-body">
              <div class="task-title-row">
                <strong>{{ task.title }}</strong>
                <span v-if="task.approvalRequired" class="approval-mini">待审批</span>
              </div>
              <div class="task-meta">
                <span>{{ roleLabel(task.assignee) }}</span>
                <span v-if="task.dependsOn.length">依赖 {{ dependencyLabels(task.dependsOn) }}</span>
                <span v-else>起点</span>
                <span v-if="task.attemptCount != null" class="task-runtime-meta">
                  尝试 {{ integerLabel(task.attemptCount) }} 次
                </span>
                <span v-if="task.budgetCny != null" class="task-runtime-meta">
                  预算 {{ moneyLabel(task.budgetCny) }}
                </span>
              </div>
              <p v-if="task.message">{{ task.message }}</p>
              <div v-if="task.progress != null" class="task-progress">
                <span :style="{ width: `${clampPercent(task.progress)}%` }" />
              </div>
            </div>
          </li>
        </ol>
      </section>

      <section class="team-section event-section">
        <div class="section-heading">
          <h3>事件 / 交接时间线</h3>
          <div class="event-heading-actions">
            <span>{{ eventCountLabel }}</span>
            <select
              v-model="eventFilter"
              class="event-filter"
              aria-label="筛选协作事件"
              data-testid="event-filter"
            >
              <option value="all">全部事件</option>
              <option value="task">任务</option>
              <option value="handoff">交接</option>
              <option value="skill-mcp">Skill / MCP</option>
              <option value="approval">审批</option>
            </select>
          </div>
        </div>
        <ol v-if="visibleEvents.length" class="event-list" aria-live="polite">
          <li
            v-for="event in visibleEvents"
            :key="event.id"
            class="event-row"
            :data-event-category="eventCategory(event)"
          >
            <span class="event-marker" :class="`event-${eventTone(event)}`" aria-hidden="true" />
            <div>
              <div class="event-topline">
                <strong>{{ eventTitle(event) }}</strong>
                <time>{{ formatEventTime(event) }}</time>
              </div>
              <p v-if="event.actor || event.target" class="handoff-route">
                {{ roleLabel(event.actor) }}
                <template v-if="event.target"> → {{ roleLabel(event.target) }}</template>
              </p>
              <p v-if="event.message" class="event-message">{{ event.message }}</p>
            </div>
          </li>
        </ol>
        <div v-else class="empty-state">
          {{ events.length ? '当前筛选没有匹配事件。' : '等待第一条协作事件…' }}
        </div>
      </section>
    </div>

    <section v-if="snapshot.approval?.required" class="approval-panel" aria-label="人工审批">
      <div class="approval-copy">
        <span class="approval-kicker">人工检查点</span>
        <h3>{{ snapshot.approval.title }}</h3>
        <p>{{ snapshot.approval.summary || '高风险动作需要人工确认后才会继续。' }}</p>
        <small v-if="snapshot.approval.requestedBy">申请者：{{ roleLabel(snapshot.approval.requestedBy) }}</small>
      </div>
      <div class="approval-controls">
        <label>
          审批备注（驳回时建议填写）
          <textarea
            v-model="approvalReason"
            rows="2"
            :disabled="!canMutate || mutating"
            placeholder="记录决策依据，供审计回放"
            data-testid="approval-reason"
          />
        </label>
        <div class="approval-actions">
          <button
            type="button"
            class="action-button approve"
            :disabled="!canMutate || mutating"
            data-testid="approve-button"
            @click="submitApproval('approve')"
          >
            批准并发布
          </button>
          <button
            type="button"
            class="action-button reject"
            :disabled="!canMutate || mutating"
            data-testid="reject-button"
            @click="submitApproval('reject')"
          >
            驳回
          </button>
        </div>
      </div>
    </section>

    <section class="rollback-entry" aria-label="运行回滚">
      <button
        type="button"
        class="rollback-toggle"
        :disabled="!canRollback || mutating"
        data-testid="rollback-toggle"
        @click="rollbackOpen = !rollbackOpen"
      >
        {{ rollbackOpen ? '取消回滚' : '回滚到历史运行…' }}
      </button>
      <span v-if="!canMutate" class="readonly-hint">回放模式为只读</span>
      <form v-if="rollbackOpen" class="rollback-form" @submit.prevent="submitRollback">
        <label>
          目标 Run
          <select
            v-if="snapshot.rollback.targets.length"
            v-model="rollbackTarget"
            :disabled="mutating"
            data-testid="rollback-target"
          >
            <option v-for="target in snapshot.rollback.targets" :key="target.runId" :value="target.runId">
              {{ target.label || target.runId }}
            </option>
          </select>
          <input
            v-else
            v-model.trim="rollbackTarget"
            :disabled="mutating"
            placeholder="run_..."
            required
            data-testid="rollback-target"
          />
        </label>
        <label class="rollback-reason-label">
          回滚原因
          <input
            v-model.trim="rollbackReason"
            :disabled="mutating"
            placeholder="必填，将写入审计时间线"
            required
            data-testid="rollback-reason"
          />
        </label>
        <button
          type="submit"
          class="action-button reject"
          :disabled="!rollbackTarget || !rollbackReason || mutating"
          data-testid="rollback-submit"
        >
          {{ mutating ? '提交中…' : '确认回滚' }}
        </button>
      </form>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { taskApi } from '../api/index.js'
import {
  AGENT_TEAM_ROLES,
  extractTeamEvents,
  mergeTeamEvents,
  normalizeTeamSnapshot,
} from '../types/agentTeam.js'

const props = defineProps({
  taskId: { type: String, required: true },
  runId: { type: String, default: '' },
  status: { type: String, default: '' },
  progressDetail: { type: Object, default: () => ({}) },
  logLines: { type: Array, default: () => [] },
  pollInterval: { type: Number, default: 3000 },
})

const loading = ref(false)
const mutating = ref(false)
const loadError = ref('')
const actionMessage = ref('')
const actionError = ref(false)
const cursor = ref('')
const events = ref([])
const eventFilter = ref('all')
const approvalReason = ref('')
const rollbackOpen = ref(false)
const rollbackTarget = ref('')
const rollbackReason = ref('')
const teamEndpointMissing = ref(false)

function fallbackContext() {
  return {
    status: props.status,
    progressDetail: props.progressDetail,
    logLines: props.logLines,
    fallback: true,
  }
}

const snapshot = ref(normalizeTeamSnapshot(null, fallbackContext()))
let pollTimer = null
let requestInFlight = false
let destroyed = false

const sourceLabel = computed(() => ({
  live: '实时',
  replay: '持久化回放',
  fallback: '兼容回放',
}[snapshot.value.source] || '未知'))

const completedTaskCount = computed(() => snapshot.value.tasks.filter((task) => ['completed', 'skipped'].includes(task.status)).length)
const runningMemberCount = computed(() => snapshot.value.members.filter((member) => member.status === 'running').length)
const completedMemberCount = computed(() => snapshot.value.members.filter((member) => ['completed', 'skipped'].includes(member.status)).length)
const filteredEvents = computed(() => {
  if (eventFilter.value === 'all') return events.value
  return events.value.filter((event) => eventCategory(event) === eventFilter.value)
})
const visibleEvents = computed(() => filteredEvents.value.slice().reverse().slice(0, 80))
const eventCountLabel = computed(() => (
  eventFilter.value === 'all'
    ? `${events.value.length} 条`
    : `${filteredEvents.value.length} / ${events.value.length} 条`
))
const canMutate = computed(() => snapshot.value.source === 'live' && Boolean(props.runId))
const canRollback = computed(() => canMutate.value && snapshot.value.rollback.allowed)

const degradationLabel = computed(() => {
  if (!snapshot.value.metrics.degraded) return '无降级'
  const reasons = snapshot.value.metrics.degradationReasons
  return reasons.length ? `已降级 · ${reasons[0]}` : '已降级'
})

const budgetLabel = computed(() => {
  const metrics = snapshot.value.metrics
  const prefix = metrics.includesWorkerUsage ? '全链路预算' : '后端账本'
  if (metrics.spentCny == null && metrics.limitCny == null) return `${prefix}待上报`
  if (metrics.spentCny != null && metrics.limitCny != null) {
    return `${prefix} ¥${metrics.spentCny.toFixed(2)} / ¥${metrics.limitCny.toFixed(2)}`
  }
  if (metrics.remainingCny != null) return `${prefix}余额 ¥${metrics.remainingCny.toFixed(2)}`
  return `${prefix}已用 ¥${metrics.spentCny.toFixed(2)}`
})

const budgetTone = computed(() => {
  const percent = snapshot.value.metrics.budgetPercent
  if (percent == null) return ''
  if (percent >= 90) return 'budget-danger'
  if (percent >= 70) return 'budget-warning'
  return 'budget-ok'
})

function isNotFound(error) {
  return error?.response?.status === 404 || error?.status === 404
}

function isTerminal(value) {
  return ['completed', 'completed_partial', 'failed'].includes(value)
}

function applyFallback(error = '') {
  snapshot.value = normalizeTeamSnapshot(null, fallbackContext())
  events.value = snapshot.value.events
  cursor.value = ''
  loadError.value = error
  rollbackTarget.value = ''
}

async function refreshEvents() {
  try {
    const response = await taskApi.teamEvents(props.taskId, props.runId, cursor.value)
    if (destroyed) return
    const batch = extractTeamEvents(response)
    events.value = mergeTeamEvents(events.value, batch.events)
    if (batch.nextCursor !== '' && batch.nextCursor != null) cursor.value = batch.nextCursor
  } catch (error) {
    // Event history is optional for replay/legacy responses; the snapshot may
    // already contain a complete timeline.
    if (!isNotFound(error)) {
      loadError.value = '事件增量更新失败，已保留上次时间线。'
    }
  }
}

async function refreshTeam({ showLoading = false, force = false } = {}) {
  if (teamEndpointMissing.value && !force) {
    applyFallback(loadError.value)
    return
  }
  if (requestInFlight && !force) return
  if (force) teamEndpointMissing.value = false
  requestInFlight = true
  if (showLoading) loading.value = true
  loadError.value = ''
  try {
    const response = await taskApi.team(props.taskId, props.runId)
    if (destroyed) return
    teamEndpointMissing.value = false
    const next = normalizeTeamSnapshot(response, {
      status: props.status,
      progressDetail: props.progressDetail,
      logLines: props.logLines,
    })
    snapshot.value = next
    events.value = mergeTeamEvents(next.events, events.value)
    if (!rollbackTarget.value) rollbackTarget.value = next.rollback.targetRunId
    if (next.source === 'live') await refreshEvents()
  } catch (error) {
    if (destroyed) return
    if (isNotFound(error)) {
      teamEndpointMissing.value = true
      applyFallback()
    } else {
      applyFallback('协作快照暂时不可用，已切换到本地日志回放。')
    }
  } finally {
    requestInFlight = false
    loading.value = false
  }
}

async function submitApproval(decision) {
  if (!canMutate.value || !snapshot.value.approval) return
  mutating.value = true
  actionMessage.value = ''
  actionError.value = false
  const payload = {
    decision,
    expected_version: snapshot.value.approval.expectedVersion ?? snapshot.value.stateVersion,
  }
  if (approvalReason.value.trim()) payload.reason = approvalReason.value.trim()
  try {
    await taskApi.approval(props.taskId, props.runId, payload)
    actionMessage.value = decision === 'approve' ? '已批准并发布，决定已写入审计时间线。' : '已驳回，意见已写入审计时间线。'
    approvalReason.value = ''
    await refreshTeam({ force: true })
  } catch (error) {
    actionError.value = true
    actionMessage.value = error?.response?.status === 409
      ? '团队状态已更新，请核对最新审批内容后重试。'
      : (error?.message || '审批提交失败')
    if (error?.response?.status === 409) await refreshTeam({ force: true })
  } finally {
    mutating.value = false
  }
}

async function submitRollback() {
  if (!canRollback.value || !rollbackTarget.value || !rollbackReason.value) return
  mutating.value = true
  actionMessage.value = ''
  actionError.value = false
  try {
    await taskApi.rollback(props.taskId, props.runId, {
      target_run_id: rollbackTarget.value,
      reason: rollbackReason.value,
      expected_version: snapshot.value.stateVersion,
    })
    actionMessage.value = `已提交回滚：${rollbackTarget.value}`
    rollbackOpen.value = false
    rollbackReason.value = ''
    await refreshTeam({ force: true })
  } catch (error) {
    actionError.value = true
    actionMessage.value = error?.response?.status === 409
      ? '团队状态已更新，请重新选择回滚目标。'
      : (error?.message || '回滚提交失败')
    if (error?.response?.status === 409) await refreshTeam({ force: true })
  } finally {
    mutating.value = false
  }
}

function startPolling() {
  if (pollTimer || isTerminal(props.status)) return
  pollTimer = window.setInterval(() => refreshTeam(), props.pollInterval)
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = null
}

function statusLabel(status) {
  return ({
    pending: '待命',
    ready: '已就绪',
    queued: '排队中',
    running: '执行中',
    waiting: '等待中',
    waiting_approval: '待审批',
    blocked: '已阻塞',
    completed: '已完成',
    skipped: '已跳过',
    degraded: '降级执行',
    failed: '失败',
    paused: '已暂停',
    offline: '离线',
    approved: '已批准',
    published: '已发布',
    changes_requested: '待修订',
    rejected_terminal: '已终止',
  }[status] || status || '未知')
}

function roleLabel(id) {
  if (!id) return '系统'
  if (['system-freeze', 'chengzhu-backend'].includes(id)) return '系统冻结服务'
  if (['vue-user', 'vue'].includes(id)) return 'Vue 人工审批'
  if (['manager', 'agentteams-manager'].includes(id)) return 'Manager 控制面'
  return AGENT_TEAM_ROLES.find((role) => role.id === id)?.shortLabel || id
}

function taskIcon(status) {
  if (status === 'completed') return '✓'
  if (status === 'skipped') return '—'
  if (status === 'running') return '●'
  if (['failed', 'blocked'].includes(status)) return '!'
  if (status === 'waiting_approval') return '◆'
  return '○'
}

function dependencyLabels(ids) {
  return ids.map((id) => snapshot.value.tasks.find((task) => task.id === id)?.title || id).join(' + ')
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value) || 0))
}

function percentLabel(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${(number * 100).toFixed(number > 0 && number < 0.01 ? 1 : 0)}%`
}

function integerLabel(value) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, Math.round(number)).toLocaleString('zh-CN') : '—'
}

function moneyLabel(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `¥${Math.max(0, number).toFixed(2)}` : '—'
}

function durationLabel(value) {
  const seconds = Math.max(0, Number(value) || 0)
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}分${rest}秒`
}

function eventTitle(event) {
  if (event.title) return event.title
  const labels = {
    handoff: '任务交接',
    task_handoff: '任务交接',
    handoff_created: '创建任务交接',
    handoff_pending: '任务待接收',
    handoff_accepted: '任务已接收',
    handoff_completed: '任务交接完成',
    team_created: '创建协作团队',
    team_status_changed: '团队状态更新',
    team_task_status_changed: '任务状态更新',
    task_started: '开始任务',
    task_completed: '完成任务',
    tool_call: '调用工具',
    tool_result: '工具返回',
    approval_requested: '请求人工审批',
    approved: '人工已批准',
    approval_approved: '人工已批准',
    rejected: '人工已驳回',
    approval_rejected: '人工已驳回',
    artifact_registered: '产物已登记，等待审批',
    artifact_published: '审批产物已发布',
    rollback: '运行回滚',
    degraded: '降级执行',
    error: '执行异常',
  }
  return labels[event.kind] || event.kind.replace(/_/g, ' ')
}

function eventTone(event) {
  if (['error', 'failed', 'rejected'].some((token) => event.kind.includes(token))) return 'danger'
  if (['completed', 'approved', 'success'].some((token) => event.kind.includes(token))) return 'success'
  if (['approval', 'rollback', 'blocked'].some((token) => event.kind.includes(token))) return 'warning'
  if (event.kind.includes('handoff')) return 'handoff'
  return 'normal'
}

function eventCategory(event) {
  const kind = String(event?.kind || '').trim().toLowerCase().replace(/[.\s-]+/g, '_')
  if (kind.includes('handoff')) return 'handoff'
  if (['skill', 'mcp', 'tool'].some((token) => kind.includes(token))) return 'skill-mcp'
  if (['approval', 'approve', 'reject', 'rollback', 'publish', 'human'].some((token) => kind.includes(token))) {
    return 'approval'
  }
  if (kind.includes('task')) return 'task'
  return 'other'
}

function formatEventTime(event) {
  if (!event.timestamp) return event.cursor !== '' && event.cursor != null ? `#${event.cursor}` : ''
  const numeric = Number(event.timestamp)
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
    : new Date(event.timestamp)
  if (Number.isNaN(date.getTime())) return event.timestamp
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

watch(
  () => [props.taskId, props.runId],
  async () => {
    cursor.value = ''
    events.value = []
    rollbackTarget.value = ''
    teamEndpointMissing.value = false
    await refreshTeam({ showLoading: true, force: true })
  },
)

watch(
  () => [props.status, props.progressDetail, props.logLines.length],
  () => {
    if (snapshot.value.source === 'fallback') applyFallback(loadError.value)
    if (isTerminal(props.status)) stopPolling()
    else startPolling()
  },
  { deep: true },
)

onMounted(async () => {
  await refreshTeam({ showLoading: true })
  startPolling()
})

onUnmounted(() => {
  destroyed = true
  stopPolling()
})
</script>

<style scoped>
.agent-team-panel {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding: 2px 4px 20px;
  color: #1a3a6b;
}

.team-header,
.title-row,
.team-header-actions,
.section-heading,
.role-card-top,
.task-title-row,
.event-topline,
.approval-actions,
.rollback-entry,
.rollback-form {
  display: flex;
  align-items: center;
}

.team-header {
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  background: linear-gradient(100deg, #edf3fb, #f8fafc);
  border: 1px solid #d9e3f0;
}

.title-row {
  gap: 9px;
}

.team-header h2 {
  margin: 0;
  font-size: 18px;
}

.team-header p {
  margin: 4px 0 0;
  color: #5e7391;
  font-size: 12px;
  line-height: 1.5;
}

.team-header-actions {
  flex-shrink: 0;
  gap: 8px;
}

.source-badge,
.metric-badge,
.approval-mini,
.approval-kicker {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  white-space: nowrap;
}

.source-badge {
  padding: 2px 8px;
  font-size: 11px;
  border: 1px solid #aebed2;
  color: #536b8b;
  background: #fff;
}

.source-live { border-color: #7eaf93; color: #24623e; background: #eef8f1; }
.source-replay,
.source-fallback { border-color: #c9ac66; color: #7b5b12; background: #fff9e9; }

.element-link,
.icon-button,
.rollback-toggle,
.action-button {
  min-height: 32px;
  padding: 6px 11px;
  border: 1px solid #b8c8dc;
  background: #fff;
  color: #1a3a6b;
  font: inherit;
  font-size: 12px;
  text-decoration: none;
  cursor: pointer;
}

.element-link:hover,
.icon-button:hover:not(:disabled),
.rollback-toggle:hover:not(:disabled) {
  border-color: #1a3a6b;
}

.element-link.is-disabled {
  color: #8a9aaf;
  background: #f2f5f8;
  cursor: default;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.metric-strip {
  display: flex;
  gap: 7px;
  flex-wrap: wrap;
  padding: 10px 0;
}

.metric-badge {
  min-height: 26px;
  padding: 4px 9px;
  border: 1px solid #d4deea;
  background: #f8fafc;
  color: #526985;
  font-size: 11px;
}

.manager-badge { color: #1a3a6b; border-color: #b6c7dc; }
.is-healthy,
.budget-ok { color: #24623e; border-color: #acd0ba; background: #f1f8f3; }
.is-degraded,
.budget-warning { color: #8a6110; border-color: #dac180; background: #fff9e8; }
.budget-danger { color: #9f3329; border-color: #e0aaa4; background: #fdf0ee; }

.compat-notice,
.inline-error,
.action-message {
  margin-bottom: 10px;
  padding: 8px 11px;
  border-left: 3px solid #b8860b;
  background: #fff9e8;
  color: #725714;
  font-size: 12px;
  line-height: 1.5;
}

.inline-error,
.action-message.error {
  border-left-color: #c0392b;
  background: #fdecea;
  color: #8f2b22;
}

.action-message:not(.error) {
  border-left-color: #2e7d52;
  background: #edf7f0;
  color: #24623e;
}

.team-section {
  border: 1px solid #dce4ee;
  background: rgba(255, 255, 255, 0.72);
  padding: 12px;
}

.roles-section {
  margin-bottom: 10px;
}

.section-heading {
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.section-heading h3 {
  margin: 0;
  color: #314f78;
  font-size: 13px;
}

.section-heading span {
  color: #8494aa;
  font-size: 11px;
}

.event-heading-actions {
  display: flex;
  align-items: center;
  gap: 7px;
}

.event-filter {
  min-height: 27px;
  padding: 3px 24px 3px 7px;
  border: 1px solid #cbd6e3;
  background: #fff;
  color: #536a88;
  font: inherit;
  font-size: 10px;
}

.role-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.role-card {
  min-width: 0;
  padding: 9px;
  border: 1px solid #e0e7f0;
  background: #fbfcfe;
}

.role-card-top {
  gap: 6px;
}

.role-card strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: #a8b4c4;
}

.role-card.status-running .status-dot { background: #2d68a5; box-shadow: 0 0 0 3px #e0edf9; }
.role-card.status-completed .status-dot { background: #2e7d52; }
.role-card.status-failed .status-dot,
.role-card.status-blocked .status-dot { background: #c0392b; }
.role-card.status-degraded .status-dot,
.role-card.status-waiting_approval .status-dot { background: #b8860b; }
.role-card.status-skipped .status-dot { background: #8291a5; }

.role-status {
  margin-left: auto;
  flex-shrink: 0;
  color: #71839d;
  font-size: 10px;
}

.role-card p,
.role-card small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.role-card p {
  margin: 6px 0 0;
  color: #637792;
  font-size: 10px;
}

.role-card small {
  margin-top: 5px;
  color: #9b4b42;
  font-size: 10px;
}

.member-progress,
.task-progress {
  height: 3px;
  overflow: hidden;
  margin-top: 7px;
  background: #e6ecf3;
}

.member-progress span,
.task-progress span {
  display: block;
  height: 100%;
  background: #3d6ea8;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(300px, 1fr);
  gap: 10px;
}

.task-section,
.event-section {
  min-height: 300px;
  max-height: 430px;
  overflow-y: auto;
}

.task-list,
.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.task-row {
  display: grid;
  grid-template-columns: 23px 1fr;
  gap: 8px;
  padding: 8px 0;
  border-top: 1px solid #edf1f6;
}

.task-row:first-child,
.event-row:first-child { border-top: 0; }

.task-state {
  display: grid;
  place-items: center;
  width: 21px;
  height: 21px;
  border-radius: 50%;
  background: #eef2f7;
  color: #8190a5;
  font-size: 11px;
}

.task-row.status-running .task-state { background: #1a3a6b; color: #fff; }
.task-row.status-completed .task-state { background: #2e7d52; color: #fff; }
.task-row.status-failed .task-state,
.task-row.status-blocked .task-state { background: #c0392b; color: #fff; }
.task-row.status-waiting_approval .task-state { background: #b8860b; color: #fff; }

.task-title-row {
  justify-content: space-between;
  gap: 8px;
}

.task-title-row strong { font-size: 12px; }

.approval-mini,
.approval-kicker {
  padding: 2px 6px;
  background: #fff2cb;
  color: #79590f;
  font-size: 9px;
}

.task-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
  color: #8292a8;
  font-size: 10px;
}

.task-runtime-meta {
  color: #506b8c;
  font-variant-numeric: tabular-nums;
}

.task-body > p {
  margin: 5px 0 0;
  color: #62758f;
  font-size: 10px;
  line-height: 1.4;
}

.event-row {
  display: grid;
  grid-template-columns: 12px 1fr;
  gap: 8px;
  padding: 8px 0;
  border-top: 1px solid #edf1f6;
}

.event-marker {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: #8393a9;
}

.event-success { background: #2e7d52; }
.event-danger { background: #c0392b; }
.event-warning { background: #b8860b; }
.event-handoff { background: #3d6ea8; box-shadow: 0 0 0 3px #e4edf8; }

.event-topline {
  justify-content: space-between;
  gap: 8px;
}

.event-topline strong { font-size: 11px; }
.event-topline time { flex-shrink: 0; color: #94a1b2; font-size: 9px; }

.handoff-route,
.event-message {
  margin: 3px 0 0;
  font-size: 10px;
  line-height: 1.4;
}

.handoff-route { color: #3d6ea8; }
.event-message { color: #657893; }

.empty-state {
  display: grid;
  place-items: center;
  min-height: 220px;
  color: #8b99ab;
  font-size: 12px;
}

.approval-panel {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(280px, 1fr);
  gap: 16px;
  margin-top: 10px;
  padding: 13px;
  border: 1px solid #d3b96e;
  background: #fffaf0;
}

.approval-copy h3 {
  margin: 5px 0;
  font-size: 14px;
}

.approval-copy p {
  margin: 0 0 4px;
  color: #6f6242;
  font-size: 11px;
  line-height: 1.5;
}

.approval-copy small { color: #88754c; font-size: 10px; }

.approval-controls label,
.rollback-form label {
  display: grid;
  gap: 5px;
  color: #5e6f86;
  font-size: 10px;
}

.approval-controls textarea,
.rollback-form input,
.rollback-form select {
  width: 100%;
  border: 1px solid #c8d3e0;
  background: #fff;
  color: #1a3a6b;
  padding: 7px 8px;
  font: inherit;
  font-size: 11px;
}

.approval-controls textarea { resize: vertical; }
.approval-actions { justify-content: flex-end; gap: 7px; margin-top: 7px; }

.action-button {
  color: #fff;
  border-color: transparent;
}

.action-button.approve { background: #2e7d52; }
.action-button.reject { background: #a13c32; }

.rollback-entry {
  gap: 9px;
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #dfe6ee;
}

.rollback-toggle { color: #8f342c; border-color: #d9b7b3; }
.readonly-hint { color: #8b99aa; font-size: 10px; }

.rollback-form {
  width: 100%;
  align-items: end;
  gap: 8px;
  padding: 10px;
  background: #faf5f4;
  border: 1px solid #ead6d3;
}

.rollback-form label { min-width: 200px; }
.rollback-reason-label { flex: 1; }

@media (max-width: 1160px) {
  .role-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .detail-grid { grid-template-columns: 1fr; }
}

@media (max-width: 700px) {
  .team-header { align-items: flex-start; flex-direction: column; }
  .team-header-actions { width: 100%; flex-wrap: wrap; }
  .role-grid { grid-template-columns: 1fr; }
  .approval-panel { grid-template-columns: 1fr; }
  .rollback-form { align-items: stretch; flex-direction: column; }
}
</style>
