import { useEffect, useState } from 'react'
import { api, fmtNum } from '../api/client'

export default function Overview({ onGo }: { onGo: (t: any) => void }) {
  const [health, setHealth] = useState<any>(null)
  const [stats, setStats] = useState<any>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    const load = () => {
      api.health().then(setHealth).catch((e) => setErr(String(e.message || e)))
      api.stats().then(setStats).catch(() => {})
    }
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  const cfg = health?.config || {}
  const deps: any[] = health?.deps || []

  return (
    <>
      <div className="page-head">
        <h1>概览</h1>
        <p>P0 闭环：上传 → 解析切块 → 本地向量化 → Milvus 写入 → RAG 问答。知识图谱在 P1 接入。</p>
      </div>

      {err && <div className="err-box" style={{ marginBottom: 16 }}>后端未连通：{err}</div>}

      <div className="grid cols-4">
        <div className="card">
          <div className="stat-label">小说数</div>
          <div className="stat-value">{stats?.novels ?? '-'}</div>
          <div className="stat-label">已入库书籍</div>
        </div>
        <div className="card">
          <div className="stat-label">切片总数</div>
          <div className="stat-value">
            {stats ? fmtNum(stats.chunks) : '-'}
          </div>
          <div className="stat-label">父块 + 子块</div>
        </div>
        <div className="card">
          <div className="stat-label">累计字数</div>
          <div className="stat-value">
            {stats ? fmtNum(stats.chars) : '-'}
            <span className="stat-unit">字</span>
          </div>
          <div className="stat-label">原始文本规模</div>
        </div>
        <div className="card">
          <div className="stat-label">向量条数</div>
          <div className="stat-value">{stats ? fmtNum(stats.vectors) : '-'}</div>
          <div className="stat-label">{stats?.vector_backend || '-'} 后端</div>
        </div>
      </div>

      <div className="grid cols-2 section-gap">
        <div className="card">
          <div className="card-title">依赖健康</div>
          {deps.length === 0 && <div className="muted">检测中…</div>}
          {deps.map((d) => (
            <div className="kv" key={d.name}>
              <span>
                <span className={`dot ${d.status === 'ok' ? 'ok' : d.status === 'degraded' ? 'warn' : 'err'}`} style={{ marginRight: 8 }} />
                {d.name}
              </span>
              <span className="muted ellipsis" style={{ maxWidth: 300 }}>
                {d.status}
                {d.server_version ? ` · ${d.server_version}` : ''}
                {d.model ? ` · ${d.model}` : ''}
                {d.detail ? ` · ${d.detail}` : ''}
              </span>
            </div>
          ))}
        </div>

        <div className="card">
          <div className="card-title">运行配置</div>
          <div className="kv"><span>向量后端</span><span>{cfg.vector_backend || '-'}</span></div>
          <div className="kv"><span>Embedding</span><span>{cfg.embedding} · {cfg.embedding_dim}d · {cfg.embedding_threads} 线程</span></div>
          <div className="kv"><span>Milvus 集合</span><span className="mono">{cfg.milvus_collection}</span></div>
          <div className="kv"><span>LLM</span><span>{cfg.llm_model}</span></div>
          <div className="kv"><span>切块粒度</span><span>父 {cfg.chunk_parent_size} / 子 {cfg.chunk_child_size}</span></div>
          <div className="kv"><span>运行时长</span><span>{health?.uptime_sec ?? 0}s · 队列 {health?.queue ?? 0}</span></div>
        </div>
      </div>

      <div className="card section-gap">
        <div className="card-title">下一步</div>
        <div className="row">
          <button className="btn primary" onClick={() => onGo('upload')}>上传小说</button>
          <button className="btn" onClick={() => onGo('chunks')}>查看切片完成度</button>
          <button className="btn" onClick={() => onGo('chat')}>开始问答</button>
        </div>
        <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
          P1 计划：Neo4j 图谱抽取与写入、BM25 + RRF 混合检索、reranker 精排、图谱可视化、向量空间投影视图。
        </p>
      </div>
    </>
  )
}
