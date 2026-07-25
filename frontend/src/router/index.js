import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/HomeShell.vue'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },
  // Phase 5 将启用：
  // /task/:taskId/confirm  TaskConfirm
  // /task/:taskId          TaskRun
  // /report/:taskId        Report
  // /tracking              Tracking
  // /profile               Profile
  // /scenario/:scenarioId  Scenario
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
