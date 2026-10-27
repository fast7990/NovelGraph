import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtBytes, fmtNum, Job, Novel, STAGE_LABEL } from '../api/client'

const STAGE_ORDER = ['PENDING', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'COMPLETED']

export default function Upload({ onGo }: { onGo: (t: any, id?: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [jobs, setJobs] = useState<Record<string, Job>>({})
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [hot, setHot] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    try {
      const [n, j] = await Promise.all([api.novels(), api.jobs()])
      setNovels(n.items)
      const map: Record<string, Job> = {}
      // 每个 novel 取最新一条 job（后端已按时间倒序返回）
      for (const job of j) if (!map[job.novel_id]) map[job.novel_id] = job
      setJobs(map)
    } catch (e: any) {
      setErr(String(e.message || e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 1800)
    return () => clearInterval(t)
  }, [refresh])

  const doUpload = async (file: File) => {
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const res = await api.upload(file, title)
      setMsg(`已入队：${file.name}（任务 ${res.job_id.slice(0, 10)}…），切片与向量化正在进行`)
      setTitle('')
      await refresh()
    } catch (e: any) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setHot(false)
    const f = e.dataTransfer.files?.[0]
    if (f) doUpload(f)
  }

  const activeCount = Object.values(jobs).filter(
    (j) => j.status === 'RUNNING' || j.status === 'PENDING',
  ).length

  return (
    <>
      <div className="page-head">
        <h1>数据上传</h1>
        <p>支持 txt / md，自动识别 UTF-8 / GB18030。上传后立即触发解析、切块、向量化全流程。</p>
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1.4fr 1fr' }}>
        <div className="card">
          <div
            className={`dropzone ${hot ? 'hot' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setHot(true) }}
            onDragLeave={() => setHot(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
          >
            <div style={{ fontSize: 26 }}>⬆</div>
            <h3>拖入小说文件，或点击选择</h3>
            <p>单个文件 ≤ 200MB；建议先按章整理，纯文本效果最好</p>
            <input
              ref={inputRef}
              type="file"
              accept=".txt,.md,.text"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) doUpload(f)
                e.target.value = ''
              }}
            />
          </div>
          {msg && <p className="muted" style={{ marginTop: 12, color: '#12a150' }}>{msg}</p>}
          {err && <div className="err-box" style={{ marginTop: 12 }}>{err}</div>}
        </div>

        <div className="card">
          <div className="card-title">入库参数</div>
          <label className="field">
            <span>书名（可选，默认取文件名）</span>
            <input className="input" value={title} placeholder="例如：长夜将明" onChange={(e) => setTitle(e.target.value)} />
          </label>
          <div className="kv"><span>切块粒度</span><span>父块 1400 / 子块 400 字</span></div>
          <div className="kv"><span>重叠</span><span>60 字</span></div>
          <div className="kv"><span>Embedding</span><span>本地 ONNX · bge-small-zh · 4 线程</span></div>
          <div className="kv"><span>向量库</span><span>Milvus 3.0（Zilliz Serverless）</span></div>
          {activeCount > 0 && (
            <p className="muted" style={{ marginTop: 10 }}>
              队列中还有 {activeCount} 个任务在跑。入库是串行的 —— 4 核 CPU 上并行只会互相抢线程。
            </p>
          )}
        </div>
      </div>

      <div className="card pad-0 section-gap">
        <table>
          <thead>
            <tr>
              <th style={{ width: '30%' }}>小说</th>
              <th>状态</th>
              <th>进度</th>
              <th>字数</th>
              <th>章节</th>
              <th>切片</th>
              <th>向量</th>
              <th style={{ width: 150 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {novels.length === 0 && (
              <tr><td colSpan={8}><div className="empty">还没有数据，先上传一本试试</div></td></tr>
            )}
            {novels.map((n) => {
              const job = jobs[n.id]
              const running = job && (job.status === 'RUNNING' || job.status === 'PENDING')
              const pct = job?.status === 'COMPLETED' ? 100 : job?.progress || 0
              const stageIdx = job ? STAGE_ORDER.indexOf(job.stage) : -1
              return (
                <tr key={n.id} className="clickable" onClick={() => onGo('chunks', n.id)}>
                  <td>
                    <div style={{ fontWeight: 500 }}>{n.title || n.filename}</div>
                    <div className="muted">{n.filename} · {fmtBytes(n.file_size)} · {n.encoding || '?'}</div>
                  </td>
                  <td>
                    <span className={`badge ${n.status === 'READY' ? 'green' : n.status === 'FAILED' ? 'red' : n.status === 'PENDING' ? 'gray' : 'blue'}`}>
                      {STAGE_LABEL[n.status] || n.status}
                    </span>
                    {job?.stage && running && (
                      <div className="muted" style={{ marginTop: 3 }}>{STAGE_LABEL[job.stage] || job.stage}</div>
                    )}
                  </td>
                  <td style={{ minWidth: 150 }}>
                    <div className={`bar thin ${job?.status === 'COMPLETED' ? 'ok' : ''}`}>
                      <i style={{ width: `${Math.min(pct, 100)}%` }} />
                    </div>
                    <div className="muted" style={{ marginTop: 3 }}>
                      {running && job
                        ? `${STAGE_LABEL[job.stage]} · ${pct.toFixed(0)}% · ${fmtNum(job.chars_done)}/${fmtNum(job.chars_total || n.char_count)} 字`
                        : job?.status === 'FAILED'
                          ? '失败'
                          : job
                            ? `${pct.toFixed(0)}%${stageIdx >= 0 ? ` · ${STAGE_LABEL[job.stage]}` : ''}`
                            : '-'}
                    </div>
                  </td>
                  <td className="mono">{fmtNum(n.char_count)}</td>
                  <td className="mono">{n.chapter_count || '-'}</td>
                  <td className="mono">{fmtNum(n.chunk_count)}</td>
                  <td className="mono">{job ? fmtNum(job.vectors_written) : '-'}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="row">
                      <button className="btn sm" onClick={() => onGo('chunks', n.id)}>切片</button>
                      <button className="btn sm" onClick={() => onGo('chat', n.id)}>问答</button>
                      <button
                        className="btn sm ghost"
                        onClick={async () => {
                          if (!confirm(`删除《${n.title}》及其全部切片与向量？`)) return
                          await api.deleteNovel(n.id)
                          refresh()
                        }}
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}
