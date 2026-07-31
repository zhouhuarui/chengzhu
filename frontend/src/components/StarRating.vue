<template>
  <div class="star-rating">
    <span class="label">{{ label }}</span>
    <button
      v-for="n in 5"
      :key="n"
      class="star"
      :class="{ filled: n <= (hover || value) }"
      :disabled="submitted"
      @mouseenter="hover = n"
      @mouseleave="hover = 0"
      @click="submit(n)"
    >★</button>
    <span v-if="submitted" class="done-msg">感谢评分！</span>
    <span v-if="error" class="err">{{ error }}</span>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { feedbackApi } from '../api/index.js'

const props = defineProps({
  taskId: { type: String, required: true },
  runId: { type: String, default: '' },
  label: { type: String, default: '报告整体评分' },
  initialStars: { type: Number, default: 0 },
})

const value = ref(props.initialStars)
const hover = ref(0)
const submitted = ref(props.initialStars > 0)
const error = ref('')
const loading = ref(false)

async function submit(stars) {
  if (loading.value || submitted.value) return
  loading.value = true
  error.value = ''
  try {
    await feedbackApi.report(props.taskId, stars, '', props.runId)
    value.value = stars
    submitted.value = true
  } catch (e) {
    error.value = e.message || '提交失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.star-rating {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 24px 0;
  padding: 16px;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(26, 58, 107, 0.08);
}

.label {
  margin-right: 8px;
  color: #4a6285;
  font-size: 14px;
}

.star {
  background: none;
  border: none;
  font-size: 24px;
  color: #c5d2e5;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
}

.star.filled {
  color: #b8860b;
}

.star:disabled {
  cursor: default;
}

.done-msg {
  margin-left: 12px;
  color: #2e7d52;
  font-size: 13px;
}

.err {
  margin-left: 8px;
  color: #c0392b;
  font-size: 12px;
}
</style>
