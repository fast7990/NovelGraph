import { useEffect, useRef, useState } from 'react'
import { Avatar, Card, Chat, Empty, Resizable, Select, Tag, Typography } from '@douyinfe/semi-ui'
import type { Message } from '@douyinfe/semi-ui/lib/es/chat/interface'
import PageHead from '../components/PageHead'
import { api, Citation, Novel, RetrievalMode, streamSSE } from '../api/client'

// 检索模式徽标：让"这次是两路融合还是单路降级"一眼可见，排查时不用翻日志
const MODE_TAG: Record<RetrievalMode, { text: string; color: 'green' | 'amber' | 'blue' | 'red' }> = {
  hybrid: { text: '混合检索', color: 'green' },
  dense_only: { text: '仅向量', color: 'amber' },
  sparse_only: { text: '仅词法', color: 'blue' },
  empty: { text: '未命中', color: 'red' },
}

const SUGGESTIONS = [
  '这本书的主要人物有哪些？各自的身份是什么？',
  '开头三章发生了什么？',
  '主角和谁的关系最紧张？为什么？',
  '故事的高潮出现在哪一章？',
]

const ROLE_CONFIG = {
  user: {
    name: '你',
    color: 'light-blue',
    avatar: (
      <Avatar color="light-blue" size="small">
        你
      </Avatar>
    ),
  },
  assistant: {
    name: '小说助手',
    color: 'violet',
    avatar: (
      <Avatar color="violet" size="small">
        AI
      </Avatar>
    ),
  },
}

export default function ChatPage({
  novelId,
  onPick,
}: {
  novelId: string
  onPick: (id: string) => void
}) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [chats, setChats] = useState<Message[]>([])
  const [citeMap, setCiteMap] = useState<Record<string, Citation[]>>({})
  const [modeMap, setModeMap] = useState<Record<string, RetrievalMode>>({})
  const [activeId, setActiveId] = useState('') // 右侧面板当前展示的引用所属消息
  const [activeIdx, setActiveIdx] = useState(-1)
  const [sessionId, setSessionId] = useState('')
  const [loading, setLoading] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    api.novels().then((r) => {
      // 只要已经有切片就能检索，未跑完的书也允许边问边等
      const ready = r.items.filter((n) => n.chunk_count > 0 || ['READY', 'VECTORIZING'].includes(n.status))
      setNovels(ready)
      if (!novelId && ready.length) onPick(ready[0].id)
    })
  }, [])

  useEffect(() => {
    setChats([])
    setCiteMap({})
    setActiveId('')
    setSessionId('')
  }, [novelId])

  const send = async (text: string) => {
    const q = (text || '').trim()
    if (!q || !novelId || loading) return

    setLoading(true)
    const aid = `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    setActiveId(aid)
    setActiveIdx(-1)
    // 用户消息由 Chat 内部追加并通过 onChatsChange 回传，这里只补占位的助手消息
    setChats((prev) => [
      ...prev,
      { id: aid, role: 'assistant', content: '', createAt: Date.now(), status: 'loading' as const },
    ])

    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      await streamSSE(
        '/api/v1/chat',
        { novel_id: novelId, question: q, session_id: sessionId || null },
        (event, data) => {
          if (event === 'meta') {
            setCiteMap((prev) => ({ ...prev, [aid]: data.citations || [] }))
            setModeMap((prev) => ({ ...prev, [aid]: (data.mode || 'hybrid') as RetrievalMode }))
            setSessionId(data.session_id || '')
          } else if (event === 'delta') {
            setChats((prev) =>
              prev.map((m) =>
                m.id === aid ? { ...m, content: String(m.content || '') + data.text } : m,
              ),
            )
          } else if (event === 'error') {
            setChats((prev) =>
              prev.map((m) =>
                m.id === aid
                  ? { ...m, content: String(m.content || '') || `生成失败：${data.message || '未知错误'}`, status: 'error' as const }
                  : m,
              ),
            )
          }
        },
        ctrl.signal,
      )
      setChats((prev) =>
        prev.map((m) =>
          m.id === aid ? { ...m, status: (m.content ? 'complete' : 'error') as 'complete' | 'error' } : m,
        ),
      )
    } catch (e: any) {
      const aborted = e?.name === 'AbortError'
      setChats((prev) =>
        prev.map((m) =>
          m.id === aid
            ? {
                ...m,
                status: (aborted ? 'incomplete' : 'error') as 'incomplete' | 'error',
                content: String(m.content || '') || (aborted ? '（已停止生成）' : `生成失败：${e.message || e}`),
              }
            : m,
        ),
      )
    } finally {
      setLoading(false)
      abortRef.current = null
    }
  }

  const citations = citeMap[activeId] || []
  const retMode = modeMap[activeId]

  return (
    <>
      <PageHead
        title="问答"
        desc="混合检索（P0 为稠密召回）→ 回溯父块 → 拼装上下文 → 流式生成，答案带原文引用。"
        extra={
          <Select
            value={novelId || undefined}
            onChange={(v) => onPick(String(v))}
            style={{ width: 260 }}
            placeholder="选择已入库的小说…"
          >
            {novels.map((n) => (
              <Select.Option key={n.id} value={n.id}>
                {n.title || n.filename}
              </Select.Option>
            ))}
          </Select>
        }
      />

      {novels.length === 0 && (
        <Card>
          <Empty title="还没有完成入库的小说" description="先去「数据上传」页传一本" />
        </Card>
      )}

      {novels.length > 0 && (
        <div className="chat-page">
          <div className="chat-main">
            <Chat
              style={{ height: '100%', width: '100%' }}
              chats={chats}
              onChatsChange={(list) => setChats(list || [])}
              onMessageSend={(content) => send(content)}
              onHintClick={(hint) => send(hint)}
              hints={chats.length === 0 ? SUGGESTIONS : []}
              roleConfig={ROLE_CONFIG}
              placeholder="例如：第三章里谁救了主角？"
              enableUpload={false}
              showStopGenerate={loading}
              onStopGenerator={() => abortRef.current?.abort()}
              showClearContext
              onClear={() => {
                setSessionId('')
                setCiteMap({})
                setChats([])
              }}
              chatBoxRenderConfig={{
                renderChatBoxContent: ({ message, defaultContent }) => {
                  const cs = message?.id ? citeMap[message.id] : undefined
                  if (message?.role !== 'assistant' || !cs?.length) return defaultContent
                  return (
                    <div>
                      {defaultContent}
                      <div className="cite-chips">
                        {cs.map((c, i) => (
                          <span
                            key={c.chunk_id}
                            className={`cite-chip ${
                              activeId === message.id && activeIdx === i ? 'active' : ''
                            }`}
                            onClick={() => {
                              setActiveId(String(message.id))
                              setActiveIdx(i)
                            }}
                          >
                            资料 {i + 1} · 第 {c.chapter_no} 章
                          </span>
                        ))}
                      </div>
                    </div>
                  )
                },
              }}
            />
          </div>

          <Resizable
            className="cite-panel"
            defaultSize={{ width: 340, height: '100%' }}
            minWidth={280}
            maxWidth={560}
            enable={{ left: true, right: false, top: false, bottom: false }}
            style={{ height: '100%' }}
          >
            <div className="cite-head">
              <span>引用资料</span>
              <span style={{ display: 'flex', gap: 6 }}>
                {retMode && (
                  <Tag color={MODE_TAG[retMode].color} type="light" size="small">
                    {MODE_TAG[retMode].text}
                  </Tag>
                )}
                <Tag color="grey" type="light" size="small">
                  {citations.length} 段
                </Tag>
              </span>
            </div>
            <div className="cite-body">
              {citations.length === 0 && (
                <Typography.Paragraph type="tertiary" size="small">
                  提问后这里显示检索命中的原文块。拖动左边缘可调整面板宽度。
                </Typography.Paragraph>
              )}
              {citations.map((c, i) => (
                <div
                  className="cite-item"
                  key={c.chunk_id}
                  style={
                    activeIdx === i
                      ? { borderColor: 'var(--semi-color-primary)' }
                      : undefined
                  }
                >
                  <div className="head">
                    <span>
                      资料 {i + 1} · 第 {c.chapter_no} 章
                    </span>
                    <span className="score">{c.score.toFixed(3)}</span>
                  </div>
                  {c.channels && (
                    <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
                      {c.channels.dense != null && (
                        <Tag color="blue" type="ghost" size="small">
                          向量 #{c.channels.dense}
                        </Tag>
                      )}
                      {c.channels.lexical != null && (
                        <Tag color="teal" type="ghost" size="small">
                          词法 #{c.channels.lexical}
                        </Tag>
                      )}
                      {c.rerank_score != null && (
                        <Tag color="purple" type="ghost" size="small">
                          精排 {c.rerank_score.toFixed(2)}
                        </Tag>
                      )}
                    </div>
                  )}
                  <div style={{ fontWeight: 500, marginBottom: 4 }}>{c.title}</div>
                  <div className="snip">{c.snippet}…</div>
                  <Typography.Text type="tertiary" size="small" className="mono">
                    字符 {c.char_start}-{c.char_end}
                  </Typography.Text>
                </div>
              ))}
            </div>
          </Resizable>
        </div>
      )}
    </>
  )
}
