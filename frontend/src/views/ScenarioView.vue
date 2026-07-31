<template>
  <div class="scenario-view">
    <div v-if="banner" class="sim-banner">{{ banner }}</div>

    <!-- 配置确认 -->
    <template v-if="phase === 'config'">
      <header class="page-head">
        <h1>情景推演配置</h1>
        <p class="muted">确认假设与参数后开始模拟</p>
      </header>
      <form v-if="config" class="panel" @submit.prevent="startScenario">
        <div class="field">
          <label>核心假设</label>
          <textarea v-model="config.hypothesis" rows="3" />
        </div>
        <div class="field">
          <label>注入事件（每行一条）</label>
          <textarea v-model="eventsText" rows="4" placeholder="例如：央行降息 25bp" />
        </div>
        <div class="field row-2">
          <div>
            <label>Agent 规模：{{ config.agent_count }}</label>
            <input v-model.number="config.agent_count" type="range" min="20" max="200" step="10" />
          </div>
          <div>
            <label>模拟轮数：{{ config.rounds }}</label>
            <input v-model.number="config.rounds" type="range" min="3" max="20" />
          </div>
        </div>
        <label class="checkbox">
          <input v-model="config.dual_scenario" type="checkbox" />
          开启双情景（悲观 / 基准）
        </label>
        <p class="estimate">预估：约 {{ estMinutes }} 分钟 / ¥{{ estCost }}</p>
        <p v-if="error" class="err">{{ error }}</p>
        <button type="submit" class="btn" :disabled="busy">{{ busy ? '启动中…' : '开始推演' }}</button>
      </form>
    </template>

    <!-- 运行中 -->
    <template v-else-if="phase === 'running'">
      <header class="page-head">
        <h1>推演运行中</h1>
        <p class="status-line">{{ runMessage }} · {{ runProgress }}%</p>
      </header>
      <div class="run-layout">
        <div class="tabs">
          <button
            v-for="tab in scenarioTabs"
            :key="tab.key"
            :class="{ active: activeTab === tab.key }"
            @click="activeTab = tab.key"
          >{{ tab.label }}</button>
        </div>
        <div class="timeline">
          <div v-for="(r, i) in currentRounds" :key="i" class="round">
            <span class="round-num">第 {{ r.round || i + 1 }} 轮</span>
            <p>{{ r.summary || r.message || JSON.stringify(r) }}</p>
          </div>
          <div v-if="!currentRounds.length" class="muted">等待轮次数据…</div>
        </div>
        <AgentLogStream :lines="logLines" />
      </div>
      <button class="btn secondary stop-btn" @click="stopScenario">停止推演</button>
    </template>

    <!-- 报告 -->
    <template v-else-if="phase === 'report'">
      <header class="page-head">
        <h1>推演报告</h1>
        <button class="btn secondary" @click="showInterview = true">角色采访</button>
      </header>
      <article class="markdown-body report-content" v-html="reportHtml" />
    </template>

    <!-- 采访抽屉 -->
    <aside v-if="showInterview" class="interview-drawer">
      <div class="drawer-head">
        <span>角色采访</span>
        <button @click="showInterview = false">×</button>
      </div>
      <p class="sim-note">你正在采访模拟角色，其回答仅代表沙盘内的演化状态</p>
      <div class="char-list">
        <button
          v-for="c in characters"
          :key="c.id"
          :class="{ active: selectedChar === c.id }"
          @click="selectedChar = c.id"
        >
          <strong>{{ c.name }}</strong>
          <span>{{ c.role_type || c.type }}</span>
        </button>
      </div>
      <div class="chat-msgs">
        <div v-for="(m, i) in interviewHistory" :key="i" :class="['msg', m.role]">{{ m.content }}</div>
      </div>
      <div class="chat-input">
        <input v-model="interviewMsg" placeholder="向角色提问…" @keyup.enter="sendInterview" />
        <button class="btn" :disabled="interviewBusy" @click="sendInterview">发送</button>
      </div>
    </aside>

    <div v-if="loading" class="loading">加载推演…</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import 'github-markdown-css/github-markdown.css'
import { scenarioApi, metaApi } from '../api/index.js'
import { usePolling } from '../composables/usePolling.js'
import AgentLogStream from '../components/AgentLogStream.vue'

const route = useRoute()
const scenarioId = route.params.scenarioId

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const banner = ref('')
const phase = ref('config')
const config = ref(null)
const eventsText = ref('')
const runProgress = ref(0)
const runMessage = ref('')
const runData = ref({})
const logLines = ref([])
let logFromLine = 0
const activeTab = ref('baseline')
const showInterview = ref(false)
const characters = ref([])
const selectedChar = ref('')
const interviewHistory = ref([])
const interviewMsg = ref('')
const interviewBusy = ref(false)
const reportHtml = ref('')

const scenarioTabs = computed(() => {
  if (config.value?.dual_scenario) {
    return [
      { key: 'pessimistic', label: '悲观情景' },
      { key: 'baseline', label: '基准情景' },
    ]
  }
  return [{ key: 'baseline', label: '基准情景' }]
})

const currentRounds = computed(() => {
  const actions = runData.value?.recent_actions || []
  const byRound = {}
  for (const a of actions) {
    const rnd = a.round || 0
    if (!byRound[rnd]) byRound[rnd] = { round: rnd, summary: '' }
    const snippet = a.content || a.message || a.role || ''
    byRound[rnd].summary += (byRound[rnd].summary ? '；' : '') + snippet
  }
  return Object.values(byRound).sort((a, b) => (a.round || 0) - (b.round || 0))
})

function mapConfigFromApi(raw = {}) {
  return {
    hypothesis: raw.hypothesis || '',
    agent_count: raw.agent_scale ?? raw.agent_count ?? 50,
    rounds: raw.max_rounds ?? raw.rounds ?? 8,
    dual_scenario: raw.counter_scenario?.enabled !== false,
    injected_events: raw.injected_events || [],
  }
}

function mapConfigToApi(form) {
  const events = eventsText.value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((content, i) => ({
      round: i + 1,
      type: 'official_disclosure',
      content,
      poster_role: 'company_ir',
    }))
  return {
    hypothesis: form.hypothesis,
    agent_scale: form.agent_count,
    max_rounds: form.rounds,
    injected_events: events.length ? events : form.injected_events,
    counter_scenario: {
      enabled: form.dual_scenario,
      hypothesis: form.dual_scenario
        ? '上述事件未发生或影响符合市场一致预期'
        : undefined,
    },
  }
}

function eventsToText(events = []) {
  return events
    .map((e) => (typeof e === 'string' ? e : e.content || ''))
    .filter(Boolean)
    .join('\n')
}

function syncCharactersFromActions(actions = []) {
  const seen = new Set()
  characters.value = actions
    .filter((a) => a.role && !seen.has(a.role) && seen.add(a.role))
    .map((a, i) => ({
      id: a.agent_id || String(i),
      name: a.role,
      role_type: a.role,
    }))
}

const estMinutes = computed(() => Math.round((config.value?.rounds || 5) * 3 + (config.value?.agent_count || 50) / 20))
const estCost = computed(() => Math.round(estMinutes * 0.25))

onMounted(async () => {
  try {
    const meta = await metaApi.disclaimer()
    banner.value = meta?.data?.scenario_banner || '本推演为模拟情景，不代表真实预测'
  } catch {
    banner.value = '本推演为模拟情景，不代表真实预测'
  }
  await loadStatus()
  loading.value = false
})

async function loadStatus() {
  try {
    const res = await scenarioApi.status(scenarioId)
    const d = res?.data || {}
    config.value = mapConfigFromApi(d.config || {})
    eventsText.value = eventsToText(config.value.injected_events)
    syncCharactersFromActions(d.recent_actions || [])

    const st = d.status || 'awaiting_confirm'
    if (st === 'running') {
      phase.value = 'running'
      startPolling()
    } else if (st === 'completed') {
      phase.value = 'report'
      await loadReport()
    } else {
      phase.value = 'config'
    }
  } catch {
    config.value = mapConfigFromApi()
    phase.value = 'config'
  }
}

async function startScenario() {
  busy.value = true
  error.value = ''
  try {
    const scenarioConfig = mapConfigToApi(config.value)
    await scenarioApi.start(scenarioId, scenarioConfig)
    phase.value = 'running'
    startPolling()
  } catch (e) {
    error.value = e.message || '启动失败'
  } finally {
    busy.value = false
  }
}

async function pollRun() {
  const [statusRes, logRes] = await Promise.all([
    scenarioApi.status(scenarioId).catch(() => null),
    scenarioApi.agentLog(scenarioId, logFromLine).catch(() => null),
  ])
  if (statusRes?.data) {
    runProgress.value = statusRes.data.progress ?? runProgress.value
    runMessage.value = statusRes.data.message || runMessage.value
    runData.value = statusRes.data
    syncCharactersFromActions(statusRes.data.recent_actions || [])
    if (statusRes.data.status === 'completed') {
      phase.value = 'report'
      stopPolling()
      await loadReport()
    }
  }
  if (logRes?.data?.lines?.length) {
    logLines.value = [...logLines.value, ...logRes.data.lines]
    logFromLine = logRes.data.next_line ?? logFromLine
  }
  return statusRes?.data
}

let statusPoll = null

function startPolling() {
  statusPoll = usePolling(pollRun, {
    interval: 2000,
    stopWhen: (d) => d?.status === 'completed' || d?.status === 'failed',
  })
  statusPoll.start()
}

function stopPolling() {
  statusPoll?.stop()
}

async function loadReport() {
  try {
    const res = await scenarioApi.report(scenarioId)
    const md = res?.data?.markdown || ''
    reportHtml.value = DOMPurify.sanitize(marked.parse(md, { gfm: true }))
  } catch {
    reportHtml.value = '<p>报告尚未生成</p>'
  }
}

function stopScenario() {
  stopPolling()
  phase.value = 'config'
}

async function sendInterview() {
  const msg = interviewMsg.value.trim()
  if (!msg || interviewBusy.value) return
  interviewHistory.value.push({ role: 'user', content: msg })
  interviewMsg.value = ''
  interviewBusy.value = true
  try {
    const res = await scenarioApi.interview(scenarioId, msg, 3)
    const answers = res?.data?.answers || []
    if (!answers.length) {
      interviewHistory.value.push({ role: 'assistant', content: '暂无角色回答' })
    } else {
      for (const a of answers) {
        interviewHistory.value.push({
          role: 'assistant',
          content: `${a.role || '角色'}：${a.answer || '无回答'}`,
        })
      }
      if (!characters.value.length) {
        characters.value = answers.map((a) => ({
          id: a.agent_id,
          name: a.role,
          role_type: a.role,
        }))
      }
    }
  } catch (e) {
    interviewHistory.value.push({ role: 'assistant', content: e.message })
  } finally {
    interviewBusy.value = false
  }
}

onUnmounted(stopPolling)
</script>

<style scoped>
.scenario-view {
  max-width: 1000px;
  margin: 0 auto;
  position: relative;
}

.sim-banner {
  background: #fff3e0;
  border: 1px solid #b8860b;
  color: #5a4a20;
  padding: 10px 16px;
  margin-bottom: 16px;
  font-size: 14px;
}

.page-head h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.muted, .status-line {
  color: #6a7f9c;
  font-size: 14px;
  margin: 0;
}

.panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 22px;
  margin-top: 16px;
}

.field {
  margin-bottom: 16px;
}

.field label {
  display: block;
  font-size: 13px;
  color: #4a6285;
  margin-bottom: 6px;
}

textarea, input[type="range"] {
  width: 100%;
  border: 1px solid #c5d2e5;
  padding: 10px;
  font: inherit;
}

.row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  margin-bottom: 12px;
}

.estimate {
  color: #b8860b;
  font-size: 14px;
}

.btn {
  border: none;
  background: #1a3a6b;
  color: #fff;
  padding: 10px 20px;
  font: inherit;
  cursor: pointer;
}

.btn.secondary {
  background: transparent;
  color: #1a3a6b;
  border: 1px solid #c5d2e5;
}

.tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.tabs button {
  padding: 8px 16px;
  border: 1px solid #c5d2e5;
  background: #fff;
  cursor: pointer;
  font: inherit;
}

.tabs button.active {
  background: #1a3a6b;
  color: #fff;
  border-color: #1a3a6b;
}

.run-layout {
  display: grid;
  gap: 12px;
}

.timeline {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 14px;
  max-height: 240px;
  overflow-y: auto;
}

.round {
  padding: 8px 0;
  border-bottom: 1px solid #eef3f8;
  font-size: 14px;
}

.round-num {
  color: #b8860b;
  font-weight: 600;
  margin-right: 8px;
}

.stop-btn {
  margin-top: 12px;
}

.report-content {
  background: #fff;
  padding: 28px;
  border: 1px solid rgba(26, 58, 107, 0.08);
  margin-top: 16px;
}

.interview-drawer {
  position: fixed;
  top: 0;
  right: 0;
  width: 380px;
  height: 100vh;
  background: #fff;
  border-left: 1px solid #c5d2e5;
  box-shadow: -8px 0 24px rgba(26, 58, 107, 0.1);
  display: flex;
  flex-direction: column;
  z-index: 150;
  padding: 16px;
}

.drawer-head {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 8px;
}

.drawer-head button {
  background: none;
  border: none;
  font-size: 20px;
  cursor: pointer;
}

.sim-note {
  font-size: 12px;
  color: #b8860b;
  margin-bottom: 12px;
}

.char-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  max-height: 160px;
  overflow-y: auto;
}

.char-list button {
  text-align: left;
  padding: 8px 10px;
  border: 1px solid #d9e2ef;
  background: #fff;
  cursor: pointer;
  font: inherit;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.char-list button.active {
  border-color: #1a3a6b;
  background: #f3f7fc;
}

.chat-msgs {
  flex: 1;
  overflow-y: auto;
  font-size: 14px;
}

.msg {
  padding: 8px;
  margin-bottom: 6px;
  border-radius: 4px;
}

.msg.user { background: #e8eef5; }
.msg.assistant { background: #f3f7fc; }

.chat-input {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.chat-input input {
  flex: 1;
  border: 1px solid #c5d2e5;
  padding: 8px;
  font: inherit;
}

.err { color: #c0392b; }
.loading { color: #6a7f9c; padding: 24px; text-align: center; }
</style>
