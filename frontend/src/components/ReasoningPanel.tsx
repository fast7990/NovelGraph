/**
 * 思考过程面板（问答页）。
 *
 * 为什么必须有这块：glm-5 是推理模型，实测「检索 ≈2~10s、首思考 1~2.5s、首正文 15~43s」。
 * 以前只转发 `delta.content`，于是用户点完发送要盯着空气半分钟，看起来像卡死。
 * 现在两个阶段都有反馈：
 *   retrieving → 检索与拼上下文（不确定耗时，只说在做什么）
 *   thinking   → 模型思考增量，边流边显示，标题带计时
 *   answering  → 正文已到，面板自动收起，标题留「已思考 43.2 秒 / 3537 字」
 *
 * 交互约定：
 * - 思考中默认展开，正文开始自动收起，但**用户手动点过就听用户的**（touched 标记）。
 * - 思考文本按纯文本渲染，不当 markdown 解析：它是含未闭合 `**` 的半成品草稿，
 *   边流边解析会抽搐，而且这里要的是"能看清推理脉络"，不是排版。
 */
import { useEffect, useRef, useState } from 'react'
import { IconChevronDown, IconChevronRight } from '@douyinfe/semi-icons'

export type ThinkPhase = 'retrieving' | 'thinking' | 'answering' | 'done'

export type RetSummary = {
  mode: string
  dense?: number
  lexical?: number
  parents?: number
  reranked?: boolean
  graphFacts?: number
  degraded?: boolean
}

const MODE_TEXT: Record<string, string> = {
  hybrid: '混合检索',
  dense_only: '仅向量',
  sparse_only: '仅词法',
  empty: '未命中',
}

export default function ReasoningPanel({
  text,
  phase,
  ms,
  retrieval,
}: {
  text: string
  phase: ThinkPhase
  ms?: number
  retrieval?: RetSummary
}) {
  const running = phase === 'retrieving' || phase === 'thinking'
  const [open, setOpen] = useState(running)
  const touched = useRef(false)
  // 计时从"思考真正开始"算：检索那几秒不算进模型的思考时间，和后端 reasoning_ms 对得上
  const startRef = useRef<number | null>(phase === 'thinking' ? Date.now() : null)
  // 正文开始时先冻住计时：`done` 事件要等整段答案生成完才到，中间不能显示"已思考 0 秒"
  const frozenRef = useRef<number | null>(null)
  const [, tick] = useState(0)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)

  useEffect(() => {
    if (phase === 'thinking' && startRef.current == null) startRef.current = Date.now()
    if (phase !== 'thinking' && startRef.current != null && frozenRef.current == null) {
      frozenRef.current = Date.now() - startRef.current
    }
  }, [phase])

  // 自动开合：过程进行中展开，正文一开始就收起；用户动过开关之后不再抢方向盘
  useEffect(() => {
    if (touched.current) return
    setOpen(running)
  }, [running])

  // 每秒 10 帧刷新计时（只在思考时跑，停下就清掉）
  useEffect(() => {
    if (phase !== 'thinking') return
    const t = window.setInterval(() => tick((n) => n + 1), 100)
    return () => window.clearInterval(t)
  }, [phase])

  // 思考中贴底滚动；用户往上翻过就别再拽回来
  useEffect(() => {
    const el = bodyRef.current
    if (!el || !open || phase !== 'thinking' || !stickRef.current) return
    el.scrollTop = el.scrollHeight
  }, [text, open, phase])

  const liveSecs = startRef.current ? (Date.now() - startRef.current) / 1000 : 0
  const secs =
    phase === 'thinking' ? liveSecs : ms ? ms / 1000 : frozenRef.current ? frozenRef.current / 1000 : 0
  const hasText = text.length > 0
  const hasFacts = !!retrieval && (retrieval.dense != null || retrieval.lexical != null)

  const title =
    phase === 'retrieving'
      ? '正在检索资料…'
      : phase === 'thinking'
        ? hasText
          ? '正在思考…'
          : '正在拆解问题…'
        : secs > 0 && hasText
          ? `已思考 ${secs.toFixed(1)} 秒`
          : hasText
            ? '已思考'
            : '检索过程'

  // 收合后既没有思考文本也没有检索信息（非推理模型）：整块不渲染，别留空壳
  if (!running && !hasText && !hasFacts) return null

  return (
    <div className={`think ${running ? 'running' : ''}`}>
      <button
        type="button"
        className="think-head"
        onClick={() => {
          touched.current = true
          setOpen((v) => !v)
        }}
      >
        <span className="think-caret">{open ? <IconChevronDown size="small" /> : <IconChevronRight size="small" />}</span>
        {running ? <span className="think-dot" /> : null}
        <span className="think-title">{title}</span>
        <span className="think-meta">
          {phase === 'thinking' ? `${secs.toFixed(1)}s` : hasText ? `${text.length} 字` : ''}
        </span>
      </button>

      {open && (
        <div
          className="think-body"
          ref={(el) => {
            bodyRef.current = el
          }}
          onScroll={(e) => {
            const el = e.currentTarget
            stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          }}
        >
          {retrieval && (
            <div className="think-facts">
              <span className={`fact ${retrieval.mode === 'hybrid' ? 'ok' : 'warn'}`}>
                {MODE_TEXT[retrieval.mode] || retrieval.mode}
              </span>
              {retrieval.dense != null && <span className="fact">向量 {retrieval.dense}</span>}
              {retrieval.lexical != null && <span className="fact">词法 {retrieval.lexical}</span>}
              {retrieval.reranked != null && (
                <span className="fact">{retrieval.reranked ? '已精排' : '未精排'}</span>
              )}
              {retrieval.parents != null && <span className="fact">资料 {retrieval.parents} 段</span>}
              {retrieval.graphFacts ? <span className="fact">图谱关系 {retrieval.graphFacts} 条</span> : null}
            </div>
          )}
          {hasText ? (
            <div className="think-text">{text}</div>
          ) : (
            <div className="think-skeleton">
              {phase === 'retrieving' ? '混合检索召回切片 → 回溯父块 → 拼装上下文…' : '正在等待模型返回第一段思考…'}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
