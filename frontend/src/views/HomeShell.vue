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
      <section class="card">
        <h2>Phase 0 骨架就绪</h2>
        <p>后端健康检查：<strong :class="healthOk ? 'ok' : 'bad'">{{ healthText }}</strong></p>
        <p class="muted">产品文档见 <code>docs/product/</code>。下一步：数据工具层 / 图谱层 / 多 Agent 编排。</p>
        <button class="btn" @click="ping">重新检测</button>
      </section>

      <section class="card disclaimer">
        <p>{{ disclaimer }}</p>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import axios from 'axios'

const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5001'
const healthText = ref('检测中…')
const healthOk = ref(false)
const disclaimer = ref('本系统仅做公开信息整理与情景观察，不构成投资建议。')

async function ping() {
  healthText.value = '检测中…'
  healthOk.value = false
  try {
    const { data } = await axios.get(`${API}/api/health`, { timeout: 5000 })
    healthOk.value = data?.status === 'ok'
    healthText.value = healthOk.value ? `OK · ${data.service}` : JSON.stringify(data)
  } catch (e) {
    healthText.value = `失败：${e.message}`
  }
  try {
    const { data } = await axios.get(`${API}/api/meta/disclaimer`, { timeout: 5000 })
    if (data?.success && data.data?.disclaimer) {
      disclaimer.value = data.data.disclaimer
    }
  } catch (_) {
    /* ignore */
  }
}

onMounted(ping)
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
  padding: 48px 32px 16px;
  max-width: 880px;
  margin: 0 auto;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 14px;
}
.brand h1 {
  margin: 0;
  font-size: 56px;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.en {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 22px;
  color: #b8860b;
}
.tagline {
  margin: 8px 0 0;
  color: #4a6285;
  font-size: 16px;
}
.main {
  max-width: 880px;
  margin: 0 auto;
  padding: 16px 32px 64px;
  display: grid;
  gap: 16px;
}
.card {
  background: rgba(255, 255, 255, 0.78);
  border: 1px solid #d5dfec;
  padding: 20px 22px;
}
.card h2 {
  margin: 0 0 12px;
  font-size: 22px;
}
.muted {
  color: #5a6f8c;
  font-size: 14px;
}
.ok { color: #1f7a3a; }
.bad { color: #a33; }
.btn {
  margin-top: 12px;
  border: 1px solid #1a3a6b;
  background: #1a3a6b;
  color: #fff;
  padding: 8px 16px;
  cursor: pointer;
  font: inherit;
}
.disclaimer {
  font-size: 13px;
  line-height: 1.65;
  color: #5a6f8c;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #e8eef7;
  padding: 1px 6px;
}
</style>
