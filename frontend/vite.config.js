import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@locales': path.resolve(__dirname, '../locales')
    }
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.js'],
    include: ['tests/unit/**/*.spec.js'],
    clearMocks: true,
  },
  server: {
    // 私有数据模式默认仅本机访问；远程调试需显式改写 host。
    host: '127.0.0.1',
    port: 3000,
    open: false,
    proxy: {
      '/api': {
        // Docker 中由 compose 指向 backend 服务；本机开发保持原地址。
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:5001',
        changeOrigin: true,
        secure: false
      }
    }
  }
})
