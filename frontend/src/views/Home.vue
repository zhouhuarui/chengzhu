<template>
  <div class="home">
    <header class="hero">
      <p class="tagline">胸有成竹 · 先画后行</p>
      <h1>新建投研整理任务</h1>
    </header>

    <section class="panel">
      <textarea
        v-model="requirement"
        rows="4"
        placeholder="描述你的投研需求，例如：对比宁德时代与比亚迪近两年财务与公告要点"
        @keydown.meta.enter="submit"
        @keydown.ctrl.enter="submit"
      />

      <div class="chips">
        <span class="chips-label">试试示例：</span>
        <button
          v-for="ex in examples"
          :key="ex.key"
          class="chip"
          @click="requirement = ex.text"
        >{{ ex.label }}</button>
      </div>

      <div v-if="watchSymbols.length" class="prefill">
        <span class="chips-label">常用标的：</span>
        <button
          v-for="sym in watchSymbols"
          :key="sym.code || sym.name"
          class="chip amber"
          @click="appendSymbol(sym)"
        >{{ sym.name || sym.code }}</button>
      </div>

      <div
        class="upload-zone"
        :class="{ 'has-files': files.length }"
        @dragover.prevent
        @drop.prevent="onDrop"
        @click="fileInput?.click()"
      >
        <input
          ref="fileInput"
          type="file"
          multiple
          accept=".pdf,.md,.txt,.png,.jpg,.jpeg,.webp"
          hidden
          @change="onFileSelect"
        />
        <template v-if="!files.length">
          <span>可选：拖拽或点击上传 pdf / md / txt / png / jpg / webp（≤50MB）</span>
        </template>
        <div v-else class="file-list">
          <div v-for="(f, i) in files" :key="i" class="file-item">
            {{ f.name }}
            <button @click.stop="files.splice(i, 1)">×</button>
          </div>
        </div>
      </div>

      <div class="actions">
        <button class="btn" :disabled="busy || !requirement.trim()" @click="submit">
          {{ busy ? '解析中…' : '提交并确认任务卡' }}
        </button>
        <span class="muted">Cmd+Enter 快捷提交</span>
      </div>
      <p v-if="error" class="err">{{ error }}</p>
    </section>

    <section class="panel history">
      <h2>历史任务</h2>
      <div v-if="loadingHistory" class="muted">加载中…</div>
      <div v-else-if="!tasks.length" class="empty-state">
        <p>暂无历史任务</p>
        <p class="muted">点击上方示例快速开始第一次研究</p>
      </div>
      <div v-else class="task-grid">
        <article
          v-for="t in tasks"
          :key="t.task_id"
          class="task-card"
          @click="goTask(t)"
        >
          <div class="task-head">
            <span class="status-badge" :class="t.status">{{ statusLabel(t.status) }}</span>
            <span class="deliverable-icon">{{ deliverableIcon(t) }}</span>
          </div>
          <p class="task-req">{{ t.requirement || '未命名任务' }}</p>
          <p class="task-meta">{{ formatDate(t.created_at) }}</p>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { taskApi, memoryApi } from '../api/index.js'

const router = useRouter()
const requirement = ref('')
const files = ref([])
const fileInput = ref(null)
const busy = ref(false)
const error = ref('')
const tasks = ref([])
const loadingHistory = ref(true)
const watchSymbols = ref([])

const examples = [
  { key: 'summary', label: '摘要', text: '整理宁德时代最近一个季度的公告与财务要点，形成投研摘要' },
  { key: 'compare', label: '对比', text: '对比宁德时代与比亚迪近两年财务与公告要点，突出差异与风险' },
  { key: 'tracking', label: '追踪', text: '持续追踪比亚迪的公告与新闻动态，每周汇总变更要点' },
]

onMounted(async () => {
  await Promise.all([loadHistory(), loadPrefill()])
})

async function loadHistory() {
  loadingHistory.value = true
  try {
    const res = await taskApi.list(20)
    tasks.value = res?.data || []
  } catch {
    tasks.value = []
  } finally {
    loadingHistory.value = false
  }
}

async function loadPrefill() {
  try {
    const res = await memoryApi.prefill()
    watchSymbols.value = res?.data?.watch_symbols || []
  } catch {
    watchSymbols.value = []
  }
}

function appendSymbol(sym) {
  const name = sym.name || sym.code
  if (!requirement.value.includes(name)) {
    requirement.value = requirement.value.trim()
      ? `${requirement.value.trim()} ${name}`
      : name
  }
}

function onFileSelect(e) {
  addFiles(Array.from(e.target.files || []))
  e.target.value = ''
}

function onDrop(e) {
  addFiles(Array.from(e.dataTransfer.files || []))
}

function addFiles(newFiles) {
  const valid = newFiles.filter((f) => {
    const ext = f.name.split('.').pop()?.toLowerCase()
    return ['pdf', 'md', 'txt'].includes(ext) && f.size <= 50 * 1024 * 1024
  })
  files.value.push(...valid)
}

async function submit() {
  if (busy.value || !requirement.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    const res = await taskApi.create(requirement.value.trim(), files.value)
    const taskId = res?.data?.task_id
    if (!taskId) throw new Error('创建失败')
    router.push({ name: 'TaskConfirm', params: { taskId } })
  } catch (e) {
    error.value = e.message || '创建失败'
  } finally {
    busy.value = false
  }
}

function goTask(t) {
  const s = t.status
  if (s === 'awaiting_confirm') {
    router.push({ name: 'TaskConfirm', params: { taskId: t.task_id } })
  } else if (s === 'completed' || s === 'completed_partial') {
    router.push({ name: 'Report', params: { taskId: t.task_id } })
  } else if (s === 'failed') {
    router.push({ name: 'TaskRun', params: { taskId: t.task_id } })
  } else {
    router.push({ name: 'TaskRun', params: { taskId: t.task_id } })
  }
}

function statusLabel(s) {
  const map = {
    awaiting_confirm: '待确认',
    collecting: '采集中',
    analyzing: '分析中',
    completed: '已完成',
    completed_partial: '部分完成',
    failed: '失败',
  }
  return map[s] || s
}

function deliverableIcon(t) {
  const d = t.task_card?.deliverable
  return { summary: '📋', compare: '⚖', tracking: '📡' }[d] || '📄'
}

function formatDate(iso) {
  if (!iso) return ''
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<style scoped>
.home {
  max-width: 920px;
  margin: 0 auto;
}

.hero {
  margin-bottom: 20px;
}

.tagline {
  color: #4a6285;
  margin: 0 0 4px;
}

.hero h1 {
  margin: 0;
  font-size: 32px;
  font-weight: 700;
}

.panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 20px 22px;
  margin-bottom: 16px;
}

textarea {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #c5d2e5;
  background: #fff;
  padding: 12px;
  font: inherit;
  color: inherit;
  resize: vertical;
}

.chips, .prefill {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.chips-label {
  font-size: 13px;
  color: #6a7f9c;
}

.chip {
  border: 1px solid #c5d2e5;
  background: #fff;
  padding: 4px 12px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  color: #1a3a6b;
}

.chip:hover {
  border-color: #1a3a6b;
}

.chip.amber {
  border-color: #b8860b;
  color: #b8860b;
}

.upload-zone {
  margin-top: 14px;
  border: 1px dashed #c5d2e5;
  padding: 16px;
  text-align: center;
  font-size: 13px;
  color: #6a7f9c;
  cursor: pointer;
  background: #fafcfe;
}

.upload-zone.has-files {
  text-align: left;
}

.file-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.file-item {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}

.file-item button {
  background: none;
  border: none;
  cursor: pointer;
  color: #9aa8bc;
}

.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
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

.muted {
  color: #6a7f9c;
  font-size: 13px;
}

.err {
  color: #c0392b;
  margin-top: 8px;
  font-size: 14px;
}

.history h2 {
  margin: 0 0 14px;
  font-size: 20px;
}

.empty-state {
  text-align: center;
  padding: 32px;
  color: #4a6285;
}

.task-grid {
  display: grid;
  gap: 12px;
}

.task-card {
  padding: 14px 16px;
  border: 1px solid #d9e2ef;
  background: #fff;
  cursor: pointer;
  transition: border-color 0.15s;
}

.task-card:hover {
  border-color: #b8860b;
}

.task-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.status-badge {
  font-size: 12px;
  padding: 2px 8px;
  background: #e8eef5;
  color: #4a6285;
}

.status-badge.completed,
.status-badge.completed_partial {
  background: #e8f5e9;
  color: #2e7d52;
}

.status-badge.failed {
  background: #fdecea;
  color: #c0392b;
}

.task-req {
  margin: 0 0 6px;
  font-size: 14px;
  line-height: 1.5;
}

.task-meta {
  margin: 0;
  font-size: 12px;
  color: #9aa8bc;
}
</style>
