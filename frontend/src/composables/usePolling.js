import { ref, onUnmounted } from 'vue'

/**
 * 通用轮询 hook：start / stop / 间隔 / 终止条件
 */
export function usePolling(callback, options = {}) {
  const {
    interval = 2000,
    immediate = true,
    stopWhen = null,
  } = options

  const running = ref(false)
  const error = ref(null)
  let timer = null
  let inFlight = false

  async function tick() {
    if (!running.value || inFlight) return
    inFlight = true
    error.value = null
    try {
      const result = await callback()
      if (stopWhen && stopWhen(result)) {
        stop()
      }
      return result
    } catch (e) {
      error.value = e
    } finally {
      inFlight = false
    }
  }

  function start() {
    if (timer) return
    running.value = true
    if (immediate) tick()
    timer = setInterval(tick, interval)
  }

  function stop() {
    running.value = false
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  onUnmounted(stop)

  return { start, stop, running, error, tick }
}
