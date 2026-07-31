<template>
  <div class="app-layout" :class="{ 'demo-mode': isDemo }">
    <header class="top-nav">
      <router-link to="/" class="brand">
        <span class="brand-zh">成竹</span>
        <span class="brand-en">Foresketch</span>
      </router-link>
      <nav class="nav-links">
        <router-link to="/" class="nav-link">新建任务</router-link>
        <router-link to="/tracking" class="nav-link">
          追踪中心
          <span v-if="unreadCount > 0" class="badge">{{ unreadCount > 9 ? '9+' : unreadCount }}</span>
        </router-link>
        <router-link to="/profile" class="nav-link">我的偏好</router-link>
        <LanguageSwitcher />
      </nav>
    </header>
    <main class="main-content">
      <slot />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import LanguageSwitcher from './LanguageSwitcher.vue'
import { trackingApi } from '../api/index.js'
import { usePolling } from '../composables/usePolling.js'

const route = useRoute()
const unreadCount = ref(0)
const isDemo = ref(false)

async function fetchNotifications() {
  try {
    const res = await trackingApi.notifications()
    unreadCount.value = res?.data?.count ?? 0
  } catch {
    unreadCount.value = 0
  }
}

const { start, stop } = usePolling(fetchNotifications, { interval: 30000, immediate: true })

onMounted(() => {
  isDemo.value = route.query.demo === '1'
  start()
})

onUnmounted(stop)
</script>

<style scoped>
.app-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1200px 600px at 10% -10%, #d6e4f5 0%, transparent 55%),
    linear-gradient(180deg, #f7f9fc 0%, #eef3f8 100%);
  color: #1a3a6b;
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
}

.app-layout.demo-mode {
  font-size: 110%;
}

.top-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 32px;
  border-bottom: 1px solid rgba(26, 58, 107, 0.1);
  background: rgba(255, 255, 255, 0.65);
  backdrop-filter: blur(8px);
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 12px;
  text-decoration: none;
  color: inherit;
}

.brand-zh {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.brand-en {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 16px;
  color: #b8860b;
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 20px;
}

.nav-link {
  color: #1a3a6b;
  text-decoration: none;
  font-size: 15px;
  position: relative;
  padding: 4px 0;
}

.nav-link:hover,
.nav-link.router-link-active {
  color: #b8860b;
}

.badge {
  position: absolute;
  top: -6px;
  right: -14px;
  background: #c0392b;
  color: #fff;
  font-size: 10px;
  min-width: 16px;
  height: 16px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
  font-family: system-ui, sans-serif;
}

.main-content {
  flex: 1;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  padding: 24px 32px 48px;
}
</style>
