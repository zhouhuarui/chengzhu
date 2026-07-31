<template>
  <section class="debate-panel">
    <div v-if="!payload || (!claims.length && !challenges.length && !verdict)" class="empty">
      {{ loading ? '正在读取辩论记录…' : '当前 run 尚无辩论记录。' }}
    </div>

    <template v-else>
      <header class="debate-head">
        <div>
          <span class="eyebrow">EVIDENCE DEBATE</span>
          <h3>{{ statusLabel }}</h3>
        </div>
        <div class="summary-counts">
          <span><b>{{ claims.length }}</b> 观点</span>
          <span><b>{{ challenges.length }}</b> 反证</span>
          <span><b>{{ withdrawnCount }}</b> 撤回</span>
          <span><b>{{ auditFailureCount }}</b> 审计失败</span>
        </div>
      </header>

      <div v-if="currentRole || currentRound" class="current-turn">
        第 {{ currentRound || '–' }} 回合 · {{ roleLabel(currentRole) }}
      </div>

      <div class="columns">
        <section>
          <h4>观点流水</h4>
          <article v-for="claim in claims" :key="claimKey(claim)" class="card claim-card">
            <div class="card-meta">
              <span class="role" :class="roleClass(claim.role)">{{ roleLabel(claim.role) }}</span>
              <span>R{{ claim.round ?? claim.round_number ?? '–' }}</span>
              <span class="state" :class="statusClass(claim.status)">{{ claimStatus(claim.status) }}</span>
            </div>
            <p>{{ claim.assertion || claim.statement || claim.claim || claim.text || '（无观点文本）' }}</p>
            <div v-if="claim.assumptions?.length" class="assumptions">
              假设：{{ toText(claim.assumptions) }}
            </div>
            <div class="references">
              <button
                v-for="ref in evidenceRefs(claim)"
                :key="ref"
                type="button"
                @click="$emit('evidence', ref)"
              >
                {{ displayRef(ref) }}
              </button>
            </div>
          </article>
        </section>

        <section>
          <h4>质询与反证</h4>
          <article
            v-for="item in challenges"
            :key="challengeKey(item)"
            class="card challenge-card"
            :class="{ 'audit-failed': challengeAuditFailed(item) }"
          >
            <div class="card-meta">
              <span>{{ item.challenge_type || item.type || '反证' }}</span>
              <span>→ {{ item.target_claim_id || item.target_claim || '观点' }}</span>
              <span class="state" :class="{ failed: challengeAuditFailed(item) }">
                {{ challengeStateLabel(item) }}
              </span>
            </div>
            <p>{{ item.argument || item.statement || item.text || '（无反证文本）' }}</p>
            <div v-if="item.response" class="response">回应：{{ item.response }}</div>
            <div v-if="challengeAuditFailed(item)" class="challenge-audit-warning">
              <strong>审计失败：该反证不参与裁决</strong>
              <div v-for="issue in challengeAuditIssues(item)" :key="issue" class="audit-issue">
                问题码：<code>{{ issue }}</code>
              </div>
            </div>
            <div class="references">
              <button
                v-for="ref in evidenceRefs(item)"
                :key="ref"
                type="button"
                @click="$emit('evidence', ref)"
              >
                {{ displayRef(ref) }}
              </button>
            </div>
          </article>
        </section>
      </div>

      <section v-if="verdict" class="verdict">
        <h4>裁决结果</h4>
        <div class="verdict-grid">
          <div v-for="section in verdictSections" :key="section.key" class="verdict-section">
            <h5>{{ section.label }}</h5>
            <ul v-if="section.items.length">
              <li v-for="(item, index) in section.items" :key="index">
                {{ verdictText(item) }}
                <button
                  v-for="ref in evidenceRefs(item)"
                  :key="ref"
                  class="inline-ref"
                  type="button"
                  @click="$emit('evidence', ref)"
                >{{ displayRef(ref) }}</button>
              </li>
            </ul>
            <p v-else>暂无</p>
          </div>
        </div>
      </section>
    </template>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  debate: { type: Object, default: null },
  loading: { type: Boolean, default: false },
})

defineEmits(['evidence'])

const payload = computed(() => props.debate?.data || props.debate)
const claims = computed(() => asArray(payload.value?.claims || payload.value?.claim_cards))
const challenges = computed(() => asArray(payload.value?.challenges))
const rawAudits = computed(() => asArray(payload.value?.audit || payload.value?.audits || payload.value?.audit_results))
const claimAudits = computed(() => rawAudits.value.filter((item) => (
  item?.audit_type !== 'challenge' && !item?.challenge_id
)))
const challengeAudits = computed(() => uniqueAudits([
  ...rawAudits.value.filter((item) => item?.audit_type === 'challenge' || item?.challenge_id),
  ...asArray(payload.value?.challenge_audit || payload.value?.challenge_audits),
], 'challenge'))
const challengeAuditById = computed(() => Object.fromEntries(
  challengeAudits.value
    .filter((item) => item?.challenge_id)
    .map((item) => [String(item.challenge_id), item])
))
const verdict = computed(() => payload.value?.verdict || payload.value?.debate_verdict || null)
const state = computed(() => payload.value?.progress || payload.value?.state || payload.value?.manifest || {})
const currentRound = computed(() => state.value?.current_round || payload.value?.current_round)
const currentRole = computed(() => state.value?.current_role || state.value?.role || payload.value?.current_role)
const withdrawnCount = computed(() => claims.value.filter((item) => ['withdrawn', 'retracted'].includes(item.status)).length)
const auditFailureCount = computed(() => (
  uniqueAudits(claimAudits.value, 'claim').filter(auditFailed).length +
  challengeAudits.value.filter(auditFailed).length
))

const statusLabel = computed(() => {
  const status = payload.value?.status || state.value?.status
  const labels = {
    pending: '等待开始', running: '辩论进行中', debating: '辩论进行中',
    adjudicating: '证据裁决中', completed: '辩论已完成', failed: '辩论未完成',
    fallback_direct: '辩论未完成，已降级直接分析',
  }
  return labels[status] || (verdict.value ? '辩论已完成' : '辩论记录')
})

const verdictSections = computed(() => {
  const value = verdict.value || {}
  return [
    { key: 'consensus_facts', label: '共识事实', items: asArray(value.consensus_facts) },
    { key: 'supported_interpretations', label: '有证据支持的解释', items: asArray(value.supported_interpretations || value.interpretations) },
    { key: 'unresolved_disagreements', label: '未决分歧', items: asArray(value.unresolved_disagreements || value.unresolved_disputes || value.disagreements) },
    { key: 'withdrawn_claims', label: '撤回观点', items: asArray(value.withdrawn_claims) },
    { key: 'evidence_gaps', label: '证据不足', items: asArray(value.evidence_gaps) },
    { key: 'follow_up_public_events', label: '后续公开事项', items: asArray(value.follow_up_public_events || value.follow_up_public_items || value.follow_ups) },
  ]
})

function asArray(value) {
  if (Array.isArray(value)) return value
  if (value == null || value === '') return []
  return [value]
}

function uniqueAudits(items, type) {
  const unique = new Map()
  asArray(items).forEach((item, index) => {
    if (!item || typeof item !== 'object') return
    const id = type === 'challenge' ? item.challenge_id : item.claim_id
    unique.set(`${type}:${id || index}`, item)
  })
  return [...unique.values()]
}

function auditIssues(audit) {
  if (!audit || typeof audit !== 'object') return []
  return asArray(audit.issues || audit.failures || audit.hard_failures || audit.error_codes)
    .map((issue) => String(issue))
    .filter(Boolean)
}

function auditFailed(audit) {
  if (!audit || typeof audit !== 'object') return false
  return audit.hard_pass === false || audit.passed === false || auditIssues(audit).length > 0
}

function challengeAudit(item) {
  const id = item?.challenge_id || item?.id
  return id ? challengeAuditById.value[String(id)] : null
}

function challengeAuditFailed(item) {
  return auditFailed(challengeAudit(item))
}

function challengeAuditIssues(item) {
  return auditIssues(challengeAudit(item))
}

function challengeStateLabel(item) {
  if (challengeAuditFailed(item)) return '审计失败 · 无效反证'
  return resolutionLabel(item?.resolution_status || item?.status)
}

function claimKey(claim) {
  return claim.claim_id || claim.id || JSON.stringify(claim)
}

function challengeKey(item) {
  return item.challenge_id || item.id || JSON.stringify(item)
}

function roleLabel(role) {
  const labels = {
    quality: '稳健与质量', quality_agent: '稳健与质量', conservative_quality: '稳健与质量',
    growth: '成长与变化', growth_agent: '成长与变化', growth_change: '成长与变化',
    evidence_auditor: '证据审计', auditor: '证据审计',
    judge: '裁决综合', synthesizer: '裁决综合',
  }
  return labels[role] || role || '系统'
}

function roleClass(role = '') {
  if (String(role).includes('quality') || role === 'quality') return 'quality'
  if (String(role).includes('growth') || role === 'growth') return 'growth'
  return 'neutral'
}

function statusClass(status = '') {
  return ['withdrawn', 'rejected', 'audit_failed'].includes(status) ? 'failed' : status
}

function claimStatus(status) {
  const labels = {
    proposed: '待审计', accepted: '已采纳', disputed: '仍有分歧',
    revised: '已修订', withdrawn: '已撤回', rejected: '未采纳', audit_failed: '审计失败',
  }
  return labels[status] || status || '待处理'
}

function resolutionLabel(status) {
  const labels = { open: '未解决', resolved: '已解决', sustained: '反证成立', upheld: '反证成立', dismissed: '反证未成立', rejected: '反证未成立' }
  return labels[status] || status || '待回应'
}

function evidenceRefs(item) {
  if (!item || typeof item !== 'object') return []
  const refs = item.evidence_refs || item.evidence_uids || item.evidence_ids || item.citations || []
  return asArray(refs).map((ref) => typeof ref === 'object' ? (ref.display_id || ref.evidence_uid || ref.id) : ref).filter(Boolean)
}

function displayRef(ref) {
  const text = String(ref)
  return /^E\d+$/.test(text) ? `[${text}]` : text.startsWith('[') ? text : `[${text}]`
}

function toText(value) {
  return asArray(value).map(verdictText).join('；')
}

function verdictText(item) {
  if (typeof item === 'string' || typeof item === 'number') return String(item)
  return item?.assertion || item?.statement || item?.text || item?.description || item?.claim || JSON.stringify(item)
}
</script>

<style scoped>
.debate-panel { color: #243b5a; }
.empty { padding: 32px; color: #6a7f9c; text-align: center; border: 1px dashed #c5d2e5; }
.debate-head { display: flex; justify-content: space-between; gap: 18px; align-items: flex-end; margin-bottom: 10px; }
.debate-head h3 { margin: 2px 0 0; font-size: 20px; }
.eyebrow { color: #b8860b; font-size: 10px; letter-spacing: .16em; }
.summary-counts { display: flex; gap: 12px; color: #6a7f9c; font-size: 12px; flex-wrap: wrap; }
.summary-counts b { color: #1a3a6b; font-size: 15px; }
.current-turn { background: #edf2f8; border-left: 3px solid #1a3a6b; padding: 8px 11px; margin-bottom: 14px; font-size: 13px; }
.columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
h4 { margin: 0 0 9px; font-size: 15px; }
.card { border: 1px solid #d9e1ec; background: rgba(255,255,255,.8); padding: 11px; margin-bottom: 8px; }
.challenge-card { border-left: 3px solid #75507b; }
.challenge-card.audit-failed {
  border-color: #c0392b;
  background: #fff7f6;
}
.card-meta { display: flex; gap: 7px; align-items: center; color: #6a7f9c; font-size: 11px; flex-wrap: wrap; }
.role { padding: 2px 6px; color: #fff; background: #6a7f9c; }
.role.quality { background: #445d48; }
.role.growth { background: #805a32; }
.state { margin-left: auto; }
.state.accepted { color: #2e7d52; }
.state.disputed { color: #b8860b; }
.state.failed, .state.withdrawn, .state.rejected { color: #c0392b; }
.card p { margin: 8px 0 4px; line-height: 1.6; font-size: 13px; }
.assumptions, .response { color: #6a7f9c; font-size: 12px; margin-top: 6px; }
.challenge-audit-warning {
  margin-top: 8px;
  padding: 8px 10px;
  border-left: 3px solid #c0392b;
  background: #fdecea;
  color: #8f2f24;
  font-size: 12px;
  line-height: 1.5;
}
.audit-issue { margin-top: 3px; overflow-wrap: anywhere; }
.audit-issue code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.references { display: flex; gap: 4px; margin-top: 8px; flex-wrap: wrap; }
.references button, .inline-ref { border: 0; padding: 0; background: transparent; color: #1a3a6b; cursor: pointer; font-size: 11px; }
.verdict { margin-top: 18px; border-top: 1px solid #c5d2e5; padding-top: 14px; }
.verdict-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.verdict-section { background: #f6f8fb; padding: 10px 12px; }
.verdict-section h5 { margin: 0 0 6px; color: #1a3a6b; }
.verdict-section ul { margin: 0; padding-left: 17px; }
.verdict-section li, .verdict-section p { margin: 3px 0; font-size: 12px; line-height: 1.5; }
@media (max-width: 760px) { .columns, .verdict-grid { grid-template-columns: 1fr; } .debate-head { align-items: flex-start; flex-direction: column; } }
</style>
