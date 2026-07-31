import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import TaskConfirmView from '../views/TaskConfirmView.vue'
import TaskRunView from '../views/TaskRunView.vue'
import ReportView from '../views/ReportView.vue'
import TrackingView from '../views/TrackingView.vue'
import ProfileView from '../views/ProfileView.vue'
import ScenarioView from '../views/ScenarioView.vue'

const routes = [
  { path: '/', name: 'Home', component: Home },
  { path: '/task/:taskId/confirm', name: 'TaskConfirm', component: TaskConfirmView },
  { path: '/task/:taskId', name: 'TaskRun', component: TaskRunView },
  { path: '/report/:taskId', name: 'Report', component: ReportView },
  { path: '/tracking', name: 'Tracking', component: TrackingView },
  { path: '/profile', name: 'Profile', component: ProfileView },
  { path: '/scenario/:scenarioId', name: 'Scenario', component: ScenarioView },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
