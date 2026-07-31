<template>
  <div class="report-view">
    <header class="toolbar">
      <div class="toolbar-left">
        <h1>{{ report?.title || '投研报告' }}</h1>
        <p v-if="report?.summary" class="summary">{{ report.summary }}</p>
        <div class="run-selector-row">
          <label v-if="runs.length">
            历史 Run
            <select v-model="selectedRunId" @change="switchRun">
              <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
                {{ runLabel(run) }}
              </option>
            </select>
          </label>
          <span v-if="report?.analysis_mode" class="analysis-badge">
            {{ report.analysis_mode === 'evidence_debate' ? '证据辩论' : '直接分析' }}
          </span>
          <span v-if="report?.debate_status === 'fallback_direct'" class="fallback-badge">辩论未完成</span>
        </div>
      </div>
      <div class="toolbar-actions">
        <button class="btn" @click="showSubscribe = true">开启追踪</button>
        <button class="btn secondary" @click="createScenario">情景推演</button>
        <button class="btn secondary" @click="exportMd">导出 Markdown</button>
        <button class="btn secondary" @click="showReview = true">审校记录</button>
        <button
          v-if="report?.analysis_mode === 'evidence_debate' || report?.debate_status"
          class="btn secondary"
          @click="openDebate"
        >辩论记录</button>
        <button class="btn secondary" @click="chatOpen = !chatOpen">
          {{ chatOpen ? '关闭对话' : '追问报告' }}
        </button>
      </div>
    </header>

    <div class="report-body" :class="{ 'with-chat': chatOpen }">
      <article
        ref="articleRef"
        class="markdown-body report-content"
        v-html="renderedHtml"
        @click="onContentClick"
      />

      <aside v-if="chatOpen" class="chat-drawer">
        <div class="chat-header">
          <span>追问报告</span>
          <button @click="chatOpen = false">Esc 关闭</button>
        </div>
        <div class="chat-messages">
          <div v-for="(m, i) in chatHistory" :key="i" :class="['msg', m.role]">
            <div class="msg-content">{{ m.content }}</div>
            <details v-if="m.tool_calls?.length" class="tool-calls">
              <summary>查证过程（{{ m.tool_calls.length }} 步）</summary>
              <pre>{{ JSON.stringify(m.tool_calls, null, 2) }}</pre>
            </details>
          </div>
        </div>
        <div class="chat-input">
          <input
            v-model="chatQuestion"
            placeholder="例如：归母净利润差多少？"
            @keyup.enter="sendChat"
          />
          <button class="btn" :disabled="chatBusy" @click="sendChat">发送</button>
        </div>
      </aside>
    </div>

    <div v-for="(sec, idx) in contentSections" :key="idx" class="section-feedback">
      <FeedbackBar
        :key="`${selectedRunId}-${idx}`"
        :task-id="taskId"
        :run-id="selectedRunId"
        :section-index="idx + 1"
        :initial-vote="sectionVotes[idx + 1] || ''"
      />
    </div>

    <StarRating
      :key="selectedRunId"
      :task-id="taskId"
      :run-id="selectedRunId"
      :initial-stars="reportStars"
    />

    <SubscribeDialog
      :visible="showSubscribe"
      :task-id="taskId"
      @close="showSubscribe = false"
    />

    <div v-if="showReview" class="dialog-overlay" @click.self="showReview = false">
      <div class="review-dialog">
        <h3>审校记录</h3>
        <div v-if="reviewLoading" class="muted">加载中…</div>
        <div v-else-if="!reviewLog.length" class="muted">暂无审校记录</div>
        <div v-else class="review-list">
          <div v-for="(r, i) in reviewLog" :key="i" class="review-item">
            <span class="verdict" :class="r.verdict">{{ r.verdict || '—' }}</span>
            <span>{{ r.section_title || r.section || r.message || JSON.stringify(r) }}</span>
          </div>
        </div>
        <button class="btn" @click="showReview = false">关闭</button>
      </div>
    </div>

    <div v-if="showDebate" class="dialog-overlay" @click.self="showDebate = false">
      <div class="debate-dialog">
        <div class="dialog-head">
          <h3>结构化辩论记录</h3>
          <button type="button" @click="showDebate = false">×</button>
        </div>
        <p class="drawer-note">仅展示观点、证据、反证、审计和裁决，不展示原始思维链。</p>
        <DebatePanel
          :debate="debateData"
          :loading="debateLoading"
          @evidence="jumpToEvidence"
        />
      </div>
    </div>

    <div v-if="selectedEvidenceCard" class="dialog-overlay evidence-card-overlay" @click.self="closeEvidenceCard">
      <EvidencePopover
        :id="selectedEvidenceDisplayId"
        :card="selectedEvidenceCard"
        standalone
        @close="closeEvidenceCard"
      />
    </div>

    <div v-if="loading" class="loading">加载报告…</div>
    <p v-if="error" class="err">{{ error }}</p>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, h, createApp, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import * as echarts from 'echarts'
import 'github-markdown-css/github-markdown.css'
import { reportApi, feedbackApi, scenarioApi, taskApi } from '../api/index.js'
import FeedbackBar from '../components/FeedbackBar.vue'
import StarRating from '../components/StarRating.vue'
import SubscribeDialog from '../components/SubscribeDialog.vue'
import EvidencePopover from '../components/EvidencePopover.vue'
import DebatePanel from '../components/DebatePanel.vue'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId
const selectedRunId = ref(String(route.query.run_id || ''))
const runs = ref([])

const report = ref(null)
const loading = ref(true)
const error = ref('')
const sourcesIndex = ref({})
const chatOpen = ref(false)
const chatQuestion = ref('')
const chatHistory = ref([])
const chatBusy = ref(false)
const showSubscribe = ref(false)
const showReview = ref(false)
const showDebate = ref(false)
const debateData = ref(null)
const debateLoading = ref(false)
const reviewLog = ref([])
const reviewLoading = ref(false)
const sectionVotes = ref({})
const reportStars = ref(0)
const articleRef = ref(null)
const selectedEvidenceCard = ref(null)
const selectedEvidenceRef = ref('')

const contentSections = computed(() => report.value?.sections?.filter((s) => !s.system) || [])
const selectedEvidenceDisplayId = computed(() => {
  const displayId = selectedEvidenceCard.value?.display_id || selectedEvidenceRef.value
  return String(displayId || '').replace(/^E(?=\d+$)/, '')
})

const renderedHtml = computed(() => {
  const md = report.value?.markdown || buildMarkdownFromSections()
  if (!md) return ''
  // 先把 ```chart 换成占位，避免 marked 弄丢；渲染后再挂 ECharts
  const charts = []
  const md2 = md.replace(/```chart\s*([\s\S]*?)```/g, (_, body) => {
    const id = charts.length
    charts.push(body.trim())
    return `\n\n<div class="chart-mount" data-chart-id="${id}"></div>\n\n`
  })
  let html = marked.parse(md2, { gfm: true, breaks: true })
  html = html.replace(/\[E(\d+)\]/g, '<sup class="evidence-ref-slot" data-ev-id="$1"></sup>')
  html = html.replace(/\[S(\d+)\]/g, '<sup class="scenario-ref-slot" data-ev-id="$1"></sup>')
  // 把 chart JSON 塞进 data 属性供 renderCharts 读取
  html = html.replace(
    /class="chart-mount" data-chart-id="(\d+)"/g,
    (_, id) => {
      const raw = charts[Number(id)] || '{}'
      const encoded = encodeURIComponent(raw)
      return `class="chart-mount" data-chart-id="${id}" data-chart-json="${encoded}"`
    }
  )
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['data-ev-id', 'data-chart-id', 'data-chart-json'],
    ADD_TAGS: ['div'],
  })
})

function buildMarkdownFromSections() {
  const secs = report.value?.sections || []
  return secs.map((s) => `## ${s.title}\n\n${s.content || ''}`).join('\n\n')
}

onMounted(async () => {
  await loadRuns()
  await loadReport()
  await loadFeedback()
  window.addEventListener('keydown', onKeydown)
})

watch(renderedHtml, async () => {
  await nextTick()
  mountEvidenceRefs()
  renderCharts()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
})

function onKeydown(e) {
  if (e.key === 'Escape' && chatOpen.value) chatOpen.value = false
}

async function loadReport() {
  loading.value = true
  error.value = ''
  try {
    const res = await reportApi.get(taskId, selectedRunId.value)
    report.value = res?.data
    if (!selectedRunId.value) {
      selectedRunId.value = report.value?.run_id || ''
    }
    await loadSources()
  } catch (e) {
    error.value = e.message || '报告尚未生成'
  } finally {
    loading.value = false
  }
}

async function loadRuns() {
  try {
    const res = await taskApi.runs(taskId)
    const data = res?.data || {}
    runs.value = data.runs || data.items || (Array.isArray(data) ? data : [])
    if (!selectedRunId.value && runs.value.length) {
      const published =
        runs.value.find((run) => run.is_latest_published) ||
        runs.value.find((run) => run.report_ready) ||
        runs.value.find((run) => ['completed', 'completed_partial'].includes(run.status))
      selectedRunId.value = published?.run_id || ''
    }
  } catch {
    runs.value = []
  }
}

async function switchRun() {
  router.replace({ query: selectedRunId.value ? { run_id: selectedRunId.value } : {} })
  report.value = null
  sourcesIndex.value = {}
  reviewLog.value = []
  debateData.value = null
  selectedEvidenceCard.value = null
  selectedEvidenceRef.value = ''
  sectionVotes.value = {}
  reportStars.value = 0
  await loadReport()
  await loadFeedback()
}

function runLabel(run) {
  const mode = run.analysis_mode === 'evidence_debate' ? '辩论' : '直接'
  const created = run.created_at || run.started_at
  const date = created ? String(created).replace('T', ' ').slice(0, 16) : ''
  const short = String(run.run_id || '').slice(0, 8)
  return `${date || short} · ${mode}${run.status ? ` · ${run.status}` : ''}`
}

async function loadSources() {
  try {
    const ev = await taskApi.evidence(taskId, selectedRunId.value ? { run_id: selectedRunId.value } : {})
    const items = ev?.data?.items || report.value?.sources || []
    const idx = {}
    for (const item of items) {
      const id = item.id ?? item.card_id
      if (id != null) idx[id] = item
      if (typeof id === 'string' && /^E\d+$/.test(id)) idx[id.slice(1)] = item
      if (item.display_id) idx[String(item.display_id).replace(/^E/, '')] = item
      if (item.evidence_uid) idx[item.evidence_uid] = item
    }
    if (report.value?.sources) {
      for (const s of report.value.sources) {
        const id = s.id ?? s.card_id
        if (id != null) idx[id] = s
        if (typeof id === 'string' && /^E\d+$/.test(id)) idx[id.slice(1)] = s
        if (s.display_id) idx[String(s.display_id).replace(/^E/, '')] = s
        if (s.evidence_uid) idx[s.evidence_uid] = s
      }
    }
    sourcesIndex.value = idx
  } catch {
    sourcesIndex.value = {}
  }
}

function mountEvidenceRefs() {
  const el = articleRef.value
  if (!el) return
  el.querySelectorAll('.evidence-ref-slot, .scenario-ref-slot').forEach((slot) => {
    const id = slot.dataset.evId
    const variant = slot.classList.contains('scenario-ref-slot') ? 'scenario' : 'evidence'
    const app = createApp({
      render: () => h(EvidencePopover, { id, card: sourcesIndex.value[id], variant }),
    })
    const mount = document.createElement('span')
    slot.replaceWith(mount)
    app.mount(mount)
  })
}

function renderCharts() {
  const el = articleRef.value
  if (!el) return
  el.querySelectorAll('.chart-mount').forEach((mount) => {
    if (mount.dataset.rendered) return
    try {
      const raw = decodeURIComponent(mount.dataset.chartJson || '{}')
      const spec = JSON.parse(raw)
      mount.className = 'echart-container'
      mount.style.height = '320px'
      mount.dataset.rendered = '1'
      const chart = echarts.init(mount)
      chart.setOption(buildChartOption(spec))
      const refs = (spec.source_refs || []).join(' ')
      if (refs) {
        const cap = document.createElement('div')
        cap.className = 'chart-refs muted'
        cap.textContent = `来源 ${refs}`
        mount.after(cap)
      }
    } catch {
      /* skip invalid chart blocks */
    }
  })
  // 兼容旧路径：marked 生成的 code.language-chart
  el.querySelectorAll('pre code.language-chart, pre code[class*="chart"]').forEach((codeEl) => {
    try {
      const spec = JSON.parse(codeEl.textContent)
      const container = document.createElement('div')
      container.className = 'echart-container'
      container.style.height = '320px'
      codeEl.parentElement.replaceWith(container)
      const chart = echarts.init(container)
      chart.setOption(buildChartOption(spec))
    } catch {
      /* skip */
    }
  })
}

function buildChartOption(spec) {
  const type = spec.type || 'line'
  const labels = spec.x || spec.labels || []
  const series = spec.series || []
  const title = spec.title ? { text: spec.title, left: 'center' } : undefined

  if (type === 'bar') {
    return {
      title,
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: labels },
      yAxis: { type: 'value' },
      series: series.length
        ? series.map((s) => ({ type: 'bar', name: s.name, data: s.data || [] }))
        : [{ type: 'bar', data: spec.values || [] }],
      color: ['#1a3a6b', '#b8860b', '#2e7d52'],
    }
  }
  if (type === 'timeline') {
    const events = spec.events || []
    return {
      title,
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: events.map((e) => e.date || e.label) },
      yAxis: { type: 'value' },
      series: [{
        type: 'line',
        data: events.map((e) => e.value ?? 1),
        smooth: true,
      }],
      color: ['#b8860b'],
    }
  }
  return {
    title,
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { top: 28 } : undefined,
    grid: series.length > 1 ? { top: 64 } : undefined,
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value' },
    series: series.length
      ? series.map((s) => ({ type: 'line', name: s.name, data: s.data || [], smooth: true }))
      : [{ type: 'line', data: spec.values || [], smooth: true }],
    color: ['#1a3a6b', '#b8860b', '#2e7d52'],
  }
}

async function loadFeedback() {
  try {
    const res = await feedbackApi.get(taskId, selectedRunId.value)
    const items = res?.data || []
    for (const f of items) {
      if (f.kind === 'section_vote' || f.vote) {
        sectionVotes.value[f.section_index] = f.vote
      }
      if (f.stars) reportStars.value = f.stars
    }
  } catch {
    /* optional */
  }
}

async function sendChat() {
  const q = chatQuestion.value.trim()
  if (!q || chatBusy.value) return
  chatHistory.value.push({ role: 'user', content: q })
  chatQuestion.value = ''
  chatBusy.value = true
  try {
    const res = await reportApi.chat(taskId, q, chatHistory.value.slice(0, -1), selectedRunId.value)
    const d = res?.data || {}
    chatHistory.value.push({
      role: 'assistant',
      content: d.answer || d.response || '无回答',
      tool_calls: d.tool_calls,
    })
  } catch (e) {
    chatHistory.value.push({ role: 'assistant', content: e.message || '请求失败' })
  } finally {
    chatBusy.value = false
  }
}

async function exportMd() {
  try {
    const md = await reportApi.markdown(taskId, selectedRunId.value)
    const blob = new Blob([typeof md === 'string' ? md : report.value?.markdown || ''], {
      type: 'text/markdown',
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `report_${taskId}.md`
    a.click()
  } catch {
    const blob = new Blob([report.value?.markdown || ''], { type: 'text/markdown' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `report_${taskId}.md`
    a.click()
  }
}

async function createScenario() {
  try {
    const hypothesis =
      report.value?.summary ||
      report.value?.title ||
      '基于当前报告结论进行情景推演'
    const res = await scenarioApi.create({
      task_id: taskId,
      hypothesis,
      ...(selectedRunId.value ? { run_id: selectedRunId.value } : {}),
    })
    const id = res?.data?.scenario_id
    if (id) router.push({ name: 'Scenario', params: { scenarioId: id } })
  } catch (e) {
    error.value = e.message || '创建推演失败'
  }
}

function onContentClick(e) {
  const btn = e.target.closest('[data-scenario-trigger]')
  if (btn) {
    const eid = btn.dataset.evidenceId
    const hypothesis = `若证据 E${eid} 所述情景发生，市场将如何演化？`
    scenarioApi
      .create({
        task_id: taskId,
        ...(selectedRunId.value ? { run_id: selectedRunId.value } : {}),
        hypothesis,
        from_evidence_id: parseInt(eid, 10),
      })
      .then((res) => {
        const id = res?.data?.scenario_id
        if (id) router.push({ name: 'Scenario', params: { scenarioId: id } })
      })
      .catch((err) => {
        error.value = err.message || '创建推演失败'
      })
  }
}

watch(showReview, async (v) => {
  if (!v) return
  reviewLoading.value = true
  try {
    const res = await reportApi.reviewLog(taskId, selectedRunId.value)
    reviewLog.value = res?.data?.items || res?.data || []
  } catch {
    reviewLog.value = []
  } finally {
    reviewLoading.value = false
  }
})

async function openDebate() {
  showDebate.value = true
  if (debateData.value) return
  debateLoading.value = true
  try {
    const res = await taskApi.debate(taskId, selectedRunId.value)
    debateData.value = res?.data || res || null
  } catch (e) {
    debateData.value = null
    error.value = e.message || '辩论记录暂不可用'
  } finally {
    debateLoading.value = false
  }
}

async function jumpToEvidence(ref) {
  showDebate.value = false
  await nextTick()
  const raw = String(ref || '').trim().replace(/^\[/, '').replace(/\]$/, '')
  const normalized = raw.replace(/^E(?=\d+$)/, '')
  const escaped = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(normalized) : normalized
  const target = articleRef.value?.querySelector(`[data-evidence-ref="${escaped}"]`)
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'center' })
    target.querySelector('.evidence-ref')?.click()
  } else {
    const card = sourcesIndex.value[raw] || sourcesIndex.value[normalized]
    if (card) {
      selectedEvidenceRef.value = raw
      selectedEvidenceCard.value = card
      error.value = ''
    } else {
      error.value = `当前冻结证据快照中未找到：${ref}`
    }
  }
}

function closeEvidenceCard() {
  selectedEvidenceCard.value = null
  selectedEvidenceRef.value = ''
}
</script>

<style scoped>
.report-view {
  max-width: 1100px;
  margin: 0 auto;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #d9e2ef;
}

.toolbar h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.summary {
  margin: 0;
  color: #4a6285;
  font-size: 14px;
}

.run-selector-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  color: #6a7f9c;
  font-size: 12px;
}

.run-selector-row label {
  display: flex;
  align-items: center;
  gap: 6px;
}

.run-selector-row select {
  max-width: 290px;
  border: 1px solid #c5d2e5;
  background: #fff;
  color: #1a3a6b;
  padding: 5px 7px;
  font: inherit;
}

.analysis-badge,
.fallback-badge {
  padding: 3px 7px;
  background: #e8eef5;
  color: #1a3a6b;
}

.evidence-card-overlay {
  z-index: 1300;
}

.fallback-badge {
  background: #fff3e0;
  color: #a25315;
}

.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: flex-start;
}

.report-body {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0;
}

.report-body.with-chat {
  grid-template-columns: 1fr 340px;
}

.report-content {
  background: rgba(255, 255, 255, 0.85);
  padding: 28px 32px;
  border: 1px solid rgba(26, 58, 107, 0.08);
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
}

.report-content :deep(h1),
.report-content :deep(h2) {
  color: #1a3a6b;
  border-bottom: 1px solid #eef3f8;
  padding-bottom: 8px;
}

.report-content :deep(table) {
  font-size: 14px;
}

.chat-drawer {
  background: #fff;
  border: 1px solid #d9e2ef;
  display: flex;
  flex-direction: column;
  max-height: 70vh;
  position: sticky;
  top: 16px;
}

.chat-header {
  display: flex;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid #eef3f8;
  font-weight: 600;
}

.chat-header button {
  background: none;
  border: none;
  color: #6a7f9c;
  cursor: pointer;
  font-size: 12px;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.msg {
  margin-bottom: 12px;
  font-size: 14px;
}

.msg.user .msg-content {
  background: #e8eef5;
  padding: 8px 12px;
  border-radius: 4px;
}

.msg.assistant .msg-content {
  background: #f3f7fc;
  padding: 8px 12px;
  border-radius: 4px;
}

.tool-calls {
  font-size: 11px;
  margin-top: 6px;
  color: #6a7f9c;
}

.chat-input {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #eef3f8;
}

.chat-input input {
  flex: 1;
  border: 1px solid #c5d2e5;
  padding: 8px;
  font: inherit;
}

.btn {
  border: none;
  background: #1a3a6b;
  color: #fff;
  padding: 8px 14px;
  font: inherit;
  cursor: pointer;
  font-size: 13px;
}

.btn.secondary {
  background: transparent;
  color: #1a3a6b;
  border: 1px solid #c5d2e5;
}

.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(26, 58, 107, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.review-dialog {
  background: #fff;
  padding: 24px;
  width: 520px;
  max-width: 90vw;
  max-height: 70vh;
  overflow-y: auto;
}

.debate-dialog {
  background: #fff;
  padding: 20px;
  width: min(1040px, 94vw);
  max-height: 88vh;
  overflow-y: auto;
}

.dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #eef3f8;
  padding-bottom: 10px;
  margin-bottom: 8px;
}

.dialog-head h3 {
  margin: 0;
}

.dialog-head button {
  border: 0;
  background: transparent;
  color: #6a7f9c;
  font-size: 24px;
  cursor: pointer;
}

.drawer-note {
  margin: 0 0 14px;
  color: #6a7f9c;
  font-size: 12px;
}

.review-item {
  padding: 8px 0;
  border-bottom: 1px solid #eef3f8;
  font-size: 14px;
  display: flex;
  gap: 10px;
}

.verdict {
  font-size: 12px;
  padding: 2px 6px;
  background: #e8eef5;
}

.verdict.pass { background: #e8f5e9; color: #2e7d52; }
.verdict.revise { background: #fff3e0; color: #e67e22; }

.loading, .muted {
  color: #6a7f9c;
  padding: 24px;
  text-align: center;
}

.err {
  color: #c0392b;
  padding: 12px;
}
</style>
