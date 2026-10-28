import { useEffect, useState } from 'react'
import { Layout, Nav } from '@douyinfe/semi-ui'
import {
  IconApartment,
  IconComment,
  IconHome,
  IconLayers,
  IconPulse,
  IconSourceControl,
  IconTickCircle,
  IconUpload,
} from '@douyinfe/semi-icons'
import Overview from './pages/Overview'
import Upload from './pages/Upload'
import ChunkReview from './pages/ChunkReview'
import Chat from './pages/Chat'
import VectorView from './pages/VectorView'
import GraphView from './pages/GraphView'
import Tasks from './pages/Tasks'
import DebugSearch from './pages/DebugSearch'
import { api } from './api/client'

type Tab = 'overview' | 'upload' | 'tasks' | 'chunks' | 'vector' | 'graph' | 'search' | 'chat'

const NAV = [
  { itemKey: 'overview', text: '概览', icon: <IconHome /> },
  { itemKey: 'upload', text: '数据上传', icon: <IconUpload /> },
  { itemKey: 'tasks', text: '任务中心', icon: <IconTickCircle /> },
  { itemKey: 'chunks', text: '切片完成度', icon: <IconSourceControl /> },
  { itemKey: 'vector', text: '向量库', icon: <IconLayers /> },
  { itemKey: 'graph', text: '知识图谱', icon: <IconApartment /> },
  { itemKey: 'search', text: '检索调试台', icon: <IconPulse /> },
  { itemKey: 'chat', text: '问答', icon: <IconComment /> },
]

const STATUS_TEXT: Record<string, string> = { ok: '正常', degraded: '降级', down: '异常', checking: '检测中' }
const STATUS_COLOR: Record<string, string> = {
  ok: 'var(--semi-color-success)',
  degraded: 'var(--semi-color-warning)',
  down: 'var(--semi-color-danger)',
  checking: 'var(--semi-color-text-3)',
}

const TABS: Tab[] = ['overview', 'upload', 'tasks', 'chunks', 'vector', 'graph', 'search', 'chat']

const readHash = (): Tab => {
  const h = window.location.hash.replace('#', '') as Tab
  return TABS.includes(h) ? h : 'overview'
}

export default function App() {
  const [tab, setTab] = useState<Tab>(readHash)
  const [status, setStatus] = useState<string>('checking')
  // 支持 ?novel=nv_xxx 直接定位到某本书，便于排查问题与分享链接
  const [novelId, setNovelId] = useState<string>(
    () => new URLSearchParams(window.location.search).get('novel') || '',
  )

  useEffect(() => {
    const onHash = () => setTab(readHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    if (window.location.hash.replace('#', '') !== tab) {
      window.history.replaceState(null, '', `#${tab}`)
    }
  }, [tab])

  useEffect(() => {
    api.health().then((h) => setStatus(h.status)).catch(() => setStatus('down'))
  }, [])

  const go = (t: Tab, id?: string) => {
    if (id) setNovelId(id)
    setTab(t)
  }

  return (
    <Layout className="app-shell">
      <Layout.Sider className="app-sider" style={{ width: 208, height: '100%' }}>
        <Nav
          mode="vertical"
          style={{ height: '100%', maxWidth: '100%' }}
          selectedKeys={[tab]}
          onSelect={(d) => setTab(d.itemKey as Tab)}
          items={NAV}
          header={
            <div className="brand">
              <div className="brand-mark">RAG</div>
              <div>
                <div className="brand-title">小说知识中台</div>
                <div className="brand-sub">PostgreSQL + pgvector</div>
              </div>
            </div>
          }
          footer={
            <div className="nav-foot">
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  display: 'inline-block',
                  background: STATUS_COLOR[status] || STATUS_COLOR.checking,
                  boxShadow: `0 0 0 3px color-mix(in srgb, ${STATUS_COLOR[status] || STATUS_COLOR.checking} 18%, transparent)`,
                }}
              />
              服务{STATUS_TEXT[status] || status}
            </div>
          }
        />
      </Layout.Sider>

      <Layout.Content className="app-content">
        {tab === 'overview' && <Overview onGo={go} />}
        {tab === 'upload' && <Upload onGo={go} />}
        {tab === 'tasks' && <Tasks novelId={novelId} onGo={go} />}
        {tab === 'chunks' && <ChunkReview novelId={novelId} onPick={setNovelId} />}
        {tab === 'vector' && <VectorView novelId={novelId} onPick={setNovelId} />}
        {tab === 'graph' && <GraphView novelId={novelId} onPick={setNovelId} />}
        {tab === 'search' && <DebugSearch novelId={novelId} onPick={setNovelId} />}
        {tab === 'chat' && <Chat novelId={novelId} onPick={setNovelId} />}
      </Layout.Content>
    </Layout>
  )
}
