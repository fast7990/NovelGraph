import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // 固定 IPv4：Node 18+ 会把 localhost 解析成 ::1，导致 127.0.0.1 连不上
    // （curl / 预览面板 / 手机同网段调试都会踩这个坑）
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
