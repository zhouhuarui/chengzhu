<template>
  <div class="pipeline-steps">
    <div
      v-for="(step, idx) in steps"
      :key="step.key"
      class="step"
      :class="stepClass(step, idx)"
    >
      <div class="step-marker">
        <span class="dot">{{ stepIcon(step, idx) }}</span>
        <span v-if="idx < steps.length - 1" class="line" />
      </div>
      <div class="step-body">
        <div class="step-title">{{ step.label }}</div>
        <div v-if="step.key === currentStage && progress != null" class="step-progress">
          {{ progress }}%
        </div>

        <!-- 采集子行 -->
        <div v-if="step.key === 'collecting' && expandedCollecting" class="sub-rows">
          <div v-for="c in collectors" :key="c.name" class="sub-row">
            <span class="sub-icon">{{ collectorIcon(c.state) }}</span>
            <span class="sub-name">{{ c.label }}</span>
            <span class="sub-count">{{ c.cards ?? 0 }} 条</span>
          </div>
        </div>

        <!-- 分析章节 -->
        <div v-if="step.key === 'analyzing' && expandedAnalyzing" class="sub-rows">
          <div v-for="(s, si) in sections" :key="si" class="sub-row">
            <span class="sub-icon">{{ sectionIcon(s.state) }}</span>
            <span class="sub-name">{{ s.title }}</span>
          </div>
        </div>

        <div v-if="step.key === 'debating' && debateDetail" class="sub-rows debate-summary">
          <div class="sub-row">
            <span class="sub-icon">{{ debateDetail.current_round ? `R${debateDetail.current_round}` : '·' }}</span>
            <span class="sub-name">{{ roleLabel(debateDetail.current_role || debateDetail.role) }}</span>
          </div>
          <div class="debate-counts">
            <span>观点 {{ debateDetail.claim_count ?? debateDetail.claims ?? 0 }}</span>
            <span>反证 {{ debateDetail.challenge_count ?? debateDetail.challenges ?? 0 }}</span>
            <span>撤回 {{ debateDetail.withdrawn_count ?? debateDetail.withdrawn ?? 0 }}</span>
            <span>审计失败 {{ debateDetail.audit_failure_count ?? debateDetail.audit_failed_count ?? debateDetail.audit_failures ?? 0 }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, default: '' },
  progress: { type: Number, default: 0 },
  progressDetail: { type: Object, default: () => ({}) },
})

const BASE_STAGES = [
  { key: 'parsing', label: '解析' },
  { key: 'collecting', label: '采集' },
  { key: 'ingesting', label: '建图' },
  { key: 'normalizing', label: '标准化' },
  { key: 'debating', label: '辩论', debateOnly: true },
  { key: 'adjudicating', label: '裁决', debateOnly: true },
  { key: 'analyzing', label: '分析' },
  { key: 'reviewing', label: '审校' },
  { key: 'assembling', label: '装配' },
]

const COLLECTOR_LABELS = {
  announcement: '公告采集',
  financial_report: '财报采集',
  news: '新闻采集',
  research_report: '研报采集',
  industry_data: '行业数据',
}

const debateEnabled = computed(() => {
  const detail = props.progressDetail || {}
  return detail.analysis_mode === 'evidence_debate' || Boolean(detail.debate)
})

const steps = computed(() => BASE_STAGES.filter((stage) => !stage.debateOnly || debateEnabled.value))

const currentStage = computed(() => props.progressDetail?.stage || props.status || 'parsing')

const currentIdx = computed(() => {
  const idx = steps.value.findIndex((s) => s.key === currentStage.value)
  return idx >= 0 ? idx : 0
})

const expandedCollecting = computed(() => currentIdx.value >= 1)
const expandedAnalyzing = computed(() => {
  const analyzingIdx = steps.value.findIndex((step) => step.key === 'analyzing')
  return currentIdx.value >= analyzingIdx
})

const debateDetail = computed(() => props.progressDetail?.debate || null)

const collectors = computed(() => {
  const raw = props.progressDetail?.collectors || {}
  return Object.entries(raw).map(([name, info]) => ({
    name,
    label: COLLECTOR_LABELS[name] || name,
    state: info?.state || 'pending',
    cards: info?.cards ?? 0,
  }))
})

const sections = computed(() => props.progressDetail?.sections || [])

function stepClass(step, idx) {
  if (props.status === 'completed' || props.status === 'completed_partial') return 'done'
  if (idx < currentIdx.value) return 'done'
  if (idx === currentIdx.value) return 'active'
  return 'pending'
}

function stepIcon(step, idx) {
  const cls = stepClass(step, idx)
  if (cls === 'done') return '✓'
  if (cls === 'active') return '●'
  return '○'
}

function collectorIcon(state) {
  if (state === 'done' || state === 'completed') return '✓'
  if (state === 'running' || state === 'active') return '◌'
  if (state === 'failed' || state === 'error') return '✗'
  return '·'
}

function sectionIcon(state) {
  return collectorIcon(state)
}

function roleLabel(role) {
  const labels = {
    quality: '稳健与质量视角',
    quality_agent: '稳健与质量视角',
    growth: '成长与变化视角',
    growth_agent: '成长与变化视角',
    evidence_auditor: '证据审计',
    auditor: '证据审计',
    judge: '裁决综合',
    synthesizer: '裁决综合',
  }
  return labels[role] || role || '准备辩论'
}
</script>

<style scoped>
.pipeline-steps {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.step {
  display: flex;
  gap: 12px;
  min-height: 48px;
}

.step-marker {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 24px;
}

.dot {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  background: #e8eef5;
  color: #6a7f9c;
  flex-shrink: 0;
}

.step.active .dot {
  background: #1a3a6b;
  color: #fff;
}

.step.done .dot {
  background: #b8860b;
  color: #fff;
}

.line {
  flex: 1;
  width: 2px;
  background: #c5d2e5;
  min-height: 16px;
}

.step.done .line {
  background: #b8860b;
}

.step-body {
  flex: 1;
  padding-bottom: 12px;
}

.step-title {
  font-weight: 600;
  font-size: 15px;
  padding-top: 2px;
}

.step.active .step-title {
  color: #1a3a6b;
}

.step-progress {
  font-size: 13px;
  color: #b8860b;
  margin-top: 2px;
}

.sub-rows {
  margin-top: 8px;
  padding-left: 4px;
}

.sub-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #4a6285;
  padding: 3px 0;
}

.sub-icon {
  width: 16px;
  text-align: center;
}

.sub-name {
  flex: 1;
}

.sub-count {
  color: #b8860b;
  font-variant-numeric: tabular-nums;
}

.debate-summary .sub-icon {
  width: 26px;
  color: #b8860b;
  font-weight: 700;
}

.debate-counts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 8px;
  color: #6a7f9c;
  font-size: 12px;
  padding: 3px 0 0 34px;
}
</style>
