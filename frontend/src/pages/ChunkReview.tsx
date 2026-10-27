import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, Chunk, ChunkReport, fmtNum, Novel } from '../api/client'

const SEV_LABEL: Record<string, string> = { info: '提示', warn: '警告', error: '严重' }
const SEV_CLASS: Record<string, string> = { info: 'gray', warn: 'orange', error: 'red' }

const CODE_DESC: Record<string, string> = {
  NO_CHAPTER_STRUCTURE: '未识别到章节结构',
  CHAPTER_TOO_SHORT: '章节过短（疑似目录页）',
  CHAPTER_TOO_LONG: '章节过长（疑似漏切）',
  CHUNK_TOO_LONG: '块超出模型长度上限',
  CHUNK_TOO_SHORT: '块过短（信息量不足）',
  PARENT_MISSING: '父块无子块',
  OVERLAP_ANOMALY: '重叠异常',
  DUP_CHUNK: '重复块',
  ENCODING_SUSPECT: '编码疑似错误',
}

export default function ChunkReview({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [report, setReport] = useState<ChunkReport | null>(null)
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [total, setTotal] = useState(0)
  const [level, setLevel] = useState<number | undefined>(1)
  const [chapter, setChapter] = useState<string>('')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState<Chunk | null>(null)

  useEffect(() => {
    api.novels().then((r) => {
      setNovels(r.items)
      if (!novelId && r.items.length) onPick(r.items[0].id)
    })
  }, [])

  const novel = useMemo(() => novels.find((n) => n.id === novelId), [novels, novelId])

  const loadReport = useCallback(async () => {
    if (!novelId) return
    try {
      setReport(await api.chunkReport(novelId))
    } catch { /* 可能正在切片 */ }
  }, [novelId])

  const loadChunks = useCallback(async () => {
    if (!novelId) return
    setLoading(true)
    try {
      const r = await api.chunks(novelId, {
        level,
        chapter_no: chapter ? Number(chapter) : undefined,
        page,
        page_size: 10,
      })
      setChunks(r.items)
      setTotal(r.total)
    } finally {
      setLoading(false)
    }
  }, [novelId, level, chapter, page])

  useEffect(() => {
    loadReport()
    loadChunks()
    const t = setInterval(loadReport, 4000)
    return () => clearInterval(t)
  }, [loadReport, loadChunks])

  const histMax = Math.max(1, ...(report?.token_hist || [1]))
  const running = novel && ['PROCESSING', 'PENDING', 'VECTORIZING'].includes(novel.status)

  return (
    <>
      <div className="page-head row between" style={{ alignItems: 'flex-start' }}>
        <div>
          <h1>切片完成度</h1>
          <p>进度按「已切字符 / 总字符」计算 —— 块数在流式切分中事前未知，用它算会出现越跑越退的假进度。</p>
        </div>
        <select className="input" style={{ width: 260 }} value={novelId} onChange={(e) => { onPick(e.target.value); setPage(1) }}>
          <option value="">选择小说…</option>
          {novels.map((n) => (
            <option key={n.id} value={n.id}>{n.title || n.filename}</option>
          ))}
        </select>
      </div>

      {!novelId && <div className="card"><div className="empty">先选择一本小说</div></div>}

      {novelId && report && (
        <>
          <div className="grid cols-4">
            <div className="card">
              <div className="stat-label">切块完成度</div>
              <div className="stat-value">{(report.completeness * 100).toFixed(1)}<span className="stat-unit">%</span></div>
              <div className="bar ok" style={{ marginTop: 10 }}>
                <i style={{ width: `${report.completeness * 100}%` }} />
              </div>
              <div className="stat-label" style={{ marginTop: 6 }}>
                {fmtNum(report.covered_chars)} / {fmtNum(report.char_count)} 字已覆盖
              </div>
            </div>
            <div className="card">
              <div className="stat-label">切片数量</div>
              <div className="stat-value">{fmtNum(report.chunk_count)}</div>
              <div className="stat-label">父块 {fmtNum(report.parent_count)} · 子块 {fmtNum(report.chunk_count)}</div>
            </div>
            <div className="card">
              <div className="stat-label">块长度</div>
              <div className="stat-value">{report.avg_tokens}<span className="stat-unit">tok 均值</span></div>
              <div className="stat-label">P95 {report.p95_tokens} tok（上限 512）</div>
            </div>
            <div className="card">
              <div className="stat-label">质量告警</div>
              <div className="stat-value" style={{ color: report.issues.length ? '#d97706' : '#12a150' }}>
                {report.issues.length}
              </div>
              <div className="stat-label">
                严重 {report.issues_by_severity?.error || 0} · 警告 {report.issues_by_severity?.warn || 0}
              </div>
            </div>
          </div>

          <div className="grid cols-2 section-gap">
            <div className="card">
              <div className="card-title">
                阶段进度
                <button className="btn sm" onClick={async () => { await api.rebuild(novelId); loadReport() }}>重跑全流程</button>
              </div>
              <div className="progress-row">
                <span className="label">解析分章</span>
                <div className="bar"><i style={{ width: report.chapter_count ? '100%' : '0%' }} /></div>
                <span className="value">{report.chapter_count} 章</span>
              </div>
              <div className="progress-row">
                <span className="label">切块覆盖</span>
                <div className="bar"><i style={{ width: `${report.completeness * 100}%` }} /></div>
                <span className="value">{(report.completeness * 100).toFixed(1)}%</span>
              </div>
              <div className="progress-row">
                <span className="label">向量化</span>
                <div className="bar">
                  <i style={{ width: `${novel ? Math.min(100, (novel.chunk_count ? 100 : 0)) : 0}%` }} />
                </div>
                <span className="value">{report.chunk_count} 条</span>
              </div>
              <p className="muted" style={{ marginTop: 10, marginBottom: 0 }}>
                {running
                  ? '任务进行中：数字每 4 秒刷新一次，可切到「上传」页看实时 SSE 进度。'
                  : '已完成。若改了切块参数，点「重跑全流程」会先清空旧切片再重建，不会产生重复数据。'}
              </p>
            </div>

            <div className="card">
              <div className="card-title">子块长度分布（token 估算）</div>
              <div className="hist">
                {report.token_hist.map((v, i) => (
                  <div className="hist-col" key={i}>
                    <span className="hist-n">{v}</span>
                    <div className="hist-bar" style={{ height: `${(v / histMax) * 100}%` }} />
                    <span className="hist-x">{report.token_buckets[i]}+</span>
                  </div>
                ))}
              </div>
              <p className="muted" style={{ marginBottom: 0 }}>
                落在 512 之后的块会被 embedding 截断，是需要处理的信号。
              </p>
            </div>
          </div>

          {report.issues.length > 0 && (
            <div className="card pad-0 section-gap">
              <div className="card-title" style={{ padding: '16px 20px 0' }}>
                质量告警（{report.issues.length}）
                <span className="muted">按章节 / 类型排序，前 200 条</span>
              </div>
              <table style={{ marginTop: 10 }}>
                <thead>
                  <tr><th style={{ width: 70 }}>章节</th><th style={{ width: 90 }}>类型</th><th>说明</th><th style={{ width: 110 }}>原文位置</th></tr>
                </thead>
                <tbody>
                  {report.issues.map((it, i) => (
                    <tr key={i}>
                      <td className="mono">{it.chapter_no || '-'}</td>
                      <td><span className={`badge ${SEV_CLASS[it.severity]}`}>{SEV_LABEL[it.severity]}</span></td>
                      <td>
                        <div>{CODE_DESC[it.code] || it.code}</div>
                        <div className="muted">{it.detail}</div>
                      </td>
                      <td className="mono muted">@{it.char_start}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {report.sample_chunks.length > 0 && (
            <div className="card section-gap">
              <div className="card-title">人工抽检（每 20 章抽 1 块）</div>
              <div className="grid cols-2">
                {report.sample_chunks.map((c) => (
                  <div key={c.id}>
                    <div className="row between" style={{ marginBottom: 5 }}>
                      <span className="muted">第 {c.chapter_no} 章 · 第 {c.seq} 块</span>
                      <span className="tag">{c.token_est} tok</span>
                    </div>
                    <div className="chunk-box">{c.content.slice(0, 500)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="card pad-0 section-gap">
            <div className="card-title" style={{ padding: '16px 20px 0' }}>
              切片浏览（{total}）
              <div className="row">
                <select className="input" style={{ width: 130, padding: '5px 8px' }} value={level ?? ''} onChange={(e) => { setLevel(e.target.value === '' ? undefined : Number(e.target.value)); setPage(1) }}>
                  <option value="">全部层级</option>
                  <option value={0}>父块</option>
                  <option value={1}>子块</option>
                </select>
                <input className="input" style={{ width: 100, padding: '5px 8px' }} placeholder="章节号" value={chapter} onChange={(e) => { setChapter(e.target.value); setPage(1) }} />
              </div>
            </div>
            <table style={{ marginTop: 10 }}>
              <thead>
                <tr><th style={{ width: 60 }}>章</th><th style={{ width: 50 }}>序</th><th style={{ width: 64 }}>层级</th><th>内容</th><th style={{ width: 80 }}>token</th></tr>
              </thead>
              <tbody>
                {loading && <tr><td colSpan={5}><div className="empty">加载中…</div></td></tr>}
                {!loading && chunks.length === 0 && <tr><td colSpan={5}><div className="empty">暂无切片</div></td></tr>}
                {chunks.map((c) => (
                  <tr key={c.id} className="clickable" onClick={() => setOpen(c)}>
                    <td className="mono">{c.chapter_no}</td>
                    <td className="mono">{c.seq}</td>
                    <td><span className={`badge ${c.level === 0 ? 'blue' : 'gray'}`}>{c.level === 0 ? '父' : '子'}</span></td>
                    <td className="ellipsis" style={{ maxWidth: 520 }}>{c.content.slice(0, 90)}</td>
                    <td className="mono">{c.token_est}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="row between" style={{ padding: '12px 20px' }}>
              <span className="muted">第 {page} 页 / 共 {Math.max(1, Math.ceil(total / 10))} 页</span>
              <div className="row">
                <button className="btn sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
                <button className="btn sm" disabled={page >= Math.ceil(total / 10)} onClick={() => setPage(page + 1)}>下一页</button>
              </div>
            </div>
          </div>
        </>
      )}

      {open && (
        <div
          onClick={() => setOpen(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(16,24,40,0.34)', display: 'grid', placeItems: 'center', zIndex: 50, padding: 30 }}
        >
          <div className="card" style={{ width: 'min(780px, 100%)', maxHeight: '82vh', overflow: 'auto' }} onClick={(e) => e.stopPropagation()}>
            <div className="row between" style={{ marginBottom: 12 }}>
              <div>
                <b>第 {open.chapter_no} 章 · 第 {open.seq} 块</b>
                <div className="muted mono">
                  {open.id} · 字符 {open.char_start}-{open.char_end} · {open.token_est} tok
                </div>
              </div>
              <button className="btn sm" onClick={() => setOpen(null)}>关闭</button>
            </div>
            <div className="chunk-box" style={{ maxHeight: '60vh' }}>{open.content}</div>
            {open.parent_id && (
              <p className="muted mono" style={{ marginBottom: 0 }}>parent: {open.parent_id}</p>
            )}
          </div>
        </div>
      )}
    </>
  )
}
