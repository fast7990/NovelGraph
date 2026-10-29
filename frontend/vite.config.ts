import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址默认 8000；起第二个后端实例验证时用 VITE_API_TARGET 指过去，
// 免得为了测新代码去动别人正在跑的那个进程。
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // 固定 IPv4：Node 18+ 会把 localhost 解析成 ::1，导致 127.0.0.1 连不上
    // （curl / 预览面板 / 手机同网段调试都会踩这个坑）
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
