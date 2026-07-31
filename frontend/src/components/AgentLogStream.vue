<template>
  <div class="agent-log-stream" ref="containerRef">
    <div v-if="lines.length === 0" class="empty">等待 Agent 动作…</div>
    <div
      v-for="(line, idx) in lines"
      :key="idx"
      class="log-line"
      :class="actionClass(line)"
    >
      <span class="agent-badge" :style="{ background: agentColor(line.agent) }">
        {{ agentLabel(line.agent) }}
      </span>
      <span class="elapsed">{{ formatElapsed(line.elapsed_seconds) }}</span>
      <div class="log-content">
        <template v-if="line.action === 'tool_call'">
          <strong>{{ line.details?.tool || line.details?.name || '工具调用' }}</strong>
          <span class="params">{{ summarizeParams(line.details) }}</span>
        </template>
        <template v-else-if="line.action === 'tool_result'">
          <details>
            <summary>工具结果</summary>
            <pre>{{ JSON.stringify(line.details, null, 2) }}</pre>
          </details>
        </template>
        <template v-else-if="line.action === 'react_thought'">
          <em>正在分析已冻结证据（原始思维过程不展示）</em>
        </template>
        <template v-else-if="line.action === 'error'">
          <span class="err-text">{{ line.details?.error || line.details?.message || '错误' }}</span>
        </template>
        <template v-else-if="line.action === 'revise'">
          <span class="revise-text">审校修订：{{ line.details?.issue || line.details?.message || '' }}</span>
        </template>
        <template v-else-if="line.action === 'claim' || line.action === 'claim_created'">
          <strong>提出观点</strong>：{{ line.details?.statement || line.details?.claim || line.details?.message || '' }}
        </template>
        <template v-else-if="line.action === 'challenge' || line.action === 'challenge_created'">
          <strong>提出反证</strong>：{{ line.details?.argument || line.details?.message || '' }}
        </template>
        <template v-else-if="line.action === 'withdraw' || line.action === 'claim_withdrawn'">
          <span class="withdraw-text">撤回观点：{{ line.details?.statement || line.details?.message || '' }}</span>
        </template>
        <template v-else-if="line.action === 'audit_failed'">
          <span class="err-text">证据审计未通过：{{ line.details?.reason || line.details?.message || '' }}</span>
        </template>
        <template v-else>
          {{ line.details?.message || line.action }}
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'

const props = defineProps({
  lines: { type: Array, default: () => [] },
  autoScroll: { type: Boolean, default: true },
})

const containerRef = ref(null)

const AGENT_COLORS = {
  collector_news: '#3d6ea8',
  collector_announcement: '#2e7d52',
  collector_financial: '#1a3a6b',
  analyst: '#b8860b',
  reviewer: '#c0392b',
  quality: '#445d48',
  quality_agent: '#445d48',
  growth: '#805a32',
  growth_agent: '#805a32',
  evidence_auditor: '#75507b',
  auditor: '#75507b',
  judge: '#1a3a6b',
  synthesizer: '#1a3a6b',
  system: '#6a7f9c',
}

watch(
  () => props.lines.length,
  async () => {
    if (!props.autoScroll) return
    await nextTick()
    const el = containerRef.value
    if (el) el.scrollTop = el.scrollHeight
  }
)

function agentColor(agent) {
  return AGENT_COLORS[agent] || '#4a6285'
}

function agentLabel(agent) {
  if (!agent) return '系统'
  const labels = {
    quality: '稳健/质量',
    quality_agent: '稳健/质量',
    growth: '成长/变化',
    growth_agent: '成长/变化',
    evidence_auditor: '证据审计',
    auditor: '证据审计',
    judge: '裁决',
    synthesizer: '裁决',
  }
  if (labels[agent]) return labels[agent]
  return agent.replace(/^collector_/, '').replace(/_/g, ' ')
}

function actionClass(line) {
  return {
    'is-tool-call': line.action === 'tool_call',
    'is-tool-result': line.action === 'tool_result',
    'is-thought': line.action === 'react_thought',
    'is-error': line.action === 'error',
    'is-revise': line.action === 'revise',
    'is-challenge': ['challenge', 'challenge_created'].includes(line.action),
    'is-withdraw': ['withdraw', 'claim_withdrawn'].includes(line.action),
  }
}

function formatElapsed(sec) {
  if (sec == null) return ''
  const s = Math.floor(sec)
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}:${String(s % 60).padStart(2, '0')}` : `${s}s`
}

function summarizeParams(details) {
  if (!details) return ''
  const params = details.params || details.arguments || details
  const str = typeof params === 'string' ? params : JSON.stringify(params)
  return str.length > 80 ? str.slice(0, 80) + '…' : str
}
</script>

<style scoped>
.agent-log-stream {
  height: 100%;
  overflow-y: auto;
  padding: 12px;
  background: rgba(255, 255, 255, 0.5);
  border: 1px solid rgba(26, 58, 107, 0.08);
}

.empty {
  color: #6a7f9c;
  font-size: 14px;
  padding: 24px;
  text-align: center;
}

.log-line {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: flex-start;
  padding: 8px 10px;
  margin-bottom: 6px;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.5;
}

.is-tool-call {
  background: #e8f0fa;
  border-left: 3px solid #3d6ea8;
}

.is-tool-result {
  background: #f3f5f7;
  border-left: 3px solid #9aa8bc;
}

.is-thought {
  background: #faf8f3;
  font-style: italic;
}

.is-error {
  background: #fdecea;
  border-left: 3px solid #c0392b;
}

.is-revise {
  background: #fff3e0;
  border-left: 3px solid #e67e22;
}

.is-challenge {
  background: #f3edf5;
  border-left: 3px solid #75507b;
}

.is-withdraw {
  background: #f6f0e9;
  border-left: 3px solid #805a32;
}

.agent-badge {
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  white-space: nowrap;
  text-transform: capitalize;
}

.elapsed {
  color: #9aa8bc;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  padding-top: 2px;
}

.log-content {
  flex: 1 1 100%;
  min-width: 0;
}

.params {
  display: block;
  color: #4a6285;
  font-size: 12px;
  margin-top: 2px;
}

.err-text {
  color: #c0392b;
}

.revise-text {
  color: #e67e22;
  font-weight: 500;
}

.withdraw-text {
  color: #805a32;
  text-decoration: line-through;
}

pre {
  font-size: 11px;
  overflow-x: auto;
  max-height: 120px;
  margin-top: 4px;
}
</style>
