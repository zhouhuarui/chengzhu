<template>
  <div v-if="visible" class="dialog-overlay" @click.self="close">
    <div class="dialog">
      <h3>开启追踪订阅</h3>
      <p class="hint">定期自动重跑任务，推送变更简报</p>
      <div class="field">
        <label>频率</label>
        <div class="radio-group">
          <label><input v-model="cron" type="radio" value="daily" /> 每日</label>
          <label><input v-model="cron" type="radio" value="weekly" /> 每周</label>
        </div>
      </div>
      <div class="field">
        <label>推送时间（小时）</label>
        <input v-model.number="hour" type="range" min="6" max="22" />
        <span>{{ hour }}:00</span>
      </div>
      <p v-if="error" class="err">{{ error }}</p>
      <div class="actions">
        <button class="btn secondary" @click="close">取消</button>
        <button class="btn" :disabled="loading" @click="submit">
          {{ loading ? '提交中…' : '确认订阅' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { trackingApi } from '../api/index.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
  taskId: { type: String, required: true },
})

const emit = defineEmits(['close', 'subscribed'])

const cron = ref('weekly')
const hour = ref(8)
const loading = ref(false)
const error = ref('')

watch(() => props.visible, (v) => {
  if (v) {
    error.value = ''
    cron.value = 'weekly'
    hour.value = 8
  }
})

function close() {
  emit('close')
}

async function submit() {
  loading.value = true
  error.value = ''
  try {
    const res = await trackingApi.subscribe(props.taskId, cron.value, hour.value)
    emit('subscribed', res?.data)
    close()
  } catch (e) {
    error.value = e.message || '订阅失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.dialog-overlay {
  position: fixed;
  inset: 0;
  background: rgba(26, 58, 107, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.dialog {
  background: #fff;
  padding: 24px 28px;
  width: 360px;
  max-width: 90vw;
  border: 1px solid #c5d2e5;
  box-shadow: 0 12px 40px rgba(26, 58, 107, 0.2);
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
  color: #1a3a6b;
}

.dialog h3 {
  margin: 0 0 8px;
}

.hint {
  color: #6a7f9c;
  font-size: 13px;
  margin: 0 0 16px;
}

.field {
  margin-bottom: 16px;
}

.field label {
  display: block;
  font-size: 13px;
  margin-bottom: 6px;
  color: #4a6285;
}

.radio-group {
  display: flex;
  gap: 16px;
}

.radio-group label {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}

.btn {
  border: none;
  background: #1a3a6b;
  color: #fff;
  padding: 8px 16px;
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
  font-size: 13px;
}
</style>
