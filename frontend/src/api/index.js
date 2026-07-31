import axios from 'axios'
import i18n from '../i18n'

// 空字符串 = 同源 /api（走 Vite proxy），便于公网只暴露 3000
const BASE_URL =
  import.meta.env.VITE_API_BASE_URL !== undefined
    ? import.meta.env.VITE_API_BASE_URL
    : ''

const service = axios.create({
  baseURL: BASE_URL,
  timeout: 300000,
})

service.interceptors.request.use(
  (config) => {
    config.headers['Accept-Language'] = i18n.global.locale.value
    // FormData 必须由浏览器自动带 multipart boundary；JSON 才设 application/json
    if (typeof FormData !== 'undefined' && config.data instanceof FormData) {
      if (config.headers) {
        delete config.headers['Content-Type']
      }
    } else if (config.data && typeof config.data === 'object' && !config.headers['Content-Type']) {
      config.headers['Content-Type'] = 'application/json'
    }
    return config
  },
  (error) => Promise.reject(error)
)

service.interceptors.response.use(
  (response) => {
    const ct = response.headers['content-type'] || ''
    if (ct.includes('text/markdown') || typeof response.data === 'string') {
      return response.data
    }
    const res = response.data
    if (res && res.success === false) {
      return Promise.reject(new Error(res.error || res.message || 'Error'))
    }
    return res
  },
  (error) => {
    const apiError = error.response?.data?.error || error.response?.data?.message
    if (typeof apiError === 'string' && apiError) {
      error.message = apiError
    }
    return Promise.reject(error)
  }
)

export { BASE_URL, service }

// ── task ──
export const taskApi = {
  create(requirement, files = []) {
    const form = new FormData()
    form.append('requirement', requirement)
    for (const f of files) form.append('files', f)
    // Let axios set multipart boundary automatically
    return service.post('/api/task/create', form)
  },
  confirm(taskId, taskCard) {
    return service.post(`/api/task/${taskId}/confirm`, { task_card: taskCard })
  },
  status(taskId, runId = '') {
    return service.get(`/api/task/${taskId}/status`, { params: runId ? { run_id: runId } : {} })
  },
  agentLog(taskId, fromLine = 0, runId = '') {
    return service.get(`/api/task/${taskId}/agent-log`, {
      params: { from_line: fromLine, ...(runId ? { run_id: runId } : {}) },
    })
  },
  evidence(taskId, params = {}) {
    return service.get(`/api/task/${taskId}/evidence`, { params })
  },
  list(limit = 20) {
    return service.get('/api/task/list', { params: { limit } })
  },
  get(taskId) {
    return service.get(`/api/task/${taskId}`)
  },
  graph(taskId, runId = '') {
    return service.get(`/api/task/${taskId}/graph`, { params: runId ? { run_id: runId } : {} })
  },
  runs(taskId) {
    return service.get(`/api/task/${taskId}/runs`)
  },
  debate(taskId, runId = '') {
    return service.get(`/api/task/${taskId}/debate`, { params: runId ? { run_id: runId } : {} })
  },
  delete(taskId) {
    return service.delete(`/api/task/${taskId}`)
  },
}

// ── report ──
export const reportApi = {
  get(taskId, runId = '') {
    return service.get(`/api/report/${taskId}`, { params: runId ? { run_id: runId } : {} })
  },
  markdown(taskId, runId = '') {
    const params = runId ? { run_id: runId } : {}
    return service.get(`/api/report/${taskId}/markdown`, { params, responseType: 'text' }).catch(() =>
      service.get(`/api/report/${taskId}`, {
        params: { format: 'markdown', ...params },
        responseType: 'text',
      })
    )
  },
  chat(taskId, question, history = [], runId = '') {
    return service.post(`/api/report/${taskId}/chat`, { question, history, ...(runId ? { run_id: runId } : {}) })
  },
  reviewLog(taskId, runId = '') {
    return service.get(`/api/report/${taskId}/review-log`, { params: runId ? { run_id: runId } : {} })
  },
}

// ── feedback ──
export const feedbackApi = {
  section(taskId, sectionIndex, vote, comment = '', runId = '') {
    return service.post('/api/feedback/section', { task_id: taskId, section_index: sectionIndex, vote, comment, ...(runId ? { run_id: runId } : {}) })
  },
  report(taskId, stars, comment = '', runId = '') {
    return service.post('/api/feedback/report', { task_id: taskId, stars, comment, ...(runId ? { run_id: runId } : {}) })
  },
  get(taskId, runId = '') {
    return service.get(`/api/feedback/${taskId}`, { params: runId ? { run_id: runId } : {} })
  },
}

// ── memory ──
export const memoryApi = {
  prefill() {
    return service.get('/api/memory/prefill')
  },
  preferences() {
    return service.get('/api/memory/preferences')
  },
  deletePreference(key) {
    return service.delete(`/api/memory/preferences/${encodeURIComponent(key)}`)
  },
  deleteUser() {
    return service.delete('/api/memory/user')
  },
  playbook() {
    return service.get('/api/memory/playbook')
  },
  confirmPlaybook(id) {
    return service.post(`/api/memory/playbook/${id}/confirm`)
  },
  deletePlaybook(id) {
    return service.delete(`/api/memory/playbook/${id}`)
  },
  stats() {
    return service.get('/api/memory/playbook/stats')
  },
  sourceHealth(windowDays = 7) {
    return service.get('/api/memory/source-health', { params: { window_days: windowDays } })
  },
}

// ── tracking ──
export const trackingApi = {
  subscribe(taskId, cron, hour = 8) {
    return service.post('/api/tracking/subscribe', { task_id: taskId, cron, hour })
  },
  list() {
    return service.get('/api/tracking/list')
  },
  pause(subId) {
    return service.post(`/api/tracking/${subId}/pause`)
  },
  resume(subId) {
    return service.post(`/api/tracking/${subId}/resume`)
  },
  delete(subId) {
    return service.delete(`/api/tracking/${subId}`)
  },
  briefs(subId) {
    return service.get(`/api/tracking/${subId}/briefs`)
  },
  runNow(subId) {
    return service.post(`/api/tracking/${subId}/run-now`)
  },
  notifications() {
    return service.get('/api/tracking/notifications')
  },
}

// ── scenario ──
export const scenarioApi = {
  create(payload) {
    return service.post('/api/scenario/create', payload)
  },
  start(scenarioId, scenarioConfig) {
    return service.post(`/api/scenario/${scenarioId}/start`, {
      scenario_config: scenarioConfig,
    })
  },
  status(scenarioId) {
    return service.get(`/api/scenario/${scenarioId}/status`)
  },
  runStatus(scenarioId) {
    return service.get(`/api/scenario/${scenarioId}/run-status`)
  },
  agentLog(scenarioId, fromLine = 0) {
    return service.get(`/api/scenario/${scenarioId}/agent-log`, { params: { from_line: fromLine } })
  },
  interview(scenarioId, topic, maxAgents = 3) {
    return service.post(`/api/scenario/${scenarioId}/interview`, {
      topic,
      max_agents: maxAgents,
    })
  },
  report(scenarioId) {
    return service.get(`/api/scenario/${scenarioId}/report`)
  },
}

// ── meta ──
export const metaApi = {
  disclaimer() {
    return service.get('/api/meta/disclaimer')
  },
  health() {
    return service.get('/api/health')
  },
}

export default service
