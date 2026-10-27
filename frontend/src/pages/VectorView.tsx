import { useEffect, useMemo, useState } from 'react'
import { api, fmtNum, Novel } from '../api/client'

export default function VectorView({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [data, setData] = useState<any>(null)
  const [stats, setStats] = useState<any>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.novels().then((r) => {
      setNovels(r.items)
      if (!novelId && r.items.length) onPick(r.items[0].id)
    })
    api.stats().then(setStats).catch(() => {})
  }, [])

  useEffect(() => {
    if (!novelId) return
    api.vectorSample(novelId, 400).then(setData).catch((e) => setErr(String(e.message || e)))
  }, [novelId])

  const byChapter = useMemo(() => {
    const items: any[] = data?.items || []
    const map: Record<number, number> = {}
    for (const it of items) map[it.chapter_no] = (map[it.chapter_no] || 0) + 1
    return Object.entries(map).slice(0, 40).map(([k, v]) => ({ chapter: Number(k), count: v }))
  }, [data])

  const maxCount = Math.max(1, ...byChapter.map((b) => b.count))

  return (
    <>
      <div className="page-head row between" style={{ alignItems: 'flex-start' }}>
        <div>
          <h1>向量库</h1>
          <p>查看 Milvus 集合状态与已写入的向量样本。二维投影散点图（UMAP）排在 P1。</p>
        </div>
        <select className="input" style={{ width: 260 }} value={novelId} onChange={(e) => onPick(e.target.value)}>
          <option value="">选择小说…</option>
          {novels.map((n) => (
            <option key={n.id} value={n.id}>{n.title || n.filename}</option>
          ))}
        </select>
      </div>

      {err && <div className="err-box" style={{ marginBottom: 14 }}>{err}</div>}

      <div className="grid cols-4">
        <div className="card">
          <div className="stat-label">后端</div>
          <div className="stat-value" style={{ fontSize: 20 }}>{stats?.vector_backend || data?.backend || '-'}</div>
          <div className="stat-label">Milvus 3.0 · COSINE</div>
        </div>
        <div className="card">
          <div className="stat-label">全库向量</div>
          <div className="stat-value">{stats ? fmtNum(stats.vectors) : '-'}</div>
          <div className="stat-label">跨所有小说</div>
        </div>
        <div className="card">
          <div className="stat-label">本书样本</div>
          <div className="stat-value">{data ? data.count : '-'}</div>
          <div className="stat-label">拉取上限 400 条</div>
        </div>
        <div className="card">
          <div className="stat-label">向量维度</div>
          <div className="stat-value">{stats || data ? 512 : '-'}</div>
          <div className="stat-label">bge-small-zh-v1.5</div>
        </div>
      </div>

      <div className="card section-gap">
        <div className="card-title">按章节分布（样本）</div>
        <div className="hist" style={{ height: 150 }}>
          {byChapter.length === 0 && <div className="muted">暂无数据</div>}
          {byChapter.map((b) => (
            <div className="hist-col" key={b.chapter} title={`第 ${b.chapter} 章：${b.count} 块`}>
              <span className="hist-n">{b.count}</span>
              <div className="hist-bar" style={{ height: `${(b.count / maxCount) * 100}%` }} />
              <span className="hist-x">{b.chapter}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="card pad-0 section-gap">
        <div className="card-title" style={{ padding: '16px 20px 0' }}>向量记录样本</div>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr><th>chunk_id</th><th>章节</th><th>序</th><th>层级</th><th>字符区间</th><th>parent</th></tr>
          </thead>
          <tbody>
            {(data?.items || []).slice(0, 30).map((it: any) => (
              <tr key={it.id}>
                <td className="mono ellipsis" style={{ maxWidth: 240 }}>{it.id}</td>
                <td className="mono">{it.chapter_no}</td>
                <td className="mono">{it.seq}</td>
                <td><span className="badge gray">{it.level === 1 ? '子' : it.level}</span></td>
                <td className="mono">{it.char_start}-{it.char_end}</td>
                <td className="mono ellipsis" style={{ maxWidth: 200 }}>{it.parent_id || '-'}</td>
              </tr>
            ))}
            {!data?.items?.length && <tr><td colSpan={6}><div className="empty">暂无向量</div></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}
