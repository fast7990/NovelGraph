// 后端接口封装。所有请求走 /api/v1，开发期由 vite proxy 转发到 8000。

const BASE = '/api/v1'

export type Novel = {
  id: string
  title: string
  filename: string
  file_size: number
  encoding: string
  char_count: number
  chapter_count: number
  chunk_count: number
  status: string
  created_at: string
}

export type Job = {
  id: string
  novel_id: string
  kind?: string
  status: string
  stage: string
  progress: number
  chars_done: number
  chars_total: number
  chapters_done: number
  chapters_total: number
  chunks_created: number
  vectors_written: number
  error: string
  stats: Record<string, any>
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
}

export type Citation = {
  chunk_id: string
  chapter_no: number
  title: string
  char_start: number
  char_end: number
  score: number
  snippet: string
  content?: string
  /** 这一块被哪几路召回、在各路排第几，如 {dense: 3, lexical: 1} */
  channels?: Record<string, number>
  rerank_score?: number | null
  rrf_score?: number
}

/** 检索模式：两路都有 → hybrid；只挂一路 → dense_only / sparse_only；都挂 → empty */
export type RetrievalMode = 'hybrid' | 'dense_only' | 'sparse_only' | 'empty'

export type Chunk = {
  id: string
  parent_id: string | null
  level: number
  chapter_no: number
  seq: number
  title: string
  content: string
  char_start: number
  char_end: number
  token_est: number
}

export type ReportIssue = {
  chapter_no: number
  code: string
  severity: 'info' | 'warn' | 'error'
  detail: string
  char_start: number
}

export type ChunkReport = {
  novel_id: string
  chapter_count: number
  chunk_count: number
  parent_count: number
  char_count: number
  covered_chars: number
  completeness: number
  avg_tokens: number
  p95_tokens: number
  token_hist: number[]
  token_buckets: number[]
  issues: ReportIssue[]
  issues_by_code: Record<string, number>
  issues_by_severity: Record<string, number>
  sample_chunks: Chunk[]
}

export type ChatSession = {
  id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  last_message_at: string | null
  /** 问答侧的 token 台账（图谱构建记在 job.stats 上，不在这里） */
  prompt_tokens?: number
  completion_tokens?: number
  llm_calls?: number
  /** 长对话是否已被压缩成摘要（>0 表示早期内容已折叠） */
  summary_chars?: number
  summary_seq?: number
}

export type ChatHistoryMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  seq: number
  created_at: string
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => fetch(`${BASE}/health`).then((r) => j<any>(r)),
  stats: () => fetch(`${BASE}/system/stats`).then((r) => j<any>(r)),

  novels: () => fetch(`${BASE}/novels`).then((r) => j<{ items: Novel[]; total: number }>(r)),
  novel: (id: string) => fetch(`${BASE}/novels/${id}`).then((r) => j<Novel>(r)),
  deleteNovel: (id: string) => fetch(`${BASE}/novels/${id}`, { method: 'DELETE' }).then((r) => j<any>(r)),

  upload: async (file: File, title: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    const res = await fetch(`${BASE}/novels`, { method: 'POST', body: form })
    return j<{ novel_id: string; job_id: string }>(res)
  },

  jobs: (params: { novelId?: string; status?: string; kind?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.novelId) q.set('novel_id', params.novelId)
    if (params.status) q.set('status', params.status)
    if (params.kind) q.set('kind', params.kind)
    q.set('limit', String(params.limit || 50))
    return fetch(`${BASE}/jobs?${q}`).then((r) => j<Job[]>(r))
  },
  job: (id: string) => fetch(`${BASE}/jobs/${id}`).then((r) => j<Job>(r)),
  jobRetry: (id: string) => fetch(`${BASE}/jobs/${id}/retry`, { method: 'POST' }).then((r) => j<any>(r)),
  jobCancel: (id: string) => fetch(`${BASE}/jobs/${id}/cancel`, { method: 'POST' }).then((r) => j<any>(r)),
  rebuild: (novelId: string) =>
    fetch(`${BASE}/novels/${novelId}/chunks/rebuild`, { method: 'POST' }).then((r) => j<any>(r)),

  chunks: (novelId: string, params: { level?: number; chapter_no?: number; page?: number; page_size?: number }) => {
    const q = new URLSearchParams()
    if (params.level !== undefined) q.set('level', String(params.level))
    if (params.chapter_no !== undefined) q.set('chapter_no', String(params.chapter_no))
    q.set('page', String(params.page || 1))
    q.set('page_size', String(params.page_size || 20))
    return fetch(`${BASE}/novels/${novelId}/chunks?${q}`).then((r) => j<{ items: Chunk[]; total: number }>(r))
  },
  chunkReport: (novelId: string) => fetch(`${BASE}/novels/${novelId}/chunk-report`).then((r) => j<ChunkReport>(r)),
  chapters: (novelId: string) =>
    fetch(`${BASE}/novels/${novelId}/chapters`).then((r) => j<{ items: any[] }>(r)),
  excerpt: (novelId: string, start: number, end: number) =>
    fetch(`${BASE}/novels/${novelId}/excerpt?char_start=${start}&char_end=${end}`).then((r) => r.text()),

  search: (novelId: string, query: string, topK = 8) =>
    fetch(`${BASE}/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ novel_id: novelId, query, top_k: topK }),
    }).then((r) => j<any>(r)),

  // ---- 会话（上下文记忆）----
  chatSessions: (novelId: string) =>
    fetch(`${BASE}/chat/sessions?novel_id=${novelId}`).then((r) => j<{ items: ChatSession[] }>(r)),
  chatSessionCreate: (novelId: string) =>
    fetch(`${BASE}/chat/sessions?novel_id=${novelId}`, { method: 'POST' }).then((r) => j<ChatSession>(r)),
  chatSessionMessages: (sessionId: string) =>
    fetch(`${BASE}/chat/sessions/${sessionId}/messages`).then((r) =>
      j<{
        items: ChatHistoryMessage[]
        /** 会话累计用量：重新打开旧对话时气泡里也要能回显总消耗 */
        usage?: {
          prompt_tokens?: number
          completion_tokens?: number
          total_tokens?: number
          calls?: number
          summary_seq?: number
          summary_chars?: number
        }
      }>(r),
    ),
  chatSessionRename: (sessionId: string, title: string) =>
    fetch(`${BASE}/chat/sessions/${sessionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }).then((r) => j<{ id: string; title: string }>(r)),
  chatSessionDelete: (sessionId: string) =>
    fetch(`${BASE}/chat/sessions/${sessionId}`, { method: 'DELETE' }).then((r) => j<any>(r)),

  vectorSample: (novelId: string, limit = 400) =>
    fetch(`${BASE}/novels/${novelId}/vectors/sample?limit=${limit}`).then((r) => j<any>(r)),

  // ---- 图谱（P1-M5）----
  graphOverview: (novelId: string) =>
    fetch(`${BASE}/graph/overview?novel_id=${novelId}`).then((r) => j<GraphOverview>(r)),
  graphEntities: (
    novelId: string,
    params: { q?: string; type?: string; page?: number; page_size?: number; order?: string } = {},
  ) => {
    const p = new URLSearchParams({ novel_id: novelId })
    if (params.q) p.set('q', params.q)
    if (params.type) p.set('type', params.type)
    if (params.order) p.set('order', params.order)
    p.set('page', String(params.page || 1))
    p.set('page_size', String(params.page_size || 30))
    return fetch(`${BASE}/graph/entities?${p}`).then((r) => j<{ items: GraphEntity[]; total: number }>(r))
  },
  graphEntity: (novelId: string, id: string) =>
    fetch(`${BASE}/graph/entities/${id}?novel_id=${novelId}`).then((r) => j<GraphEntityDetail>(r)),
  graphSubgraph: (novelId: string, params: { center?: string; hops?: number; include_pending?: boolean } = {}) => {
    const p = new URLSearchParams({ novel_id: novelId })
    if (params.center) p.set('center', params.center)
    if (params.hops) p.set('hops', String(params.hops))
    if (params.include_pending) p.set('include_pending', 'true')
    return fetch(`${BASE}/graph/subgraph?${p}`).then((r) => j<GraphSubgraph>(r))
  },
  graphRelations: (novelId: string, params: { status?: string; page?: number; page_size?: number } = {}) => {
    const p = new URLSearchParams({ novel_id: novelId })
    if (params.status) p.set('status', params.status)
    p.set('page', String(params.page || 1))
    p.set('page_size', String(params.page_size || 30))
    return fetch(`${BASE}/graph/relations?${p}`).then((r) => j<{ items: GraphRelationRow[]; total: number }>(r))
  },
  graphStatus: (novelId: string) =>
    fetch(`${BASE}/graph/status?novel_id=${novelId}`).then((r) => j<GraphStatus>(r)),
  graphBuild: (novelId: string, force = false) =>
    fetch(`${BASE}/graph/build?novel_id=${novelId}&force=${force}`, { method: 'POST' }).then((r) =>
      j<{ job_id: string | null; reused: boolean; message: string }>(r),
    ),
}

export type GraphEntity = {
  id: string
  name: string
  type: string
  importance: number
  mention_count: number
  first_chapter: number
  desc: string
  aliases: string[]
}

export type GraphEdgeLine = {
  id: string
  src: string
  dst: string
  relation: string
  strength: number
  confidence: number
  status: string
  chapter_no: number
}

export type GraphSubgraph = {
  nodes: GraphEntity[]
  edges: GraphEdgeLine[]
  center: string | null
  truncated: boolean
  node_total: number
}

export type GraphOverview = {
  entities: number
  relations: number
  pending_relations: number
  mentions: number
  by_type: { type: string; count: number; mentions: number }[]
  by_relation: { relation: string; count: number }[]
  top_entities: GraphEntity[]
}

export type GraphEntityDetail = GraphEntity & {
  relations: {
    id: string
    direction: 'in' | 'out'
    other_id: string
    other_name: string
    relation: string
    raw_relation: string
    strength: number
    confidence: number
    status: string
    evidence: string
    chapter_no: number
    chunk_id: string | null
  }[]
  /** 该实体的关系总数。可能大于 relations.length（后端有条数上限） */
  relation_total: number
  chapters: { chapter_no: number; count: number }[]
}

export type GraphRelationRow = {
  id: string
  src_id: string
  src_name: string
  dst_id: string
  dst_name: string
  relation: string
  raw_relation: string
  strength: number
  confidence: number
  status: string
  evidence: string
  chapter_no: number
  chunk_id: string | null
}

export type GraphStatus = {
  has_graph: boolean
  built: { entities: number; relations: number; built_at: string } | null
  job: {
    id: string
    status: string
    stage: string
    progress: number
    chapters_done: number
    chapters_total: number
    error: string
    stats: Record<string, any>
    running: boolean
  } | null
}

/** SSE over POST：EventSource 不支持 POST，只能手撸流解析。 */
export async function streamSSE(
  url: string,
  body: any,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const blocks = buf.split('\n\n')
    buf = blocks.pop() || ''
    for (const block of blocks) {
      let event = 'message'
      const dataLines: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue
      try {
        onEvent(event, JSON.parse(dataLines.join('\n')))
      } catch {
        onEvent(event, dataLines.join('\n'))
      }
    }
  }
}

export function fmtNum(n: number | undefined | null): string {
  if (n === undefined || n === null) return '-'
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万'
  return String(n)
}

export function fmtBytes(n: number): string {
  if (!n) return '-'
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

export const STAGE_LABEL: Record<string, string> = {
  PENDING: '排队中',
  PARSING: '解析文本',
  CHUNKING: '切分章节',
  EMBEDDING: '向量化',
  INDEXING: '写入索引',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELED: '已取消',
  VECTORIZING: '向量化中',
  READY: '就绪',
  PROCESSING: '处理中',
  GRAPH_EXTRACTING: '图谱抽取',
  GRAPH_WRITING: '图谱写入',
}
