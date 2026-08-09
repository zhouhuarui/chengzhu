<template>
  <div ref="root" class="security-combobox">
    <div class="combobox-control" :class="{ selected: hasSelection }">
      <input
        ref="input"
        :value="query"
        type="text"
        role="combobox"
        autocomplete="off"
        aria-autocomplete="list"
        :aria-label="ariaLabel"
        :aria-controls="listboxId"
        :aria-expanded="isOpen ? 'true' : 'false'"
        :aria-activedescendant="activeDescendant"
        :aria-busy="loading ? 'true' : 'false'"
        placeholder="输入证券代码、简称或拼音"
        @focus="onFocus"
        @input="onInput"
        @keydown="onKeydown"
      />
      <button
        v-if="query"
        type="button"
        class="clear-button"
        aria-label="清除已选证券"
        @click="clearSelection"
      >
        ×
      </button>
    </div>

    <div v-if="isOpen" class="dropdown">
      <ul :id="listboxId" class="option-list" role="listbox" :aria-label="`${ariaLabel}候选`">
        <li v-if="loading" class="dropdown-status" role="option" aria-disabled="true">
          搜索中…
        </li>
        <li v-else-if="searchError" class="dropdown-status error" role="option" aria-disabled="true">
          {{ searchError }}
        </li>
        <li
          v-else-if="hasSearched && !results.length"
          class="dropdown-status"
          role="option"
          aria-disabled="true"
        >
          未找到匹配的证券
        </li>
        <template v-else>
          <li
            v-for="(item, index) in results"
            :id="optionId(index)"
            :key="item.sec_id || `${item.code}-${item.exchange}`"
            class="security-option"
            :class="{ active: index === activeIndex, disabled: isExcluded(item) }"
            role="option"
            :aria-selected="index === activeIndex ? 'true' : 'false'"
            :aria-disabled="isExcluded(item) ? 'true' : 'false'"
            @mouseenter="setActive(index)"
            @mousedown.prevent="selectItem(item)"
          >
            <span class="security-name">{{ item.name }}</span>
            <span class="security-code">{{ item.code }}</span>
            <span class="security-exchange">{{ exchangeLabel(item.exchange) }}</span>
            <span v-if="isExcluded(item)" class="already-added">已添加</span>
          </li>
        </template>
      </ul>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { securityApi } from '../api/index.js'

const props = defineProps({
  modelValue: {
    type: Object,
    default: null,
  },
  excludedCodes: {
    type: Array,
    default: () => [],
  },
  rowId: {
    type: String,
    required: true,
  },
  ariaLabel: {
    type: String,
    default: '搜索证券',
  },
  debounceMs: {
    type: Number,
    default: 200,
  },
})

const emit = defineEmits(['update:modelValue'])

const root = ref(null)
const input = ref(null)
const query = ref(displayValue(props.modelValue))
const results = ref([])
const loading = ref(false)
const hasSearched = ref(false)
const searchError = ref('')
const isOpen = ref(false)
const activeIndex = ref(-1)

let debounceTimer = null
let requestVersion = 0
let editing = false

const safeRowId = String(props.rowId).replace(/[^A-Za-z0-9_-]/g, '-')
const listboxId = `security-options-${safeRowId}`
const excludedCodeSet = computed(() => new Set(props.excludedCodes.map((code) => String(code))))
const hasSelection = computed(() => isResolved(props.modelValue) && !editing)
const activeDescendant = computed(() => {
  if (!isOpen.value || activeIndex.value < 0 || !results.value[activeIndex.value]) return undefined
  return optionId(activeIndex.value)
})

watch(
  () => [
    props.modelValue?.code,
    props.modelValue?.name,
    props.modelValue?.sec_id,
    props.modelValue?._resolved,
  ],
  () => {
    if (isResolved(props.modelValue)) {
      editing = false
      query.value = displayValue(props.modelValue)
      closeDropdown()
    } else if (!editing) {
      query.value = displayValue(props.modelValue)
    }
  },
)

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  clearTimeout(debounceTimer)
  requestVersion += 1
})

function isResolved(value) {
  return Boolean(
    value?.code
    && value?.name
    && (value?.sec_id || value?._resolved === true),
  )
}

function displayValue(value) {
  if (!value) return ''
  const code = String(value.code || '').trim()
  const name = String(value.name || '').trim()
  if (isResolved(value)) return `${name} · ${code}`
  return name || code
}

function normalizeItem(item) {
  const code = String(item?.code ?? item?.ticker ?? '').trim()
  const name = String(item?.name ?? item?.sec_short_name ?? '').trim()
  if (!code || !name) return null
  return {
    sec_id: String(item?.sec_id || '').trim(),
    code,
    name,
    exchange: String(item?.exchange ?? item?.exchange_cd ?? '').trim(),
    list_status: String(item?.list_status ?? item?.list_status_cd ?? '').trim(),
  }
}

function onFocus() {
  if (!hasSelection.value && query.value.trim()) scheduleSearch(query.value)
}

function onInput(event) {
  editing = true
  query.value = event.target.value
  emit('update:modelValue', null)
  scheduleSearch(query.value)
}

function scheduleSearch(rawQuery) {
  clearTimeout(debounceTimer)
  const searchQuery = String(rawQuery || '').trim()
  const version = ++requestVersion
  results.value = []
  activeIndex.value = -1
  hasSearched.value = false
  searchError.value = ''

  if (!searchQuery) {
    loading.value = false
    isOpen.value = false
    return
  }

  loading.value = true
  isOpen.value = true
  debounceTimer = setTimeout(() => performSearch(searchQuery, version), props.debounceMs)
}

async function performSearch(searchQuery, version) {
  try {
    const response = await securityApi.search(searchQuery, 10)
    if (version !== requestVersion) return
    const items = Array.isArray(response?.data?.items) ? response.data.items : []
    results.value = items.map(normalizeItem).filter(Boolean)
    activeIndex.value = firstSelectableIndex(results.value)
  } catch {
    if (version !== requestVersion) return
    results.value = []
    activeIndex.value = -1
    searchError.value = '证券搜索暂时不可用'
  } finally {
    if (version === requestVersion) {
      loading.value = false
      hasSearched.value = true
      isOpen.value = true
    }
  }
}

function firstSelectableIndex(items) {
  return items.findIndex((item) => !isExcluded(item))
}

function isExcluded(item) {
  return excludedCodeSet.value.has(String(item?.code || ''))
}

function selectItem(item) {
  if (isExcluded(item)) return
  const normalized = normalizeItem(item)
  if (!normalized) return
  editing = false
  query.value = displayValue({ ...normalized, _resolved: true })
  emit('update:modelValue', normalized)
  closeDropdown()
}

function clearSelection() {
  clearTimeout(debounceTimer)
  requestVersion += 1
  editing = false
  query.value = ''
  results.value = []
  loading.value = false
  hasSearched.value = false
  searchError.value = ''
  emit('update:modelValue', null)
  closeDropdown()
  nextTick(() => input.value?.focus())
}

function onKeydown(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveActive(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveActive(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const item = results.value[activeIndex.value]
    if (isOpen.value && item && !isExcluded(item)) selectItem(item)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    closeDropdown()
  } else if (event.key === 'Tab') {
    closeDropdown()
  }
}

function moveActive(direction) {
  if (!results.value.length) return
  isOpen.value = true
  let next = activeIndex.value
  for (let attempts = 0; attempts < results.value.length; attempts += 1) {
    next = (next + direction + results.value.length) % results.value.length
    if (!isExcluded(results.value[next])) {
      activeIndex.value = next
      return
    }
  }
}

function setActive(index) {
  if (!isExcluded(results.value[index])) activeIndex.value = index
}

function optionId(index) {
  return `${listboxId}-option-${index}`
}

function closeDropdown() {
  clearTimeout(debounceTimer)
  requestVersion += 1
  loading.value = false
  isOpen.value = false
  activeIndex.value = -1
}

function onDocumentPointerDown(event) {
  if (root.value && !root.value.contains(event.target)) closeDropdown()
}

function exchangeLabel(exchange) {
  return {
    XSHG: '上交所',
    XSHE: '深交所',
    XBEI: '北交所',
  }[exchange] || exchange || '交易所未知'
}
</script>

<style scoped>
.security-combobox {
  position: relative;
  flex: 1;
  min-width: 0;
}

.combobox-control {
  display: flex;
  align-items: center;
  min-height: 40px;
  border: 1px solid #c5d2e5;
  background: #fff;
}

.combobox-control:focus-within {
  border-color: #1a3a6b;
  box-shadow: inset 0 0 0 1px #1a3a6b;
}

.combobox-control.selected {
  border-color: #9fb2cc;
  background: #fafdff;
}

.combobox-control input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
  padding: 9px 10px;
  background: transparent;
  color: #1a3a6b;
  font: inherit;
}

.clear-button {
  flex: 0 0 34px;
  align-self: stretch;
  border: 0;
  background: transparent;
  color: #7b8fa9;
  cursor: pointer;
  font-size: 18px;
}

.clear-button:hover {
  color: #1a3a6b;
}

.dropdown {
  position: absolute;
  z-index: 30;
  top: calc(100% + 4px);
  right: 0;
  left: 0;
  border: 1px solid #c5d2e5;
  background: #fff;
  box-shadow: 0 8px 22px rgba(26, 58, 107, 0.14);
}

.option-list {
  max-height: 250px;
  margin: 0;
  padding: 4px 0;
  overflow-y: auto;
  list-style: none;
}

.security-option {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 8px 10px;
  cursor: pointer;
}

.security-option.active {
  background: #edf3fa;
}

.security-option.disabled {
  color: #8d9db2;
  cursor: not-allowed;
}

.security-name {
  overflow: hidden;
  color: #1a3a6b;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.security-code,
.security-exchange,
.already-added,
.dropdown-status {
  color: #6a7f9c;
  font-size: 12px;
}

.already-added {
  grid-column: 1 / -1;
  margin-top: -6px;
}

.dropdown-status {
  padding: 12px 10px;
}

.dropdown-status.error {
  color: #c0392b;
}

@media (max-width: 520px) {
  .security-option {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .security-exchange {
    display: none;
  }
}
</style>
