<template>
  <div class="profile-view">
    <header class="page-head">
      <h1>我的偏好</h1>
      <p class="muted">越用越默契 — 系统从你的反馈中学习</p>
    </header>

    <!-- 偏好标签 -->
    <section class="panel">
      <h2>偏好标签</h2>
      <div v-if="!preferences.length" class="empty">暂无偏好记录。提交报告反馈后将自动学习。</div>
      <div v-else class="tag-list">
        <span v-for="p in preferences" :key="p.key" class="pref-tag">
          {{ p.label || p.key }}: {{ formatPrefValue(p.value) }}
          <button @click="deletePref(p.key)">×</button>
        </span>
      </div>
    </section>

    <!-- 经验规则库 -->
    <section class="panel">
      <h2>经验规则库</h2>
      <table v-if="playbook.length" class="data-table">
        <thead>
          <tr>
            <th>规则</th>
            <th>来源</th>
            <th>状态</th>
            <th>命中</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in playbook" :key="r.id" :class="{ retired: r.status === 'retired' }">
            <td>{{ r.action || r.condition }}</td>
            <td>{{ (r.evidence_run_ids || []).length ? '任务' : '—' }}</td>
            <td><span class="status-badge" :class="r.status">{{ statusLabel(r.status) }}</span></td>
            <td>{{ r.hit_count ?? 0 }}</td>
            <td>
              <button v-if="r.status === 'candidate'" class="link" @click="confirmRule(r.id)">确认启用</button>
              <button v-if="r.status !== 'retired'" class="link danger" @click="retireRule(r.id)">退休</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无规则。完成多次任务后系统将提炼候选规则。</div>
    </section>

    <!-- 学习效果 -->
    <section class="panel">
      <h2>学习效果</h2>
      <div class="stats-cards">
        <div class="stat-card">
          <div class="stat-value">{{ stats.with_rules_avg_stars ?? '—' }}</div>
          <div class="stat-label">命中规则任务均星</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ stats.without_rules_avg_stars ?? '—' }}</div>
          <div class="stat-label">未命中任务均星</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ stats.active_rules ?? stats.total_rules ?? '—' }}</div>
          <div class="stat-label">活跃规则数</div>
        </div>
      </div>
    </section>

    <!-- 数据源健康度 -->
    <section class="panel">
      <h2>数据源健康度（近 7 日）</h2>
      <table v-if="sourceHealth.length" class="data-table">
        <thead>
          <tr>
            <th>工具</th>
            <th>成功率</th>
            <th>平均延迟</th>
            <th>降级率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in sourceHealth" :key="s.tool || s.tool_name">
            <td>{{ s.tool || s.tool_name }}</td>
            <td>
              <div class="progress-bar">
                <div class="fill" :style="{ width: (s.success_rate || 0) + '%' }" />
              </div>
              {{ s.success_rate ?? '—' }}%
            </td>
            <td>{{ s.avg_latency_ms ?? '—' }} ms</td>
            <td>{{ s.degraded_rate ?? '—' }}%</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无健康度数据</div>
    </section>

    <!-- 危险区 -->
    <section v-if="!isDemo" class="panel danger-zone">
      <h2>危险操作</h2>
      <p class="muted">清空后将删除全部偏好与记忆，不可恢复</p>
      <button class="btn danger" @click="clearMemory">清空全部记忆</button>
      <p v-if="clearMsg" class="msg">{{ clearMsg }}</p>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { memoryApi } from '../api/index.js'

const route = useRoute()
const isDemo = computed(() => route.query.demo === '1')

const preferences = ref([])
const playbook = ref([])
const stats = ref({})
const sourceHealth = ref([])
const clearMsg = ref('')

onMounted(loadAll)

async function loadAll() {
  await Promise.all([loadPrefs(), loadPlaybook(), loadStats(), loadHealth()])
}

async function loadPrefs() {
  try {
    const res = await memoryApi.preferences()
    const data = res?.data
    preferences.value = data?.preferences || data?.items || (Array.isArray(data) ? data : [])
  } catch {
    preferences.value = []
  }
}

async function loadPlaybook() {
  try {
    const res = await memoryApi.playbook()
    playbook.value = res?.data || []
  } catch {
    playbook.value = []
  }
}

async function loadStats() {
  try {
    const res = await memoryApi.stats()
    const data = res?.data || {}
    stats.value = {
      ...data,
      with_rules_avg_stars: data.with_rules_avg_stars ?? data.avg_stars_hit,
      without_rules_avg_stars: data.without_rules_avg_stars ?? data.avg_stars_all,
      active_rules: data.active_rules ?? data.by_status?.active,
      total_rules: data.total_rules ?? data.total,
    }
  } catch {
    stats.value = {}
  }
}

async function loadHealth() {
  try {
    const res = await memoryApi.sourceHealth(7)
    const data = res?.data
    if (Array.isArray(data)) {
      sourceHealth.value = data
    } else if (data && typeof data === 'object') {
      sourceHealth.value = Object.entries(data).map(([tool, row]) => ({
        tool,
        tool_name: row.tool_name || tool,
        success_rate: row.success_rate != null ? Math.round(row.success_rate * 1000) / 10 : null,
        avg_latency_ms: row.avg_latency_ms,
        degraded_rate: row.degraded_rate != null ? Math.round(row.degraded_rate * 1000) / 10 : null,
        samples: row.samples,
      }))
    } else {
      sourceHealth.value = []
    }
  } catch {
    sourceHealth.value = []
  }
}

async function deletePref(key) {
  try {
    await memoryApi.deletePreference(key)
    clearMsg.value = '已删除，7 天内不会重新学习该偏好'
    await loadPrefs()
  } catch (e) {
    clearMsg.value = e.message
  }
}

async function confirmRule(id) {
  try {
    await memoryApi.confirmPlaybook(Number(id))
    await loadPlaybook()
  } catch (e) {
    clearMsg.value = e.message || '确认失败'
  }
}

async function retireRule(id) {
  try {
    await memoryApi.deletePlaybook(Number(id))
    await loadPlaybook()
  } catch (e) {
    clearMsg.value = e.message || '退休失败'
  }
}

async function clearMemory() {
  if (!confirm('确定清空全部记忆？此操作不可撤销。')) return
  try {
    await memoryApi.deleteUser()
    clearMsg.value = '记忆已清空'
    await loadAll()
  } catch (e) {
    clearMsg.value = e.message
  }
}

function formatPrefValue(v) {
  if (typeof v === 'object') return JSON.stringify(v)
  return v
}

function statusLabel(s) {
  return { candidate: '候选', active: '生效', retired: '已退休' }[s] || s
}
</script>

<style scoped>
.profile-view {
  max-width: 920px;
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

.panel {
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
  padding: 20px 22px;
  margin-bottom: 16px;
}

.panel h2 {
  margin: 0 0 14px;
  font-size: 18px;
}

.empty {
  color: #9aa8bc;
  font-size: 14px;
  padding: 12px 0;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.pref-tag {
  background: #e8eef5;
  padding: 4px 10px;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.pref-tag button {
  background: none;
  border: none;
  cursor: pointer;
  color: #9aa8bc;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.data-table th,
.data-table td {
  text-align: left;
  padding: 10px 8px;
  border-bottom: 1px solid #eef3f8;
}

.data-table tr.retired td {
  text-decoration: line-through;
  color: #9aa8bc;
}

.status-badge {
  font-size: 12px;
  padding: 2px 8px;
  background: #e8eef5;
}

.status-badge.active {
  background: #e8f5e9;
  color: #2e7d52;
}

.status-badge.candidate {
  background: #f3f5f7;
  color: #6a7f9c;
}

.link {
  background: none;
  border: none;
  color: #1a3a6b;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  text-decoration: underline;
}

.link.danger {
  color: #c0392b;
}

.stats-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.stat-card {
  background: #fff;
  border: 1px solid #d9e2ef;
  padding: 16px;
  text-align: center;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #b8860b;
  font-variant-numeric: tabular-nums;
}

.stat-label {
  font-size: 13px;
  color: #6a7f9c;
  margin-top: 4px;
}

.progress-bar {
  height: 6px;
  background: #eef3f8;
  border-radius: 3px;
  margin-bottom: 4px;
  overflow: hidden;
}

.progress-bar .fill {
  height: 100%;
  background: #1a3a6b;
}

.danger-zone {
  border-color: #f5c6cb;
}

.btn.danger {
  background: #c0392b;
  color: #fff;
  border: none;
  padding: 10px 18px;
  font: inherit;
  cursor: pointer;
  margin-top: 8px;
}

.msg {
  margin-top: 10px;
  font-size: 13px;
  color: #2e7d52;
}

@media (max-width: 640px) {
  .stats-cards {
    grid-template-columns: 1fr;
  }
}
</style>
