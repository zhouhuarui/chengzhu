<template>
  <div class="feedback-bar">
    <span class="label">本章有帮助吗？</span>
    <button
      class="vote-btn"
      :class="{ active: vote === 'up' }"
      :disabled="submitted"
      @click="submit('up')"
    >👍</button>
    <button
      class="vote-btn"
      :class="{ active: vote === 'down' }"
      :disabled="submitted"
      @click="submit('down')"
    >👎</button>
    <input
      v-if="!submitted"
      v-model="comment"
      class="comment-input"
      placeholder="补充评语（可选）"
      @keyup.enter="submit(vote || 'up')"
    />
    <span v-if="submitted" class="done-msg">已记录，系统将学习你的偏好</span>
    <span v-if="error" class="err">{{ error }}</span>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { feedbackApi } from '../api/index.js'

const props = defineProps({
  taskId: { type: String, required: true },
  runId: { type: String, default: '' },
  sectionIndex: { type: Number, required: true },
  initialVote: { type: String, default: '' },
})

const vote = ref(props.initialVote)
const comment = ref('')
const submitted = ref(!!props.initialVote)
const error = ref('')
const loading = ref(false)

async function submit(v) {
  if (loading.value) return
  vote.value = v
  loading.value = true
  error.value = ''
  try {
    await feedbackApi.section(props.taskId, props.sectionIndex, v, comment.value, props.runId)
    submitted.value = true
  } catch (e) {
    error.value = e.message || '提交失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.feedback-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  padding: 10px 12px;
  background: #f3f7fc;
  border: 1px solid #d9e2ef;
  font-size: 13px;
}

.label {
  color: #4a6285;
}

.vote-btn {
  background: #fff;
  border: 1px solid #c5d2e5;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 16px;
}

.vote-btn.active {
  border-color: #b8860b;
  background: #fff8e7;
}

.vote-btn:disabled:not(.active) {
  opacity: 0.5;
  cursor: not-allowed;
}

.comment-input {
  flex: 1;
  min-width: 160px;
  border: 1px solid #c5d2e5;
  padding: 4px 8px;
  font: inherit;
  font-size: 13px;
}

.done-msg {
  color: #2e7d52;
  font-size: 12px;
}

.err {
  color: #c0392b;
  font-size: 12px;
}
</style>
