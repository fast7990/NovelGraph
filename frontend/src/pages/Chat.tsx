import { useEffect, useRef, useState } from 'react'
import { api, Citation, Novel, streamSSE } from '../api/client'

type Msg = { role: 'user' | 'assistant'; content: string; citations?: Citation[]; degraded?: boolean }

const SUGGESTIONS = [
  '这本书的主要人物有哪些？各自的身份是什么？',
  '开头三章发生了什么？',
  '主角和谁的关系最紧张？为什么？',
  '故事的高潮出现在哪一章？',
]

export default function Chat({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [citations, setCitations] = useState<Citation[]>([])
  const [sessionId, setSessionId] = useState('')
  const [err, setErr] = useState('')
  const [degraded, setDegraded] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    api.novels().then((r) => {
      const ready = r.items.filter((n) => n.status === 'READY' || n.status === 'VECTORIZING')
      setNovels(ready)
      if (!novelId && ready.length) onPick(ready[0].id)
    })
  }, [])

  useEffect(() => {
    setMsgs([])
    setCitations([])
    setSessionId('')
  }, [novelId])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [msgs])

  const send = async (text?: string) => {
    const q = (text ?? input).trim()
    if (!q || !novelId || loading) return
    setInput('')
    setErr('')
    setLoading(true)
    setMsgs((m) => [...m, { role: 'user', content: q }, { role: 'assistant', content: '' }])

    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      await streamSSE(
        '/api/v1/chat',
        { novel_id: novelId, question: q, session_id: sessionId || null },
        (event, data) => {
          if (event === 'meta') {
            setCitations(data.citations || [])
            setSessionId(data.session_id || '')
            setDegraded(!!data.degraded)
          } else if (event === 'delta') {
            setMsgs((m) => {
              const copy = [...m]
              const last = copy[copy.length - 1]
              copy[copy.length - 1] = { ...last, content: last.content + data.text }
              return copy
            })
          } else if (event === 'error') {
            setErr(data.message || '生成失败')
          }
        },
        ctrl.signal,
      )
    } catch (e: any) {
      setErr(String(e.message || e))
    } finally {
      setLoading(false)
      abortRef.current = null
    }
  }

  return (
    <>
      <div className="page-head row between" style={{ alignItems: 'flex-start' }}>
        <div>
          <h1>问答</h1>
          <p>混合检索（P0 为稠密召回）→ 回溯父块 → 拼装上下文 → 流式生成，答案带原文引用。</p>
        </div>
        <select className="input" style={{ width: 260 }} value={novelId} onChange={(e) => onPick(e.target.value)}>
          <option value="">选择已入库的小说…</option>
          {novels.map((n) => (
            <option key={n.id} value={n.id}>{n.title || n.filename}</option>
          ))}
        </select>
      </div>

      {novels.length === 0 && (
        <div className="card"><div className="empty">还没有完成入库的小说，先去「数据上传」页传一本</div></div>
      )}

      {novels.length > 0 && (
        <div className="chat-wrap">
          <div className="chat-main">
            <div className="chat-log" ref={logRef}>
              {msgs.length === 0 && (
                <div style={{ margin: 'auto', textAlign: 'center', maxWidth: 520 }}>
                  <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>针对这本书提问</div>
                  <p className="muted" style={{ marginBottom: 16 }}>
                    检索的是切片后的原文，回答会标注资料编号，可点右侧引用看原始段落。
                  </p>
                  <div className="grid" style={{ gap: 8 }}>
                    {SUGGESTIONS.map((s) => (
                      <button key={s} className="btn" style={{ textAlign: 'left' }} onClick={() => send(s)}>{s}</button>
                    ))}
                  </div>
                </div>
              )}

              {msgs.map((m, i) => (
                <div className={`msg ${m.role}`} key={i}>
                  <div className="avatar">{m.role === 'user' ? '我' : 'AI'}</div>
                  <div className="bubble">
                    <div className="who">{m.role === 'user' ? '你' : '小说助手'}</div>
                    {m.content || (loading && i === msgs.length - 1 ? '检索中…' : '')}
                  </div>
                </div>
              ))}
              {err && <div className="err-box">{err}</div>}
            </div>

            <div className="chat-input">
              <textarea
                className="input"
                placeholder="例如：第三章里谁救了主角？"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    send()
                  }
                }}
              />
              {loading ? (
                <button className="btn" onClick={() => abortRef.current?.abort()}>停止</button>
              ) : (
                <button className="btn primary" onClick={() => send()} disabled={!input.trim()}>发送</button>
              )}
            </div>
          </div>

          <div className="cite-panel">
            <div className="card-title">
              引用资料
              {degraded && <span className="badge orange">降级召回</span>}
            </div>
            {citations.length === 0 && <div className="muted">提问后显示检索到的原文块</div>}
            {citations.map((c, i) => (
              <div className="cite-item" key={c.chunk_id}>
                <div className="head">
                  <span>资料 {i + 1} · 第 {c.chapter_no} 章</span>
                  <span className="score">{c.score.toFixed(3)}</span>
                </div>
                <div style={{ fontWeight: 500, marginBottom: 3 }}>{c.title}</div>
                <div className="snip">{c.snippet}…</div>
                <div className="muted mono" style={{ marginTop: 4 }}>字符 {c.char_start}-{c.char_end}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
