<template>
  <div class="confirm-view">
    <header class="page-head">
      <h1>确认任务卡</h1>
      <p class="muted">请核对以下解析结果，确认后开始研究</p>
    </header>

    <div v-for="(c, i) in clarifications" :key="i" class="clarification">
      {{ c }}
    </div>

    <form v-if="form" class="panel" @submit.prevent="confirm">
      <div class="field">
        <label>交付物类型</label>
        <div class="radio-row">
          <label v-for="d in deliverables" :key="d.value">
            <input v-model="form.deliverable" type="radio" :value="d.value" />
            {{ d.label }}
          </label>
        </div>
      </div>

      <div v-if="supportsDebate" class="field">
        <label>分析方式</label>
        <div class="analysis-mode-grid">
          <label class="mode-card" :class="{ selected: form.analysis_mode === 'direct' }">
            <input v-model="form.analysis_mode" type="radio" value="direct" />
            <span>
              <strong>直接分析</strong>
              <small>沿用现有研究流程，适合快速形成报告。</small>
            </span>
          </label>
          <label class="mode-card" :class="{ selected: form.analysis_mode === 'evidence_debate' }">
            <input v-model="form.analysis_mode" type="radio" value="evidence_debate" />
            <span>
              <strong>多视角证据辩论</strong>
              <small>两种基本面视角互相质询，并由确定性审计器校验后裁决。</small>
            </span>
          </label>
        </div>
      </div>

      <div v-else-if="form.deliverable === 'tracking'" class="field mode-unavailable">
        追踪任务首版仅支持直接分析，不运行完整辩论。
      </div>

      <p class="provider-note">
        文本分析由 DeepSeek 处理；如任务含上传文件，其中的候选图片页可能发送给百炼 Qwen-VL 解析。界面不会展示模型的原始思维链。
      </p>

      <div class="field">
        <label>标的</label>
        <div v-for="(sym, idx) in form.symbols" :key="idx" class="symbol-row">
          <input v-model="sym.name" placeholder="名称" />
          <input v-model="sym.code" placeholder="6位代码" maxlength="6" />
          <button type="button" class="icon-btn" @click="form.symbols.splice(idx, 1)">×</button>
        </div>
        <button type="button" class="link-btn" @click="form.symbols.push({ name: '', code: '' })">
          + 添加标的
        </button>
        <p v-if="codeWarning" class="warn">{{ codeWarning }}</p>
      </div>

      <div class="field row-2">
        <div>
          <label>开始日期</label>
          <input v-model="form.time_window.start" type="date" />
        </div>
        <div>
          <label>结束日期</label>
          <input v-model="form.time_window.end" type="date" />
        </div>
      </div>

      <div class="field">
        <label>信息类型</label>
        <div class="checkbox-grid">
          <label v-for="t in infoTypes" :key="t.value">
            <input v-model="form.info_types" type="checkbox" :value="t.value" />
            {{ t.label }}
          </label>
        </div>
      </div>

      <div class="field">
        <label>关注点</label>
        <div class="tag-input">
          <span v-for="(fp, fi) in form.focus_points" :key="fi" class="tag">
            {{ fp }}
            <button type="button" @click="form.focus_points.splice(fi, 1)">×</button>
          </span>
          <input
            v-model="focusInput"
            placeholder="输入后回车添加"
            @keydown.enter.prevent="addFocus"
          />
        </div>
      </div>

      <p v-if="error" class="err">{{ error }}</p>

      <div class="actions">
        <button type="button" class="btn secondary" @click="goBack">返回修改需求</button>
        <button type="submit" class="btn" :disabled="busy">
          {{ busy ? '启动中…' : '开始研究' }}
        </button>
      </div>
    </form>

    <div v-else-if="loading" class="muted">加载任务卡…</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { taskApi } from '../api/index.js'

const route = useRoute()
const router = useRouter()
const taskId = route.params.taskId

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const form = ref(null)
const clarifications = ref([])
const focusInput = ref('')

const deliverables = [
  { value: 'summary', label: '摘要' },
  { value: 'compare', label: '对比' },
  { value: 'tracking', label: '追踪' },
]

const infoTypes = [
  { value: 'announcement', label: '公告' },
  { value: 'financial_report', label: '财报' },
  { value: 'news', label: '新闻' },
  { value: 'research_report', label: '研报' },
  { value: 'industry_data', label: '行业数据' },
]

const codeWarning = computed(() => {
  if (!form.value) return ''
  const bad = form.value.symbols.filter((s) => !s.code || !/^\d{6}$/.test(String(s.code)))
  if (bad.length) return '请为全部标的填写有效的 6 位股票代码'
  return ''
})

const supportsDebate = computed(() => ['summary', 'compare'].includes(form.value?.deliverable))

watch(
  () => form.value?.deliverable,
  (deliverable) => {
    if (deliverable === 'tracking' && form.value) form.value.analysis_mode = 'direct'
  }
)

onMounted(loadTask)

async function loadTask() {
  loading.value = true
  try {
    const res = await taskApi.get(taskId)
    const data = res?.data
    const card = data?.task_card || {}
    form.value = {
      deliverable: card.deliverable || 'summary',
      analysis_mode: card.analysis_mode || 'direct',
      symbols: (card.symbols || []).map((s) => ({ name: s.name || '', code: s.code || '' })),
      time_window: { start: card.time_window?.start || '', end: card.time_window?.end || '' },
      info_types: [...(card.info_types || ['announcement', 'financial_report', 'news'])],
      focus_points: [...(card.focus_points || [])],
      compare_dimensions: [...(card.compare_dimensions || [])],
      output_language_style: card.output_language_style || 'professional_brief',
      clarifications: card.clarifications || [],
    }
    clarifications.value = card.clarifications || data?.clarifications || []
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function addFocus() {
  const v = focusInput.value.trim()
  if (v && !form.value.focus_points.includes(v)) {
    form.value.focus_points.push(v)
  }
  focusInput.value = ''
}

async function confirm() {
  if (codeWarning.value) {
    error.value = codeWarning.value
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await taskApi.confirm(taskId, form.value)
    const runId = result?.data?.run_id || result?.run_id
    router.push({
      name: 'TaskRun',
      params: { taskId },
      query: runId ? { run_id: runId } : {},
    })
  } catch (e) {
    error.value = e.message || '确认失败'
  } finally {
    busy.value = false
  }
}

function goBack() {
  router.push({ name: 'Home' })
}
</script>

<style scoped>
.confirm-view {
  max-width: 720px;
  margin: 0 auto;
}

.page-head h1 {
  margin: 0 0 6px;
  font-size: 28px;
}

.muted {
  color: #6a7f9c;
  font-size: 14px;
}

.clarification {
  background: #fff8e7;
  border-left: 3px solid #b8860b;
  padding: 10px 14px;
  margin-bottom: 10px;
  font-size: 14px;
  color: #5a4a20;
}

.panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 22px;
  margin-top: 16px;
}

.field {
  margin-bottom: 20px;
}

.field label {
  display: block;
  font-size: 13px;
  color: #4a6285;
  margin-bottom: 8px;
  font-weight: 600;
}

.radio-row, .checkbox-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}

.radio-row label, .checkbox-grid label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: normal;
  cursor: pointer;
}

.analysis-mode-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.field .mode-card {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-height: 78px;
  padding: 12px;
  margin: 0;
  border: 1px solid #c5d2e5;
  background: rgba(255, 255, 255, 0.74);
  cursor: pointer;
  font-weight: normal;
}

.field .mode-card.selected {
  border-color: #1a3a6b;
  box-shadow: inset 0 0 0 1px #1a3a6b;
}

.mode-card input {
  margin-top: 3px;
}

.mode-card strong,
.mode-card small {
  display: block;
}

.mode-card strong {
  color: #1a3a6b;
  margin-bottom: 4px;
}

.mode-card small,
.provider-note,
.mode-unavailable {
  color: #6a7f9c;
  font-size: 12px;
  line-height: 1.6;
}

.provider-note {
  margin: 8px 0 0;
}

.mode-unavailable {
  padding: 10px 12px;
  background: #f3f5f7;
  border-left: 3px solid #9aa8bc;
}

.symbol-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.symbol-row input {
  flex: 1;
  border: 1px solid #c5d2e5;
  padding: 8px 10px;
  font: inherit;
}

.row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

input[type="date"] {
  width: 100%;
  border: 1px solid #c5d2e5;
  padding: 8px 10px;
  font: inherit;
}

.tag-input {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  border: 1px solid #c5d2e5;
  padding: 8px;
  background: #fff;
}

.tag {
  background: #e8eef5;
  padding: 2px 8px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.tag button {
  background: none;
  border: none;
  cursor: pointer;
}

.tag-input input {
  border: none;
  outline: none;
  flex: 1;
  min-width: 120px;
  font: inherit;
}

.link-btn, .icon-btn {
  background: none;
  border: none;
  color: #1a3a6b;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}

.warn {
  color: #b8860b;
  font-size: 13px;
  margin-top: 6px;
}

.actions {
  display: flex;
  gap: 12px;
  margin-top: 8px;
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

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.err {
  color: #c0392b;
  font-size: 14px;
}

@media (max-width: 640px) {
  .analysis-mode-grid,
  .row-2 {
    grid-template-columns: 1fr;
  }
}
</style>
