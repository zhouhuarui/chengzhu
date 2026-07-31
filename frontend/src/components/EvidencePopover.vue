<template>
  <span
    class="evidence-ref-wrapper"
    :class="{ 'is-standalone-wrapper': standalone }"
    :data-evidence-ref="String(id)"
  >
    <sup
      v-if="!standalone"
      class="evidence-ref"
      :class="{ 'is-scenario': variant === 'scenario' }"
      @click.stop="toggle"
    >[{{ id }}]</sup>
    <div v-if="isOpen" class="popover" :class="{ 'is-standalone': standalone }" @click.stop>
      <button class="close" type="button" aria-label="关闭证据详情" @click="closePopover">×</button>
      <template v-if="card">
        <h4>{{ card.title || '证据 #' + id }}</h4>
        <p class="meta">
          <span>{{ card.source_name || card.source_type }}</span>
          <span v-if="card.publish_time"> · {{ card.publish_time }}</span>
        </p>
        <p class="excerpt">{{ card.excerpt || card.summary || card.content || '暂无摘录' }}</p>
        <a
          v-if="variant !== 'scenario' && card.url"
          :href="card.url"
          target="_blank"
          rel="noopener"
          class="link"
        >查看原文 ↗</a>
        <div v-else-if="variant !== 'scenario' && hasProvenance" class="provenance">
          <p class="provenance-title">结构化数据溯源</p>
          <dl>
            <template v-for="row in provenanceRows" :key="row.key">
              <dt>{{ row.label }}</dt>
              <dd :title="String(row.value)">{{ row.value }}</dd>
            </template>
          </dl>
          <p v-if="licenseNote" class="license-note">{{ licenseNote }}</p>
        </div>
        <p v-else-if="variant !== 'scenario'" class="no-link">
          该证据暂无可用原文链接
        </p>
        <p v-else class="sim-note">模拟动作 / 采访原文</p>
      </template>
      <p v-else class="no-data">暂无证据详情</p>
    </div>
  </span>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  id: { type: [String, Number], required: true },
  card: { type: Object, default: null },
  variant: { type: String, default: 'evidence' },
  standalone: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])
const open = ref(false)
const isOpen = computed(() => props.standalone || open.value)

const provenanceRows = computed(() => {
  const provenance = props.card?.provenance || {}
  const fields = [
    ['provider', '数据提供方'],
    ['api', '接口'],
    ['upstream_source', '上游来源'],
    ['record_key', '记录键'],
    ['business_key', '业务键'],
    ['as_of', '数据时点'],
    ['update_time', '更新时间'],
    ['warehouse_watermark', '仓库水位'],
    ['row_fingerprint', '行指纹'],
  ]
  return fields
    .filter(([key]) => provenance[key] !== undefined && provenance[key] !== null && provenance[key] !== '')
    .map(([key, label]) => ({ key, label, value: provenance[key] }))
})

const hasProvenance = computed(() => (
  provenanceRows.value.length > 0 || Boolean(props.card?.provenance?.license_scope)
))

const licenseNote = computed(() => {
  const provenance = props.card?.provenance || {}
  const scope = provenance.license_scope
  if (scope === 'private_derived_only') {
    return '授权提示：仅限私有派生使用，不提供原始数据批量下载。'
  }
  if (scope === 'private_only') return '授权提示：仅限私有环境使用。'
  if (scope) return `授权范围：${scope}`
  if (String(provenance.provider || '').toLowerCase() === 'datayes') {
    return '授权范围尚未确认，按私有派生数据处理。'
  }
  return ''
})

function toggle() {
  if (props.standalone) return
  open.value = !open.value
}

function closePopover() {
  if (props.standalone) {
    emit('close')
    return
  }
  open.value = false
}
</script>

<style scoped>
.evidence-ref-wrapper {
  position: relative;
  display: inline;
}

.evidence-ref-wrapper.is-standalone-wrapper {
  display: block;
}

.evidence-ref {
  color: #b8860b;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.75em;
  padding: 0 2px;
}

.evidence-ref.is-scenario {
  text-decoration: underline dashed #b8860b;
}

.popover {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 100;
  width: 280px;
  background: #fff;
  border: 1px solid #c5d2e5;
  box-shadow: 0 8px 24px rgba(26, 58, 107, 0.15);
  padding: 14px 16px;
  font-size: 13px;
  line-height: 1.5;
  color: #1a3a6b;
  font-weight: normal;
  font-style: normal;
  text-align: left;
}

.popover.is-standalone {
  position: relative;
  inset: auto;
  transform: none;
  width: min(520px, calc(100vw - 64px));
  max-height: min(70vh, 620px);
  overflow-y: auto;
}

.popover h4 {
  margin: 0 0 6px;
  font-size: 14px;
}

.meta {
  color: #6a7f9c;
  font-size: 12px;
  margin: 0 0 8px;
}

.excerpt {
  margin: 0 0 10px;
  max-height: 120px;
  overflow-y: auto;
}

.link {
  color: #1a3a6b;
  font-weight: 600;
}

.provenance {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid #e4eaf2;
}

.provenance-title {
  margin: 0 0 6px;
  font-weight: 600;
}

.provenance dl {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  gap: 3px 8px;
  margin: 0;
  font-size: 11px;
}

.provenance dt {
  color: #6a7f9c;
}

.provenance dd {
  margin: 0;
  overflow-wrap: anywhere;
  max-height: 42px;
  overflow: hidden;
}

.license-note,
.no-link {
  margin: 8px 0 0;
  color: #7a6540;
  font-size: 11px;
  line-height: 1.4;
}

.sim-note {
  color: #b8860b;
  font-size: 12px;
  margin: 0;
}

.no-data {
  color: #9aa8bc;
  margin: 0;
}

.close {
  position: absolute;
  top: 6px;
  right: 8px;
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  color: #9aa8bc;
  line-height: 1;
}
</style>
