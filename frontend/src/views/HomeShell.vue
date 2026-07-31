<template>
  <div class="shell">
    <header class="top">
      <div class="brand">
        <h1>成竹</h1>
        <span class="en">Foresketch</span>
      </div>
      <p class="tagline">胸有成竹 · 先画后行</p>
    </header>

    <main class="main">
      <section class="panel">
        <h2>新建投研整理任务</h2>
        <textarea
          v-model="requirement"
          rows="3"
          placeholder="例如：对比宁德时代与比亚迪近两年财务与公告要点"
        />
        <div class="actions">
          <button class="btn" :disabled="busy || !requirement.trim()" @click="createAndRun">
            {{ busy ? '处理中…' : '解析并开始' }}
          </button>
          <span class="muted">后端：{{ healthText }}</span>
        </div>
        <p v-if="error" class="err">{{ error }}</p>
      </section>

      <section v-if="taskId" class="panel">
        <h2>任务进度</h2>
        <p>
          <code>{{ taskId }}</code>
          · {{ status }} · {{ progress }}%
        </p>
        <p class="muted">{{ message }}</p>
        <ul v-if="card" class="meta">
          <li>交付物：{{ card.deliverable }}</li>
          <li>
            标的：
            {{ (card.symbols || []).map((s) => s.name || s.code).join('、') }}
          </li>
        </ul>
      </section>

      <section v-if="report" class="panel report">
        <h2>{{ report.title }}</h2>
        <p class="muted">{{ report.summary }}</p>
        <article class="md">{{ report.markdown }}</article>
        <div class="chat">
          <h3>追问报告</h3>
          <div class="actions">
            <input v-model="question" placeholder="例如：归母净利润差多少？" @keyup.enter="ask" />
            <button class="btn secondary" :disabled="!question.trim() || chatting" @click="ask">
              提问
            </button>
          </div>
          <p v-if="answer" class="answer">{{ answer }}</p>
        </div>
      </section>

      <section class="panel disclaimer">
        <p>{{ disclaimer }}</p>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import axios from 'axios'

const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5001'
const healthText = ref('检测中…')
const disclaimer = ref('本系统仅做信息整理与情景观察，不构成投资建议。')
const requirement = ref('对比宁德时代与比亚迪近两年财务与公告要点')
const busy = ref(false)
const error = ref('')
const taskId = ref('')
const status = ref('')
const progress = ref(0)
const message = ref('')
const card = ref(null)
const report = ref(null)
const question = ref('')
const answer = ref('')
const chatting = ref(false)
let pollTimer = null

async function ping() {
  try {
    const { data } = await axios.get(`${API}/api/health`, { timeout: 5000 })
    healthText.value = data?.status === 'ok' ? `OK` : '异常'
  } catch (e) {
    healthText.value = `失败`
  }
  try {
    const { data } = await axios.get(`${API}/api/meta/disclaimer`, { timeout: 5000 })
    if (data?.success && data.data?.disclaimer) disclaimer.value = data.data.disclaimer
  } catch (_) {}
}

async function createAndRun() {
  busy.value = true
  error.value = ''
  report.value = null
  answer.value = ''
  try {
    const { data } = await axios.post(
      `${API}/api/task/create`,
      { requirement: requirement.value.trim() },
      { headers: { 'Content-Type': 'application/json' } }
    )
    if (!data?.success) throw new Error(data?.error || '创建失败')
    taskId.value = data.data.task_id
    card.value = data.data.task_card
    status.value = data.data.status

    const conf = await axios.post(`${API}/api/task/${taskId.value}/confirm`, {
      task_card: card.value,
    })
    if (!conf.data?.success) throw new Error(conf.data?.error || '确认失败')
    status.value = 'collecting'
    progress.value = 5
    message.value = '采集与分析进行中…'
    startPoll()
  } catch (e) {
    error.value = e.response?.data?.error || e.message
    busy.value = false
  }
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    if (!taskId.value) return
    try {
      const { data } = await axios.get(`${API}/api/task/${taskId.value}/status`)
      if (!data?.success) return
      status.value = data.data.status
      progress.value = data.data.progress || 0
      message.value = data.data.message || ''
      if (['completed', 'completed_partial', 'failed'].includes(status.value)) {
        stopPoll()
        busy.value = false
        if (status.value === 'completed' || status.value === 'completed_partial') {
          await loadReport()
        }
      }
    } catch (_) {}
  }, 2000)
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function loadReport() {
  try {
    const { data } = await axios.get(`${API}/api/report/${taskId.value}`)
    if (data?.success) report.value = data.data
  } catch (_) {
    /* 可能尚未生成 */
  }
}

async function ask() {
  if (!taskId.value || !question.value.trim()) return
  chatting.value = true
  try {
    const { data } = await axios.post(`${API}/api/report/${taskId.value}/chat`, {
      question: question.value.trim(),
    })
    answer.value = data?.data?.answer || data?.error || '无回答'
  } catch (e) {
    answer.value = e.response?.data?.error || e.message
  } finally {
    chatting.value = false
  }
}

onMounted(ping)
onUnmounted(stopPoll)
</script>

<style scoped>
.shell {
  min-height: 100vh;
  background:
    radial-gradient(1200px 600px at 10% -10%, #d6e4f5 0%, transparent 55%),
    linear-gradient(180deg, #f7f9fc 0%, #eef3f8 100%);
  color: #1a3a6b;
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
}
.top {
  padding: 40px 32px 8px;
  max-width: 920px;
  margin: 0 auto;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 14px;
}
.brand h1 {
  margin: 0;
  font-size: 52px;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.en {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 20px;
  color: #b8860b;
}
.tagline {
  margin: 8px 0 0;
  color: #4a6285;
}
.main {
  max-width: 920px;
  margin: 0 auto;
  padding: 16px 32px 64px;
  display: grid;
  gap: 16px;
}
.panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 20px 22px;
}
.panel h2 {
  margin: 0 0 12px;
  font-size: 22px;
}
textarea,
input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #c5d2e5;
  background: #fff;
  padding: 10px 12px;
  font: inherit;
  color: inherit;
}
.actions {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}
.btn {
  border: none;
  background: #1a3a6b;
  color: #fff;
  padding: 10px 18px;
  font: inherit;
  cursor: pointer;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn.secondary {
  background: #b8860b;
}
.muted {
  color: #6a7f9c;
  font-size: 14px;
}
.err {
  color: #a33;
  margin-top: 8px;
}
.meta {
  padding-left: 18px;
  color: #3d5678;
}
.report .md {
  margin-top: 12px;
  line-height: 1.7;
  font-size: 14px;
  white-space: pre-wrap;
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
}
.chat {
  margin-top: 28px;
  padding-top: 16px;
  border-top: 1px solid #d9e2ef;
}
.answer {
  white-space: pre-wrap;
  margin-top: 12px;
  background: #f3f7fc;
  padding: 12px;
}
.disclaimer {
  font-size: 13px;
  color: #5a6f8c;
}
</style>
