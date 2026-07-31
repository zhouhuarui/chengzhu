<template>
  <div class="tracking-view">
    <header class="page-head">
      <h1>追踪中心</h1>
      <p class="muted">管理订阅任务，查看定期简报</p>
    </header>

    <div class="layout">
      <aside class="sub-list">
        <div v-if="loading" class="muted">加载中…</div>
        <div v-else-if="!subs.length" class="empty">
          <p>暂无追踪订阅</p>
          <p class="muted">在报告页点击「开启追踪」创建订阅</p>
        </div>
        <article
          v-for="s in subs"
          :key="s.sub_id"
          class="sub-card"
          :class="{ active: selectedId === s.sub_id }"
          @click="selectSub(s.sub_id)"
        >
          <div class="sub-head">
            <span class="status" :class="s.status">{{ s.status === 'paused' ? '已暂停' : '运行中' }}</span>
            <span class="cron">{{ cronLabel(s.cron) }} {{ s.hour }}:00</span>
          </div>
          <p class="sub-title">{{ s.task_title || s.task_id }}</p>
          <div class="sub-actions" @click.stop>
            <button v-if="s.status === 'paused'" @click="resumeSub(s.sub_id)">恢复</button>
            <button v-else @click="pauseSub(s.sub_id)">暂停</button>
            <button @click="runNow(s.sub_id)">立即重跑</button>
            <button class="danger" @click="deleteSub(s.sub_id)">删除</button>
          </div>
        </article>
      </aside>

      <section class="brief-panel">
        <h2 v-if="selectedId">简报时间线</h2>
        <div v-if="!selectedId" class="empty">← 选择订阅查看简报</div>
        <div v-else-if="briefsLoading" class="muted">加载简报…</div>
        <div v-else-if="!briefs.length" class="empty">暂无简报</div>
        <article v-for="b in briefs" :key="b.brief_id || b.date" class="brief-card">
          <header class="brief-head">
            <span class="date">{{ b.date }}</span>
            <span v-if="factCount(b, 'new') != null" class="badge new">+{{ factCount(b, 'new') }} 新增</span>
            <span v-if="factCount(b, 'changed')" class="badge changed">{{ factCount(b, 'changed') }} 变更</span>
          </header>
          <details>
            <summary>{{ b.title || '查看简报' }}</summary>
            <div class="brief-md markdown-body" v-html="renderBrief(b.markdown)" />
          </details>
        </article>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import 'github-markdown-css/github-markdown.css'
import { trackingApi } from '../api/index.js'

const subs = ref([])
const selectedId = ref('')
const briefs = ref([])
const loading = ref(true)
const briefsLoading = ref(false)

onMounted(loadSubs)

async function loadSubs() {
  loading.value = true
  try {
    const res = await trackingApi.list()
    subs.value = res?.data || []
    if (subs.value.length && !selectedId.value) {
      selectSub(subs.value[0].sub_id)
    }
  } catch {
    subs.value = []
  } finally {
    loading.value = false
  }
}

async function selectSub(id) {
  selectedId.value = id
  briefsLoading.value = true
  try {
    const res = await trackingApi.briefs(id)
    briefs.value = (res?.data || []).sort((a, b) => (b.date || '').localeCompare(a.date || ''))
  } catch {
    briefs.value = []
  } finally {
    briefsLoading.value = false
  }
}

async function pauseSub(id) {
  await trackingApi.pause(id)
  await loadSubs()
}

async function resumeSub(id) {
  await trackingApi.resume(id)
  await loadSubs()
}

async function runNow(id) {
  await trackingApi.runNow(id)
  if (selectedId.value === id) selectSub(id)
}

async function deleteSub(id) {
  if (!confirm('确定删除此订阅？')) return
  await trackingApi.delete(id)
  if (selectedId.value === id) {
    selectedId.value = ''
    briefs.value = []
  }
  await loadSubs()
}

function cronLabel(c) {
  return c === 'daily' ? '每日' : '每周'
}

function factCount(brief, kind) {
  return brief?.[`${kind}_facts_count`] ?? brief?.[`${kind}_facts`]
}

function renderBrief(md) {
  if (!md) return ''
  return DOMPurify.sanitize(marked.parse(md, { gfm: true }))
}
</script>

<style scoped>
.tracking-view {
  max-width: 1100px;
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

.layout {
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 16px;
  margin-top: 20px;
  min-height: 480px;
}

.sub-list, .brief-panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 16px;
}

.sub-card {
  padding: 12px;
  border: 1px solid #d9e2ef;
  margin-bottom: 10px;
  cursor: pointer;
  background: #fff;
}

.sub-card.active {
  border-color: #b8860b;
  background: #fffbf0;
}

.sub-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  margin-bottom: 6px;
}

.status {
  background: #e8f5e9;
  color: #2e7d52;
  padding: 1px 6px;
}

.status.paused {
  background: #f3f5f7;
  color: #6a7f9c;
}

.sub-title {
  margin: 0 0 8px;
  font-size: 14px;
}

.sub-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sub-actions button {
  font-size: 12px;
  padding: 3px 8px;
  border: 1px solid #c5d2e5;
  background: #fff;
  cursor: pointer;
  font: inherit;
}

.sub-actions .danger {
  color: #c0392b;
  border-color: #f5c6cb;
}

.brief-panel h2 {
  margin: 0 0 14px;
  font-size: 18px;
}

.empty {
  color: #6a7f9c;
  text-align: center;
  padding: 40px 20px;
  font-size: 14px;
}

.brief-card {
  border: 1px solid #d9e2ef;
  padding: 12px 14px;
  margin-bottom: 10px;
  background: #fff;
}

.brief-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.date {
  font-weight: 600;
  font-size: 14px;
}

.badge {
  font-size: 11px;
  padding: 2px 6px;
}

.badge.new {
  background: #e8f5e9;
  color: #2e7d52;
}

.badge.changed {
  background: #fff3e0;
  color: #e67e22;
}

.brief-md {
  margin-top: 10px;
  font-size: 14px;
}

@media (max-width: 768px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
