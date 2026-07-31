<template>
  <AppLayout>
    <router-view />
    <footer class="app-footer">
      <p>{{ disclaimer }}</p>
    </footer>
  </AppLayout>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import AppLayout from './components/AppLayout.vue'
import { metaApi } from './api/index.js'

const DEFAULT_DISCLAIMER =
  '本系统仅做信息整理与情景观察，不构成投资建议。'

const disclaimer = ref(DEFAULT_DISCLAIMER)

onMounted(async () => {
  try {
    const res = await metaApi.disclaimer()
    if (res?.data?.disclaimer) {
      disclaimer.value = res.data.disclaimer
    }
  } catch {
    /* use default */
  }
})
</script>

<style>
:root {
  --cz-blue: #1a3a6b;
  --cz-amber: #b8860b;
  --cz-bg: #f7f9fc;
  --cz-muted: #6a7f9c;
  --cz-border: rgba(26, 58, 107, 0.08);
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
  -webkit-font-smoothing: antialiased;
  color: var(--cz-blue);
  background: var(--cz-bg);
}

button {
  font-family: inherit;
}

::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #eef3f8;
}

::-webkit-scrollbar-thumb {
  background: #1a3a6b;
  border-radius: 4px;
}

::-webkit-scrollbar-thumb:hover {
  background: #2a5088;
}

.markdown-body {
  font-family: "Songti SC", "Noto Serif SC", Georgia, serif;
}

.markdown-body table {
  display: block;
  overflow-x: auto;
}

.app-footer {
  max-width: 1400px;
  margin: 0 auto;
  padding: 16px 32px 32px;
  font-size: 13px;
  color: var(--cz-muted);
  text-align: center;
  line-height: 1.6;
}

.app-footer p {
  background: rgba(255, 255, 255, 0.5);
  padding: 12px 16px;
  border: 1px solid var(--cz-border);
}
</style>
