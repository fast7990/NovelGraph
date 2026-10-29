import { useCallback, useEffect, useRef, useState } from 'react'
import { Avatar, Card, Chat, Empty, Resizable, Select, Tag, Toast, Typography } from '@douyinfe/semi-ui'
import type { Message } from '@douyinfe/semi-ui/lib/es/chat/interface'
import PageHead from '../components/PageHead'
import ReasoningPanel from '../components/ReasoningPanel'
import type { RetSummary, ThinkPhase } from '../components/ReasoningPanel'
import SessionList from '../components/SessionList'
import { api, ChatSession, Citation, Novel, RetrievalMode, streamSSE } from '../api/client'
import { fmtTok } from '../utils/tok'

/** 一轮问答的 token 消耗。round = 本轮，total = 这个会话到现在为止。 */
type RoundUsage = {
  round?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; calls?: number }
  total?: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
    calls?: number
    summary_seq?: number
    summary_chars?: number
  }
}

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
  // 思考过程按消息 id 存：Semi 的 Message 对象会被它内部回写，不适合塞大数据。
  // 流式增量先攒在闭包里每 90ms 刷一次，避免一个字一次 setState 把整棵树重绘。
  const [thinkMap, setThinkMap] = useState<Record<string, { text: string; phase: ThinkPhase; ms?: number }>>({})
  const [retMap, setRetMap] = useState<Record<string, RetSummary>>({})
  // 本轮用到的上下文实体（后端从历史里继承出来用于消歧）
  const [memMap, setMemMap] = useState<Record<string, { turns: number; entities: string[] }>>({})
  // 每轮问答的 token 消耗，按消息 id 存。来源是 done 事件而不是会话列表接口：
  // 落账与列表刷新之间有时序差，等列表回来再显示会让用户觉得"没记上"。
  const [usageMap, setUsageMap] = useState<Record<string, RoundUsage>>({})
  const [activeId, setActiveId] = useState('') // 右侧面板当前展示的引用所属消息
  const [activeIdx, setActiveIdx] = useState(-1)
  const [sessionId, setSessionId] = useState('')
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [sessLoading, setSessLoading] = useState(false)
  // 搜索关键词：输入即改 state（受控），真正发请求走 300ms 防抖 effect，
  // 否则每敲一个字就是一个请求，中文输入法下几乎全是废请求
  const [sessQuery, setSessQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  // 当前会话 id 用 ref 同步一份：state 更新是异步的，
  // "新建会话后立刻提问"这种时序下 state 还没落地，发出去的就是空 session_id，
  // 后端会另建一个会话，上下文当场断掉。ref 是同步的，能避免这个竞态。
  const sessionRef = useRef('')
  // 搜索词的同步副本：createSession / deleteSession / 答完刷新 这些回调里
  // 都要"保持当前过滤条件再刷一次列表"，但它们不该为了读到搜索词而重建
  // （重建会连带触发首屏 effect，把界面清一遍）
  const queryRef = useRef('')

  useEffect(() => {
    api.novels().then((r) => {
      // 只要已经有切片就能检索，未跑完的书也允许边问边等
      const ready = r.items.filter((n) => n.chunk_count > 0 || ['READY', 'VECTORIZING'].includes(n.status))
      setNovels(ready)
      if (!novelId && ready.length) onPick(ready[0].id)
    })
  }, [])

  // ---- 会话列表：按书加载 ----
  // `q` 有值时后端会按标题 + 正文过滤；不传就取全量
  const loadSessions = useCallback(
    async (nid: string, q = ''): Promise<ChatSession[]> => {
      if (!nid) {
        setSessions([])
        return []
      }
      setSessLoading(true)
      try {
        const r = await api.chatSessions(nid, q)
        setSessions(r.items || [])
        return r.items || []
      } catch {
        setSessions([])
        return []
      } finally {
        setSessLoading(false)
      }
    },
    [],
  )

  // 当前会话写进 URL：刷新、分享链接、浏览器前进后退都能回到原对话。
  // 用 replaceState 而不是 pushState —— 切会话不该把历史栈塞满。
  const syncUrl = useCallback((sid: string) => {
    const u = new URL(window.location.href)
    if (sid) u.searchParams.set('session', sid)
    else u.searchParams.delete('session')
    window.history.replaceState(null, '', u.toString())
  }, [])

  // 切会话 → 拉历史消息回填聊天区
  const openSession = useCallback(async (sid: string) => {
    sessionRef.current = sid
    setSessionId(sid)
    syncUrl(sid)
    setChats([])
    setCiteMap({})
    setThinkMap({})
    setRetMap({})
    setUsageMap({})
    setActiveId('')
    try {
      const r = await api.chatSessionMessages(sid)
      const msgs: Message[] = []
      const cites: Record<string, Citation[]> = {}
      let lastWithCites = ''
      for (const m of r.items || []) {
        msgs.push({
          id: m.id,
          role: m.role,
          content: m.content,
          createAt: new Date(m.created_at).getTime(),
          status: 'complete',
          // 后端存的是单列枚举，Semi 要两个布尔。回放时必须映射回去，
          // 否则刷新一次用户点过的赞就"消失"了——数据还在，只是图标不亮
          like: m.feedback === 'up',
          dislike: m.feedback === 'down',
        } as Message)
        if (m.role === 'assistant' && m.citations?.length) {
          cites[m.id] = m.citations
          lastWithCites = m.id
        }
      }
      setChats(msgs)
      setCiteMap(cites)
      // 历史回放没有"本轮"的账（按消息存 token 没意义、也没存），但"这个会话
      // 一共花了多少"是有的。挂到最后一条答案上，刷新或重开对话时不会以为什么都没记。
      const lastAns = [...msgs].reverse().find((m) => m.role === 'assistant' && m.id)
      const lastAnsId = String(lastAns?.id || '')
      if (lastAnsId && r.usage && (r.usage.total_tokens || 0) > 0) {
        setUsageMap({ [lastAnsId]: { total: r.usage } })
      }
      // 打开会话就展示最后一条答案的出处：否则右侧面板永远是"提问后这里显示…"，
      // 用户得先点进消息才知道当初引了哪些原文。activeIdx 回到 -1（不预选某一条）
      if (lastWithCites) {
        setActiveId(lastWithCites)
        setActiveIdx(-1)
      }
    } catch {
      // 会话已被删或网络异常：保持空界面，不要把错误抛给用户；
      // 同时把 URL 里的失效 session 去掉，否则每次刷新都会再试一次
      setChats([])
      syncUrl('')
    }
  }, [syncUrl])

  // 首屏 / 切书：清空界面 → 拉会话列表 → 若 URL 带了 session 且确实属于这本书，恢复它。
  // 声明在 openSession 之后：effect 的依赖数组在 render 期求值，提前引用会撞 TDZ。
  useEffect(() => {
    setChats([])
    setCiteMap({})
    setThinkMap({})
    setRetMap({})
    setMemMap({})
    setUsageMap({})
    setActiveId('')
    sessionRef.current = ''
    setSessionId('')
    void (async () => {
      const list = await loadSessions(novelId, queryRef.current)
      const want = new URLSearchParams(window.location.search).get('session')
      if (!novelId || !want) return
      if (!list.some((s) => s.id === want)) {
        // 会话已删、或 URL 里带的是别的书的会话：清掉参数，别每次刷新都试
        syncUrl('')
        return
      }
      await openSession(want)
    })()
  }, [novelId, loadSessions, openSession, syncUrl])

  // 搜索防抖：300ms 内的连续输入只发一次请求。
  // 首次跳过 —— 上面那个 effect 已经用同一份 query 拉过了，不跳过会开屏双请求。
  const queryInited = useRef(false)
  useEffect(() => {
    if (!queryInited.current) {
      queryInited.current = true
      return
    }
    const t = window.setTimeout(() => {
      void loadSessions(novelId, sessQuery)
    }, 300)
    return () => window.clearTimeout(t)
  }, [sessQuery, novelId, loadSessions])

  const createSession = useCallback(async () => {
    if (!novelId) return
    try {
      const s = await api.chatSessionCreate(novelId)
      await loadSessions(novelId, queryRef.current)
      await openSession(s.id)
    } catch {
      // 忽略：下一条消息会隐式建会话
    }
  }, [novelId, loadSessions, openSession])

  const renameSession = useCallback(
    async (id: string, title: string) => {
      try {
        await api.chatSessionRename(id, title)
        await loadSessions(novelId, queryRef.current)
      } catch {
        // 忽略
      }
    },
    [novelId, loadSessions],
  )

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await api.chatSessionDelete(id)
      } catch {
        // 忽略
      }
      if (id === sessionRef.current) {
        // 删掉当前会话 → 回到空界面
        sessionRef.current = ''
        setSessionId('')
        syncUrl('')
        setChats([])
        setCiteMap({})
        setThinkMap({})
        setRetMap({})
        setMemMap({})
        setUsageMap({})
        setActiveId('')
      }
      await loadSessions(novelId, queryRef.current)
    },
    [novelId, loadSessions, syncUrl],
  )

  const send = async (text: string, opts?: { regenerate?: boolean }) => {
    const q = (text || '').trim()
    if (!q || !novelId || loading) return
    const regenerate = !!opts?.regenerate

    setLoading(true)
    const aid = `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    // 服务端真实 id 在 done 事件里才拿到；在此之前所有 UI 都挂本地临时 id。
    // 用独立变量而不是就地改 aid：这些 setState 都是闭包，改 const 做不到。
    let answerId = aid
    setActiveId(aid)
    setActiveIdx(-1)
    // 用户消息由 Chat 内部追加并通过 onChatsChange 回传，这里只补占位的助手消息。
    // 重新生成走的是程序化调用、不经过输入框，所以不会多出一条用户消息。
    setChats((prev) => [
      ...prev,
      { id: aid, role: 'assistant', content: '', createAt: Date.now(), status: 'loading' as const },
    ])

    // ---- 思考过程的流式缓冲 ----
    // 从"点发送"就开始有反馈：先挂一个 retrieving 占位，meta 到了转 thinking
    const think = { text: '', phase: 'retrieving' as ThinkPhase, ms: undefined as number | undefined }
    let timer: number | null = null
    const flush = () => {
      // 同上：updater 里不要直接读 answerId，它会在 done 时被改写
      const key = answerId
      setThinkMap((prev) => ({
        ...prev,
        [key]: { text: think.text, phase: think.phase, ms: think.ms },
      }))
    }
    const scheduleFlush = () => {
      if (timer != null) return
      timer = window.setTimeout(() => {
        timer = null
        flush()
      }, 90)
    }
    const stopFlush = () => {
      if (timer != null) {
        window.clearTimeout(timer)
        timer = null
      }
    }
    flush()

    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      await streamSSE(
        '/api/v1/chat',
        {
          novel_id: novelId,
          question: q,
          session_id: sessionRef.current || null,
          regenerate,
        },
        (event, data) => {
          if (event === 'meta') {
            // 在这里就把临时 id 换成服务端预建的 id。换的时机很关键：
            // 此刻正文还是 0 个字，Semi 按 `key={item.id}` 重新挂载气泡时
            // 没有任何内容会丢；等 done 再换的话，正好是"答完那一刻"重建，
            // 正文会先空一帧再出现，看起来像闪了一下。
            const realId = String(data.message_id || '')
            if (realId && realId !== answerId) {
              const prevId = answerId
              setChats((prev) => prev.map((m) => (m.id === prevId ? { ...m, id: realId } : m)))
              setActiveId((cur) => (cur === prevId ? realId : cur))
              moveKey(prevId, realId)
              answerId = realId
            }
            setCiteMap((prev) => ({ ...prev, [answerId]: data.citations || [] }))
            setModeMap((prev) => ({ ...prev, [answerId]: (data.mode || 'hybrid') as RetrievalMode }))
            setRetMap((prev) => ({
              ...prev,
              [answerId]: {
                mode: data.mode || 'hybrid',
                dense: data.retrieval?.dense,
                lexical: data.retrieval?.lexical,
                parents: data.retrieval?.parents,
                reranked: data.retrieval?.reranked,
                graphFacts: (data.graph_facts || []).length,
                degraded: data.degraded,
              },
            }))
            if (data.session_id) {
              sessionRef.current = data.session_id
              setSessionId(data.session_id)
            }
            // 上下文命中情况：显示"接住了上文"的实体，让用户能确认记忆生效
            const mem = data.memory || {}
            setMemMap((prev) => ({
              ...prev,
              [answerId]: {
                turns: Number(mem.turns || 0),
                entities: Array.isArray(mem.entities) ? mem.entities : [],
              },
            }))
            think.phase = 'thinking'
            scheduleFlush()
          } else if (event === 'reasoning') {
            if (think.phase === 'retrieving') think.phase = 'thinking'
            think.text += String(data.text || '')
            scheduleFlush()
          } else if (event === 'delta') {
            if (think.phase === 'thinking') {
              // 正文一到就收合思考区，把版面让给答案
              think.phase = 'answering'
              stopFlush()
              flush()
            }
            {
              const key = answerId
              const piece = String(data.text || '')
              setChats((prev) =>
                prev.map((m) =>
                  m.id === key
                    ? {
                        ...m,
                        content: String(m.content || '') + piece,
                        // **正文能不能边流边显示，全看这一行。**
                        // Semi 的 ChatBoxContent 拿 `status === 'loading'` 当作
                        // "还在等第一段内容"，这时它只画一个转圈点、把 content
                        // 整个丢掉不渲染。我们此前一直挂 loading 到整段答完，
                        // 于是 content 一到就累加进 state、却一个字都上不了屏，
                        // 直到 done 才一次性铺出来——用户看到的就是"不是流式"。
                        // 首字到达即切 incomplete：内容立刻可见，而操作图标行
                        // 仍按"未完成"隐藏（ChatBoxAction 只认 complete）。
                        status: (
                          m.status === 'loading' ? 'incomplete' : m.status
                        ) as Message['status'],
                      }
                    : m,
                ),
              )
            }
          } else if (event === 'done') {
            think.phase = 'done'
            think.ms = data.reasoning_ms ?? think.ms
            // 先把思考区定版，再换 id —— 留着 pending 的 flush 会在换完 id 之后
            // 又往旧 key 写一次，把刚清掉的临时 id 重新塞回 Map
            stopFlush()
            flush()
            // 兜底：正常情况下 meta 阶段已经换过 id，这里 realId 与 answerId
            // 相同、整段跳过。留着是为了预建失败（meta.message_id 为空）的
            // 退化路径 —— 那时仍然只能在这轮结束时补换，虽然会闪一下，
            // 但总比"点赞 / 删除必定 404"强
            const realId = String(data.message_id || '')
            if (realId && realId !== answerId) {
              // 必须把旧 id 存成常量再进 updater。updater 是在 React 渲染阶段
              // 才执行的，那时 answerId 已经赋值成 realId 了 —— 直接写
              // `m.id === answerId` 一条都匹配不上，消息会永远停在 loading
              // （表现就是"答完了但图标栏是灰的、点赞/删除全 404"）
              const prevId = answerId
              setChats((prev) => prev.map((m) => (m.id === prevId ? { ...m, id: realId } : m)))
              setActiveId((cur) => (cur === prevId ? realId : cur))
              moveKey(prevId, realId)
              answerId = realId
            }
            // 消耗在 done 里就拿到了，不用等下面的列表刷新：
            // 列表刷新只更新侧栏的累计数，气泡里显示的是这一轮自己的账
            if (data.usage) {
              const useId = answerId
              setUsageMap((prev) => ({ ...prev, [useId]: data.usage as RoundUsage }))
            }
          } else if (event === 'error') {
            think.phase = 'done'
            stopFlush()
            flush()
            const key = answerId
            setChats((prev) =>
              prev.map((m) =>
                m.id === key
                  ? { ...m, content: String(m.content || '') || `生成失败：${data.message || '未知错误'}`, status: 'error' as const }
                  : m,
              ),
            )
          }
        },
        ctrl.signal,
      )
      {
        // done 分支已经把 id 换成服务端的了，这里按最新的匹配
        const key = answerId
        setChats((prev) =>
          prev.map((m) =>
            m.id === key ? { ...m, status: (m.content ? 'complete' : 'error') as 'complete' | 'error' } : m,
          ),
        )
      }
      // 落库完成 → 刷新会话列表（标题/条数/时间都变了）
      void loadSessions(novelId, queryRef.current)
    } catch (e: any) {
      const aborted = e?.name === 'AbortError'
      setChats((prev) =>
        prev.map((m) =>
          m.id === answerId
            ? {
                ...m,
                status: (aborted ? 'incomplete' : 'error') as 'incomplete' | 'error',
                content: String(m.content || '') || (aborted ? '（已停止生成）' : `生成失败：${e.message || e}`),
              }
            : m,
        ),
      )
      // 中途停止/出错：思考区定版，别一直转圈
      think.phase = 'done'
    } finally {
      stopFlush()
      flush()
      setLoading(false)
      abortRef.current = null
    }
  }

  const citations = citeMap[activeId] || []
  const retMode = modeMap[activeId]

  // 最后一条答案是唯一能"重新生成"的目标：更早的答案已被后续对话引用，
  // 重答它而不重答后面的问题，上下文会自相矛盾。Semi 的消息栏自己按
  // `lastChat && role === assistant` 判断，这里不用再算一遍。

  /** 把各 Map 里 from 的条目改名成 to。
   *
   * `done` 事件会把消息 id 从本地临时值换成服务端真值，所有以 id 为 key 的
   * 状态都得跟着搬——不搬的话引用 / 用量 / 思考面板会原地"失联"，
   * 同一条答案在界面里等于变成了两个互不相干的 id。
   */
  const moveKey = (from: string, to: string) => {
    if (!from || !to || from === to) return
    const mv = <T,>(p: Record<string, T>): Record<string, T> => {
      if (!(from in p)) return p
      const next = { ...p }
      next[to] = p[from]
      delete next[from]
      return next
    }
    setCiteMap(mv)
    setUsageMap(mv)
    setThinkMap(mv)
    setRetMap(mv)
    setMemMap(mv)
  }

  /** 清掉某条消息在各类附属 Map 里的条目（引用/用量/思考/检索/记忆）。
   *
   * 消息被删或重新生成后旧 id 就没人再引用，不清就是永久泄漏——
   * 重新生成会换一个新 id，旧条目没有任何机会被回收。
   */
  const dropMsg = (mid: string) => {
    setCiteMap((p) => omit(p, mid))
    setUsageMap((p) => omit(p, mid))
    setThinkMap((p) => omit(p, mid))
    setRetMap((p) => omit(p, mid))
    setMemMap((p) => omit(p, mid))
  }

  const regenerate = (aid: string) => {
    if (loading) return
    // 找不到就退回"最后一条"：⟳ 图标本身只挂在最后一条答案上。
    // 而且 Semi 的 resetMessage 会先把最后一条换成 loading 占位再回调
    // （换了新 id），所以按 id 找不到是正常情况，不能直接放弃。
    const found = chats.findIndex((m) => String(m.id || '') === aid)
    const cut = found >= 0 ? found : chats.length - 1
    if (cut < 0) return
    let q = ''
    for (let i = cut - 1; i >= 0; i--) {
      if (chats[i].role === 'user') {
        q = String(chats[i].content || '')
        break
      }
    }
    if (!q.trim()) return
    // 截到这条答案之前：它后面的消息理论上不存在（只有最后一条才有入口），
    // 但万一有，也一并丢掉——否则会出现"答案 A 被换掉、基于 A 的追问还留着"
    setChats((prev) => prev.slice(0, cut))
    // 旧答案的附属状态要一起清掉：不清的话这些 Map 按消息 id 只增不减，
    // 而重新生成会换一个新 id，旧条目永远没人回收
    dropMsg(aid)
    void send(q, { regenerate: true })
  }

  /** 点赞 / 点踩。Semi 负责图标状态，这里负责让它活过刷新。
   *
   * `next` 由调用方算好（Semi 传进来的是**切换前**的 message），
   * 空串表示取消：赞和踩是互斥的，后端只有一列 `feedback`。
   */
  const rate = async (mid: string, next: '' | 'up' | 'down') => {
    if (!mid) return
    try {
      await api.chatMessageFeedback(mid, next)
      Toast.success(next === 'up' ? '已标记为有用' : next === 'down' ? '已记录，会用来改进' : '已取消')
      void loadSessions(novelId, queryRef.current)
    } catch {
      Toast.error('评价没存上，请稍后重试')
    }
  }

  /** 删除单条消息（消息栏的垃圾桶）。 */
  const removeMessage = async (mid: string) => {
    if (!mid) return
    try {
      const r = await api.chatMessageDelete(mid)
      dropMsg(mid)
      // 删空了后端会连会话一起收掉，这时界面也得回到"没有会话"的状态，
      // 否则右上角还挂着 session_id，下一条消息会往一个已删的会话里塞
      if (r.session_deleted && sessionRef.current === r.session_id) {
        sessionRef.current = ''
        setSessionId('')
        syncUrl('')
        setChats([])
        setActiveId('')
      }
      Toast.success('已删除')
      void loadSessions(novelId, queryRef.current)
    } catch {
      Toast.error('删除失败，请稍后重试')
    }
  }

  return (
    <>
      <PageHead
        title="问答"
        desc="混合检索 + 图谱关系 → 回溯父块 → 流式生成；思考过程实时展开，答案带原文引用。"
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
          <div className="chat-sess">
            <SessionList
              sessions={sessions}
              activeId={sessionId}
              loading={sessLoading}
              query={sessQuery}
              onQuery={(v) => {
                queryRef.current = v
                setSessQuery(v)
              }}
              onSelect={(id) => void openSession(id)}
              onCreate={() => void createSession()}
              onRename={(id, t) => void renameSession(id, t)}
              onDelete={(id) => void deleteSession(id)}
              onExport={(id) => {
                // 直接跳 URL 让浏览器按 Content-Disposition 下载，
                // 不用 fetch+Blob（那还得自己处理中文文件名）
                window.location.href = api.chatSessionExportUrl(id)
              }}
            />
          </div>
          <div className="chat-main">
            <Chat
              style={{ height: '100%', width: '100%' }}
              chats={chats}
              onChatsChange={(list) => setChats(list || [])}
              onMessageSend={(content) => send(content)}
              // 消息栏那排图标（复制 / 赞 / 踩 / 刷新 / 删除）是答案最常用的操作，
              // 必须都接上真实现：不接回调 Semi 照样把按钮画出来，点下去毫无反应，
              // 用户分不清是"功能坏了"还是"根本没做"。复制由 Semi 内置（带 toast），
              // 其余四个各自落到后端。
              onMessageReset={(msg) => regenerate(String(msg?.id || ''))}
              onMessageGoodFeedback={(msg) =>
                void rate(String(msg?.id || ''), msg?.like ? '' : 'up')
              }
              onMessageBadFeedback={(msg) =>
                void rate(String(msg?.id || ''), msg?.dislike ? '' : 'down')
              }
              onMessageDelete={(msg) => void removeMessage(String(msg?.id || ''))}
              onHintClick={(hint) => send(hint)}
              hints={chats.length === 0 ? SUGGESTIONS : []}
              roleConfig={ROLE_CONFIG}
              placeholder="例如：第三章里谁救了主角？"
              enableUpload={false}
              showStopGenerate={loading}
              onStopGenerator={() => abortRef.current?.abort()}
              showClearContext
              onClear={() => {
                // 清空只清界面，不删后端记录 —— 用户可能只是想在当前会话里重新问，
                // 删数据要走侧栏的删除按钮
                sessionRef.current = ''
                setSessionId('')
                syncUrl('')
                setCiteMap({})
                setThinkMap({})
                setRetMap({})
                setMemMap({})
                setUsageMap({})
                setChats([])
              }}
              chatBoxRenderConfig={{
                renderChatBoxContent: ({ message, defaultContent }) => {
                  const mid = String(message?.id || '')
                  const think = mid ? thinkMap[mid] : undefined
                  const panel = think ? (
                    <ReasoningPanel
                      text={think.text}
                      phase={think.phase}
                      ms={think.ms}
                      retrieval={retMap[mid]}
                    />
                  ) : null
                  const cs = mid ? citeMap[mid] : undefined
                  const mem = mid ? memMap[mid] : undefined
                  const us = mid ? usageMap[mid] : undefined
                  if (message?.role !== 'assistant') return defaultContent
                  // 有思考面板 / 引用 / 用量，任一成立就走自定义渲染；
                  // 三者都没有就交回 Semi，别为了空壳多包一层 div
                  const hasRound = (us?.round?.total_tokens || 0) > 0
                  const hasUsage =
                    !!us && (hasRound || (us.total?.total_tokens || 0) > 0)
                  if (!panel && !cs?.length && !hasUsage) return defaultContent
                  return (
                    <div className="msg-body">
                      {panel}
                      {mem && mem.entities.length > 0 && (
                        <div className="mem-bar">
                          <span className="mem-label">承接上文</span>
                          {mem.entities.map((e) => (
                            <span key={e} className="mem-entity">
                              {e}
                            </span>
                          ))}
                          <span className="mem-note">
                            已用这些实体消歧检索（{mem.turns} 轮历史）
                          </span>
                        </div>
                      )}
                      {defaultContent}
                      {hasUsage && us && (
                        <div
                          className="tok-bar"
                          title={
                            (hasRound
                              ? `本轮输入 ${us.round?.prompt_tokens || 0} tok` +
                                `，输出 ${us.round?.completion_tokens || 0} tok` +
                                `（${us.round?.calls || 0} 次模型调用）\n`
                              : '') +
                            `本会话累计输入 ${us.total?.prompt_tokens || 0} tok` +
                            `，输出 ${us.total?.completion_tokens || 0} tok` +
                            `，共 ${us.total?.calls || 0} 次调用（含后台压缩摘要）`
                          }
                        >
                          {hasRound ? (
                            <>
                              <span className="tok-main">
                                本轮 {fmtTok(us.round?.total_tokens || 0)} tok
                              </span>
                              <span className="tok-sub">
                                入 {fmtTok(us.round?.prompt_tokens || 0)} · 出{' '}
                                {fmtTok(us.round?.completion_tokens || 0)}
                              </span>
                              <span className="tok-sep">|</span>
                            </>
                          ) : null}
                          <span className={hasRound ? 'tok-sub' : 'tok-main'}>
                            会话累计 {fmtTok(us.total?.total_tokens || 0)} tok
                          </span>
                          {us.total?.summary_chars ? (
                            <span
                              className="sess-sum"
                              title="早期回合已压缩成摘要，之后的提问按摘要 + 最近几轮原文理解"
                            >
                              摘要
                            </span>
                          ) : null}
                        </div>
                      )}
                      {cs && cs.length > 0 && (
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
                      )}
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
            {/* Resizable 只接受单个子元素，传数组会触发 React 的 children 类型警告 */}
            <div className="cite-inner">
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
            </div>
          </Resizable>
        </div>
      )}
    </>
  )
}

/** 不可变地删掉一个 key。用来清消息的附属状态——
 *  对象 Map 的条目不可能靠渲染回收，必须显式 delete。 */
function omit<T>(rec: Record<string, T>, key: string): Record<string, T> {
  if (!(key in rec)) return rec
  const next = { ...rec }
  delete next[key]
  return next
}
