import { useEffect, useRef, useState } from 'react'
import { Button, Input, Popconfirm, Spin, Typography } from '@douyinfe/semi-ui'
import { IconEdit, IconPlus, IconDelete } from '@douyinfe/semi-icons'
import { api, ChatSession } from '../api/client'

/**
 * 会话侧栏。
 *
 * 存在的理由：问答的上下文是**有状态的**——后端把历史存进 chat_messages，
 * 但如果不把 session_id 持久化、也不给用户"回到上次对话"的入口，
 * 刷新一次就等于把之前的对话扔了（数据还在，只是界面里看不见）。
 *
 * 三件事在这里闭环：
 * 1. 列出该书的会话，按最近活跃排序（会话列表接口带 message_count 与 last_message_at）；
 * 2. 切换会话 → 拉历史消息回填聊天区；
 * 3. 新建 / 重命名 / 删除。
 */
export default function SessionList({
  sessions,
  activeId,
  loading,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}: {
  sessions: ChatSession[]
  activeId: string
  loading: boolean
  onSelect: (id: string) => void
  onCreate: () => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
}) {
  const [editingId, setEditingId] = useState('')
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editingId) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editingId])

  const commit = () => {
    const t = draft.trim()
    setEditingId('')
    if (t) onRename(editingId, t)
  }

  return (
    <div className="sess-list">
      <div className="sess-head">
        <span className="sess-title">历史对话</span>
        <Button
          size="small"
          type="tertiary"
          icon={<IconPlus />}
          onClick={onCreate}
          disabled={loading}
          aria-label="新建对话"
        />
      </div>

      <div className="sess-items">
        {loading && (
          <div className="sess-empty">
            <Spin size="small" />
          </div>
        )}
        {!loading && sessions.length === 0 && (
          <div className="sess-empty">
            <Typography.Text type="tertiary" size="small">
              还没有对话记录
            </Typography.Text>
          </div>
        )}
        {!loading &&
          sessions.map((s) => (
            <div
              key={s.id}
              className={`sess-item ${activeId === s.id ? 'active' : ''}`}
              onClick={() => activeId !== s.id && onSelect(s.id)}
              title={s.title}
            >
              {editingId === s.id ? (
                <Input
                  ref={inputRef}
                  size="small"
                  value={draft}
                  onChange={(v) => setDraft(String(v))}
                  onBlur={commit}
                  onEnterPress={commit}
                  onClick={(e) => e.stopPropagation()}
                  style={{ width: '100%' }}
                />
              ) : (
                <>
                  <div className="sess-line">
                    <span className="sess-name">{s.title || '新对话'}</span>
                    <span className="sess-ops">
                      <span
                        className="sess-op"
                        role="button"
                        title="重命名"
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditingId(s.id)
                          setDraft(s.title)
                        }}
                      >
                        <IconEdit size="small" />
                      </span>
                      <Popconfirm
                        title="删除该对话？"
                        content="消息记录会一并删除，无法恢复。"
                        onConfirm={() => onDelete(s.id)}
                      >
                        <span
                          className="sess-op"
                          role="button"
                          title="删除"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <IconDelete size="small" />
                        </span>
                      </Popconfirm>
                    </span>
                  </div>
                  <div className="sess-meta">
                    <span>{s.message_count} 条</span>
                    {(s.prompt_tokens || s.completion_tokens) ? (
                      <span title="本会话问答消耗的 token（含后台摘要）">
                        {fmtTok((s.prompt_tokens || 0) + (s.completion_tokens || 0))} tok
                      </span>
                    ) : null}
                    {s.summary_chars ? (
                      <span
                        className="sess-sum"
                        title={`早期 ${Math.floor((s.summary_seq || 0) / 2)} 轮已压缩成摘要，最近几轮保留原文`}
                      >
                        摘要
                      </span>
                    ) : null}
                    {s.last_message_at && <span>{fmtWhen(s.last_message_at)}</span>}
                  </div>
                </>
              )}
            </div>
          ))}
      </div>
    </div>
  )
}

/** 相对时间。超过 7 天给绝对日期 —— "31 天前"没人算得出来。 */
function fmtWhen(iso: string) {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return ''
  const diff = Date.now() - t
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d <= 7) return `${d} 天前`
  const dt = new Date(t)
  return `${dt.getMonth() + 1}-${dt.getDate()}`
}

/** token 数量：上万就折成 k，否则侧栏那点宽度放不下。 */
function fmtTok(n: number) {
  if (n >= 10000) return `${(n / 1000).toFixed(0)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
