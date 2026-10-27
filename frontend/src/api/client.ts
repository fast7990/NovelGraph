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

  jobs: (novelId?: string) =>
    fetch(`${BASE}/jobs${novelId ? `?novel_id=${novelId}` : ''}`).then((r) => j<Job[]>(r)),
  job: (id: string) => fetch(`${BASE}/jobs/${id}`).then((r) => j<Job>(r)),
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

  vectorSample: (novelId: string, limit = 400) =>
    fetch(`${BASE}/novels/${novelId}/vectors/sample?limit=${limit}`).then((r) => j<any>(r)),
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
  VECTORIZING: '向量化中',
  READY: '就绪',
  PROCESSING: '处理中',
}
