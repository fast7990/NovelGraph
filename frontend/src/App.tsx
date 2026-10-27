import { useEffect, useState } from 'react'
import Overview from './pages/Overview'
import Upload from './pages/Upload'
import ChunkReview from './pages/ChunkReview'
import Chat from './pages/Chat'
import VectorView from './pages/VectorView'
import { api } from './api/client'

type Tab = 'overview' | 'upload' | 'chunks' | 'vector' | 'chat'

const NAV: { key: Tab; label: string; hint: string }[] = [
  { key: 'overview', label: '概览', hint: '依赖健康与全局统计' },
  { key: 'upload', label: '数据上传', hint: '入库与任务进度' },
  { key: 'chunks', label: '切片完成度', hint: '质量检查与抽检' },
  { key: 'vector', label: '向量库', hint: 'Milvus 集合与样本' },
  { key: 'chat', label: '问答', hint: 'RAG 检索增强对话' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<string>('checking')
  const [novelId, setNovelId] = useState<string>('')

  useEffect(() => {
    api.health().then((h) => setStatus(h.status)).catch(() => setStatus('down'))
  }, [])

  const go = (t: Tab, id?: string) => {
    if (id) setNovelId(id)
    setTab(t)
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">RAG</div>
          <div>
            <div className="brand-title">小说知识中台</div>
            <div className="brand-sub">Milvus 3.0 · P0</div>
          </div>
        </div>
        <nav>
          {NAV.map((n) => (
            <button
              key={n.key}
              className={`nav-item ${tab === n.key ? 'active' : ''}`}
              onClick={() => go(n.key)}
            >
              <span className="nav-label">{n.label}</span>
              <span className="nav-hint">{n.hint}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`dot ${status === 'ok' ? 'ok' : status === 'degraded' ? 'warn' : 'err'}`} />
          <span>服务 {status === 'ok' ? '正常' : status === 'degraded' ? '降级' : '异常'}</span>
        </div>
      </aside>

      <main className="main">
        {tab === 'overview' && <Overview onGo={go} />}
        {tab === 'upload' && <Upload onGo={go} />}
        {tab === 'chunks' && <ChunkReview novelId={novelId} onPick={setNovelId} />}
        {tab === 'vector' && <VectorView novelId={novelId} onPick={setNovelId} />}
        {tab === 'chat' && <Chat novelId={novelId} onPick={setNovelId} />}
      </main>
    </div>
  )
}
