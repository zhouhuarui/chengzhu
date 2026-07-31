<template>
  <div class="task-run">
    <div v-if="showCompleteBanner" class="complete-banner">
      <span>任务已完成</span>
      <button class="btn" @click="goReport">查看报告（{{ countdown }}s）</button>
    </div>

    <div v-else-if="completionWithoutReport" class="fail-banner">
      <p>分析流程已结束，但尚未生成可读取的报告。任务不会被误标为可交付。</p>
      <button class="btn" @click="reload">重新检查</button>
    </div>

    <div v-if="status === 'failed'" class="fail-banner">
      <p>{{ message || '任务失败' }}</p>
      <div class="fail-actions">
        <button class="btn" @click="reload">重试刷新</button>
        <router-link :to="{ name: 'TaskConfirm', params: { taskId } }" class="btn secondary">
          修改任务卡
        </router-link>
      </div>
    </div>

    <header class="run-header">
      <h1>任务运行中</h1>
      <p class="status-line">
        {{ humanMessage }} · {{ progress }}%
        <span v-if="etaSeconds"> · 预计剩余 {{ formatEta(etaSeconds) }}</span>
        <span v-if="runId"> · Run {{ shortRunId }}</span>
      </p>
    </header>

    <div class="three-col">
      <aside class="col pipeline-col">
        <h3>管线进度</h3>
        <PipelineSteps
          :status="status"
          :progress="progress"
          :progress-detail="progressDetail"
        />
      </aside>

      <section class="col log-col">
        <div class="stream-tabs">
          <button :class="{ active: activeTab === 'agents' }" @click="activeTab = 'agents'">
            Agent 流水
          </button>
          <button
            v-if="debateEnabled"
            :class="{ active: activeTab === 'debate' }"
            @click="activeTab = 'debate'"
          >
            辩论面板
          </button>
        </div>
        <AgentLogStream v-if="activeTab === 'agents'" :lines="logLines" />
        <div v-else class="debate-scroll">
          <DebatePanel
            :debate="debateData"
            :loading="debateLoading"
            @evidence="openEvidence"
          />
        </div>
      </section>

      <aside class="col evidence-col">
        <h3>证据统计</h3>
        <div class="evidence-counts">
          <div v-for="row in evidenceRows" :key="row.type" class="ev-row">
            <span class="ev-label">{{ row.label }}</span>
            <span class="ev-count">{{ row.count }}</span>
          </div>
        </div>
        <h3 class="graph-title">知识图谱</h3>
        <div class="graph-placeholder">
          <GraphPanel
            v-if="graphData"
            :graph-data="graphData"
            :loading="graphLoading"
            @refresh="loadGraph"
          />
          <div v-else class="graph-empty">
            <p>{{ graphLoading ? '加载图谱…' : '图谱构建中…' }}</p>
            <div class="mini-nodes">
              <span v-for="n in 6" :key="n" class="mini-node" />
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- 首次引导 -->
    <div v-if="showGuide" class="guide-overlay" @click="dismissGuide">
      <div class="guide-card" @click.stop>
        <h3>运行页导览</h3>
        <ol>
          <li><strong>左侧</strong> — 管线各阶段进度</li>
          <li><strong>中间</strong> — Agent 动作流水与结构化辩论记录</li>
          <li><strong>右侧</strong> — 证据计数与知识图谱</li>
        </ol>
        <button class="btn" @click="dismissGuide">知道了</button>
      </div>
    </div>

    <div v-if="selectedEvidenceRef" class="evidence-overlay" @click.self="closeEvidence">
      <EvidencePopover
        v-if="selectedEvidenceCard"
        :id="selectedEvidenceDisplayId"
        :card="selectedEvidenceCard"
        standalone
        @close="closeEvidence"
      />
      <div v-else class="evidence-missing-card">
        <button type="button" aria-label="关闭证据详情" @click="closeEvidence">×</button>
        <h3>证据 {{ selectedEvidenceRef }}</h3>
        <p>冻结证据仍在发布中，请稍后重试。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { taskApi, reportApi } from '../api/index.js'
import { usePolling } from '../composables/usePolling.js'
import PipelineSteps from '../components/PipelineSteps.vue'
import AgentLogStream from '../components/AgentLogStream.vue'
import GraphPanel from '../components/GraphPanel.vue'
import DebatePanel from '../components/DebatePanel.vue'
import EvidencePopover from '../components/EvidencePopover.vue'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId
const runId = ref(String(route.query.run_id || ''))

const status = ref('')
const progress = ref(0)
const progressDetail = ref({})
const message = ref('')
const etaSeconds = ref(null)
const logLines = ref([])
let logFromLine = 0

const evidenceCounts = ref({})
const evidenceItems = ref([])
const graphData = ref(null)
const graphLoading = ref(false)
const debateData = ref(null)
const debateLoading = ref(false)
const activeTab = ref('agents')
const reportReady = ref(false)
const selectedEvidenceRef = ref('')
const selectedEvidenceCard = ref(null)

const countdown = ref(3)
let countdownTimer = null

const EVIDENCE_TYPES = [
  { type: 'announcement', label: '公告' },
  { type: 'financial_report', label: '财报' },
  { type: 'news', label: '新闻' },
  { type: 'research_report', label: '研报' },
  { type: 'industry_data', label: '行业' },
]

const evidenceRows = computed(() =>
  EVIDENCE_TYPES.map((t) => ({
    ...t,
    count: evidenceCounts.value[t.type] ?? 0,
  }))
)

const showCompleteBanner = computed(() => isSuccessfulTerminal(status.value) && reportReady.value)
const completionWithoutReport = computed(() => isSuccessfulTerminal(status.value) && !reportReady.value)
const debateEnabled = computed(() => (
  progressDetail.value?.analysis_mode === 'evidence_debate' ||
  hasRealDebateMetadata(progressDetail.value?.debate) ||
  hasRealDebateMetadata(debateData.value)
))
const shortRunId = computed(() => runId.value.length > 12 ? `${runId.value.slice(0, 12)}…` : runId.value)
const selectedEvidenceDisplayId = computed(() => {
  const displayId = selectedEvidenceCard.value?.display_id || selectedEvidenceRef.value
  return String(displayId || '').replace(/^E(?=\d+$)/, '')
})

const humanMessage = computed(() => message.value || '处理中')

const showGuide = ref(false)

function isTerminal(s) {
  return ['completed', 'completed_partial', 'failed'].includes(s)
}

function isSuccessfulTerminal(s) {
  return ['completed', 'completed_partial'].includes(s)
}

function debatePayload(value) {
  return value?.data || value || null
}

function hasRealDebateMetadata(value) {
  const data = debatePayload(value)
  if (!data || typeof data !== 'object') return false
  const state = data.progress || data.state || data.metadata || {}
  return Boolean(
    data.status ||
    data.verdict ||
    data.debate_verdict ||
    data.current_round ||
    data.current_role ||
    state.status ||
    state.current_round ||
    state.current_role ||
    (Array.isArray(data.claims) && data.claims.length) ||
    (Array.isArray(data.challenges) && data.challenges.length) ||
    (Array.isArray(data.audit) && data.audit.length)
  )
}

async function pollStatus() {
  const res = await taskApi.status(taskId, runId.value)
  const d = res?.data || {}
  const prev = status.value
  status.value = d.status || ''
  progress.value = d.progress ?? 0
  progressDetail.value = d.progress_detail || {}
  const resolvedRunId = d.run_id || d.current_run_id || d.latest_run_id
  if (!runId.value && resolvedRunId) {
    runId.value = resolvedRunId
    router.replace({ query: { ...route.query, run_id: resolvedRunId } })
  }
  message.value = d.message || ''
  etaSeconds.value = d.eta_seconds ?? null
  if (
    !isTerminal(prev) &&
    isSuccessfulTerminal(d.status)
  ) {
    await checkReport()
    if (reportReady.value) startCountdown()
  }
  if (debateEnabled.value || ['debating', 'adjudicating'].includes(d.status)) pollDebate()
  return d
}

async function pollLog() {
  const res = await taskApi.agentLog(taskId, logFromLine, runId.value)
  const d = res?.data || {}
  if (d.lines?.length) {
    logLines.value = [...logLines.value, ...d.lines]
    logFromLine = d.next_line ?? logFromLine + d.lines.length
  }
  return d
}

async function pollEvidence() {
  try {
    const res = await taskApi.evidence(taskId, runId.value ? { run_id: runId.value } : {})
    const items = res?.data?.items || []
    evidenceItems.value = items
    const counts = {}
    for (const item of items) {
      const t = item.source_type || 'other'
      counts[t] = (counts[t] || 0) + 1
    }
    evidenceCounts.value = counts
  } catch {
    /* optional */
  }
}

async function loadGraph() {
  graphLoading.value = true
  try {
    const res = await taskApi.graph(taskId, runId.value)
    const d = res?.data
    if (d?.nodes?.length) {
      graphData.value = d
    }
  } catch {
    graphData.value = null
  } finally {
    graphLoading.value = false
  }
}

async function pollDebate() {
  if (debateLoading.value) return
  debateLoading.value = true
  try {
    const res = await taskApi.debate(taskId, runId.value)
    debateData.value = res?.data || res || null
  } catch {
    /* Artifacts are optional until the debate stage starts. */
  } finally {
    debateLoading.value = false
  }
}

function normalizeEvidenceRef(ref) {
  return String(ref || '').trim().replace(/^\[/, '').replace(/\]$/, '')
}

function findEvidenceCard(ref) {
  const raw = normalizeEvidenceRef(ref)
  const numeric = raw.replace(/^E(?=\d+$)/, '')
  return evidenceItems.value.find((item) => {
    const display = String(item.display_id || '')
    const cardId = String(item.card_id ?? item.id ?? '')
    return raw === display || raw === String(item.evidence_uid || '') || numeric === cardId
  }) || null
}

async function openEvidence(ref) {
  selectedEvidenceRef.value = normalizeEvidenceRef(ref)
  selectedEvidenceCard.value = findEvidenceCard(ref)
  if (!selectedEvidenceCard.value) {
    await pollEvidence()
    selectedEvidenceCard.value = findEvidenceCard(ref)
  }
}

function closeEvidence() {
  selectedEvidenceRef.value = ''
  selectedEvidenceCard.value = null
}

async function checkReport() {
  try {
    await reportApi.get(taskId, runId.value)
    reportReady.value = true
  } catch {
    reportReady.value = false
  }
}

const statusPoll = usePolling(pollStatus, {
  interval: 2000,
  stopWhen: (d) => isTerminal(d?.status),
})

const logPoll = usePolling(pollLog, {
  interval: 2000,
  stopWhen: () => isTerminal(status.value),
})

const graphPoll = usePolling(loadGraph, {
  interval: 15000,
  immediate: false,
  stopWhen: () => isTerminal(status.value),
})

watch(showCompleteBanner, (v) => {
  if (v) startCountdown()
})

function startCountdown() {
  countdown.value = 3
  clearInterval(countdownTimer)
  countdownTimer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) {
      clearInterval(countdownTimer)
      goReport()
    }
  }, 1000)
}

function goReport() {
  clearInterval(countdownTimer)
  router.push({
    name: 'Report',
    params: { taskId },
    query: runId.value ? { run_id: runId.value } : {},
  })
}

function formatEta(sec) {
  if (sec < 60) return `${Math.round(sec)} 秒`
  return `${Math.round(sec / 60)} 分钟`
}

function reload() {
  pollStatus()
}

function dismissGuide() {
  showGuide.value = false
  localStorage.setItem('chengzhu_run_guide_seen', '1')
}

let evTimer = null

onMounted(async () => {
  if (!localStorage.getItem('chengzhu_run_guide_seen')) {
    showGuide.value = true
  }
  await pollStatus()
  if (isTerminal(status.value)) {
    await checkReport()
    if (showCompleteBanner.value) startCountdown()
    return
  }
  statusPoll.start()
  logPoll.start()
  graphPoll.start()
  pollEvidence()
  evTimer = setInterval(pollEvidence, 5000)
})

onUnmounted(() => {
  statusPoll.stop()
  logPoll.stop()
  graphPoll.stop()
  clearInterval(countdownTimer)
  if (evTimer) clearInterval(evTimer)
})
</script>

<style scoped>
.task-run {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 120px);
  min-height: 560px;
}

.complete-banner,
.fail-banner {
  background: #e8f5e9;
  border: 1px solid #2e7d52;
  padding: 12px 18px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.fail-banner {
  background: #fdecea;
  border-color: #c0392b;
}

.fail-actions {
  display: flex;
  gap: 8px;
}

.evidence-overlay {
  position: fixed;
  inset: 0;
  z-index: 1300;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(18, 38, 67, 0.42);
}

.evidence-missing-card {
  position: relative;
  width: min(420px, calc(100vw - 48px));
  padding: 22px;
  background: #fff;
  border: 1px solid #c5d2e5;
  box-shadow: 0 12px 36px rgba(26, 58, 107, 0.2);
}

.evidence-missing-card button {
  position: absolute;
  top: 8px;
  right: 10px;
  border: 0;
  background: transparent;
  color: #6a7f9c;
  font-size: 20px;
  cursor: pointer;
}

.evidence-missing-card h3 {
  margin: 0 0 8px;
}

.evidence-missing-card p {
  margin: 0;
  color: #6a7f9c;
}

.run-header {
  margin-bottom: 12px;
}

.run-header h1 {
  margin: 0 0 4px;
  font-size: 24px;
}

.status-line {
  margin: 0;
  color: #4a6285;
  font-size: 14px;
}

.three-col {
  flex: 1;
  display: grid;
  grid-template-columns: 240px 1fr 280px;
  gap: 12px;
  min-height: 0;
}

.col {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 14px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.col h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: #4a6285;
  font-weight: 600;
}

.log-col {
  min-height: 0;
}

.stream-tabs {
  display: flex;
  gap: 2px;
  margin-bottom: 10px;
  border-bottom: 1px solid #d9e1ec;
}

.stream-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #6a7f9c;
  padding: 5px 10px 8px;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}

.stream-tabs button.active {
  color: #1a3a6b;
  border-bottom-color: #b8860b;
  font-weight: 700;
}

.log-col :deep(.agent-log-stream) {
  flex: 1;
  min-height: 0;
}

.debate-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 2px;
}

.evidence-counts {
  margin-bottom: 12px;
}

.ev-row {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid #eef3f8;
  font-size: 13px;
}

.ev-count {
  font-weight: 700;
  color: #b8860b;
  font-variant-numeric: tabular-nums;
}

.graph-title {
  margin-top: 8px;
}

.graph-placeholder {
  flex: 1;
  min-height: 180px;
  position: relative;
  overflow: hidden;
}

.graph-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #9aa8bc;
  font-size: 13px;
  background: #f3f7fc;
  border: 1px dashed #c5d2e5;
}

.mini-nodes {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.mini-node {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #1a3a6b;
  opacity: 0.3;
  animation: pulse 1.5s infinite;
}

.mini-node:nth-child(2) { background: #2e7d52; animation-delay: 0.2s; }
.mini-node:nth-child(3) { background: #c0392b; animation-delay: 0.4s; }
.mini-node:nth-child(4) { background: #7b4397; animation-delay: 0.6s; }
.mini-node:nth-child(5) { background: #b8860b; animation-delay: 0.8s; }

@keyframes pulse {
  0%, 100% { opacity: 0.3; transform: scale(1); }
  50% { opacity: 0.8; transform: scale(1.2); }
}

.btn {
  border: none;
  background: #1a3a6b;
  color: #fff;
  padding: 8px 16px;
  font: inherit;
  cursor: pointer;
  text-decoration: none;
  font-size: 14px;
}

.btn.secondary {
  background: transparent;
  color: #1a3a6b;
  border: 1px solid #c5d2e5;
}

.guide-overlay {
  position: fixed;
  inset: 0;
  background: rgba(26, 58, 107, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
}

.guide-card {
  background: #fff;
  padding: 24px 28px;
  max-width: 400px;
  border: 1px solid #c5d2e5;
}

.guide-card ol {
  margin: 12px 0 20px;
  padding-left: 20px;
  line-height: 1.8;
}

@media (max-width: 960px) {
  .three-col {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }
  .task-run {
    height: auto;
  }
}
</style>
