import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Banner,
  Button,
  Card,
  Progress,
  Select,
  Table,
  Tag,
  Toast,
  Typography,
} from '@douyinfe/semi-ui'
import type { ColumnProps } from '@douyinfe/semi-ui/lib/es/table/interface'
import { IconRefresh } from '@douyinfe/semi-icons'
import PageHead from '../components/PageHead'
import { usePoll } from '../hooks/usePoll'
import { api, Job, Novel, STAGE_LABEL, fmtNum } from '../api/client'

/** 任务种类：库里 kind 只有 ingest / rebuild / graph 三种，界面上要说人话。 */
const KIND_LABEL: Record<string, string> = {
  ingest: '入库',
  rebuild: '重算向量',
  graph: '图谱构建',
}
const KIND_COLOR: Record<string, string> = {
  ingest: 'blue',
  rebuild: 'cyan',
  graph: 'violet',
}

const STATUS_LABEL: Record<string, string> = {
  PENDING: '排队中',
  RUNNING: '运行中',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELED: '已取消',
}
const STATUS_COLOR: Record<string, string> = {
  PENDING: 'grey',
  RUNNING: 'blue',
  COMPLETED: 'green',
  FAILED: 'red',
  CANCELED: 'amber',
}

const isLive = (j: Job) => j.status === 'PENDING' || j.status === 'RUNNING'

/** 耗时：跑着的算到现在，跑完的算开始到结束。没开始过就返回 '-'。 */
function duration(j: Job): string {
  if (!j.started_at) return '-'
  const start = new Date(j.started_at).getTime()
  const end = j.finished_at ? new Date(j.finished_at).getTime() : Date.now()
  const sec = Math.max(0, Math.round((end - start) / 1000))
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m${sec % 60}s`
  return `${Math.floor(m / 60)}h${m % 60}m`
}

function when(iso?: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 任务实际产出：入库看块/向量，图谱看实体/关系，别统一显示一个没意义的数字。 */
function output(j: Job): string {
  if (j.kind === 'graph') {
    const s = j.stats || {}
    const e = s.entities ?? 0
    const r = s.relations ?? 0
    return e || r ? `${fmtNum(e)} 实体 · ${fmtNum(r)} 关系` : '-'
  }
  const parts: string[] = []
  if (j.chunks_created) parts.push(`${fmtNum(j.chunks_created)} 块`)
  if (j.vectors_written) parts.push(`${fmtNum(j.vectors_written)} 向量`)
  return parts.join(' · ') || '-'
}

export default function Tasks({ novelId, onGo }: { novelId: string; onGo: (t: any, id?: string) => void }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [novels, setNovels] = useState<Novel[]>([])
  const [status, setStatus] = useState('')
  const [kind, setKind] = useState('')
  const [bookId, setBookId] = useState(novelId || '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [auto, setAuto] = useState(true)
  const autoRef = useRef(auto)
  autoRef.current = auto
  const jobsRef = useRef<Job[]>([])
  jobsRef.current = jobs
  // 单飞：同一时刻只允许一个请求在途，松手才发下一个
  const inflight = useRef(false)
  // 内容指纹：接口回来的数据没变就不 setState，省掉整页重渲染
  const sigRef = useRef('')

  const load = useCallback(() => {
    if (inflight.current) return
    inflight.current = true
    api
      .jobs({ novelId: bookId || undefined, status: status || undefined, kind: kind || undefined, limit: 100 })
      .then((r) => {
        const sig = JSON.stringify(r)
        if (sig !== sigRef.current) {
          sigRef.current = sig
          setJobs(r)
        }
        setErr('')
      })
      .catch((e) => setErr(String(e.message || e)))
      .finally(() => {
        inflight.current = false
      })
  }, [bookId, status, kind])

  useEffect(() => {
    api.novels().then((r) => setNovels(r.items)).catch(() => {})
  }, [])

  useEffect(() => {
    sigRef.current = '' // 筛选变化后强制接受新结果
    load()
  }, [load])

  // 自动刷新：只有存在活任务才轮询，切到后台自动停。
  // 判活用 ref 读最新列表——把 jobs 放依赖会形成自激循环（见 usePoll 注释）。
  usePoll(() => {
    if (autoRef.current && jobsRef.current.some(isLive)) load()
  }, 3000)

  const titleOf = (id: string) => {
    const n = novels.find((x) => x.id === id)
    return n ? n.title || n.filename : id
  }

  const doRetry = async (j: Job) => {
    setBusy(j.id)
    try {
      const r = await api.jobRetry(j.id)
      Toast.success(r.message || '已重新入队')
      load()
    } catch (e: any) {
      Toast.error(String(e.message || e))
    } finally {
      setBusy('')
    }
  }

  const doCancel = async (j: Job) => {
    setBusy(j.id)
    try {
      const r = await api.jobCancel(j.id)
      Toast.success(r.message || '已取消')
      load()
    } catch (e: any) {
      Toast.error(String(e.message || e))
    } finally {
      setBusy('')
    }
  }

  const counts = useMemo(() => {
    const m: Record<string, number> = { RUNNING: 0, PENDING: 0, FAILED: 0, COMPLETED: 0, CANCELED: 0 }
    for (const j of jobs) m[j.status] = (m[j.status] || 0) + 1
    return m
  }, [jobs])

  const columns: ColumnProps<Job>[] = [
    {
      title: '任务',
      dataIndex: 'id',
      width: 200,
      render: (v: string, j: Job) => (
        <div>
          <div className="mono ellipsis" style={{ maxWidth: 190 }}>
            {v}
          </div>
          <Typography.Text type="tertiary" size="small">
            {titleOf(j.novel_id)}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 100,
      render: (v: string) => (
        <Tag color={(KIND_COLOR[v] || 'grey') as any} type="light" size="small">
          {KIND_LABEL[v] || v}
        </Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      render: (v: string) => (
        <Tag color={(STATUS_COLOR[v] || 'grey') as any} type="light" size="small">
          {STATUS_LABEL[v] || v}
        </Tag>
      ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 190,
      render: (_: any, j: Job) => (
        <div>
          <Progress
            percent={Number((j.progress || 0).toFixed(1))}
            showInfo={false}
            size="small"
            stroke={
              j.status === 'FAILED'
                ? 'var(--semi-color-danger)'
                : j.status === 'CANCELED'
                  ? 'var(--semi-color-warning)'
                  : undefined
            }
          />
          <Typography.Text type="tertiary" size="small">
            {STAGE_LABEL[j.stage] || j.stage}
            {j.kind === 'graph' && j.chapters_total
              ? ` · ${j.chapters_done}/${j.chapters_total} 章`
              : ''}
          </Typography.Text>
        </div>
      ),
    },
    {
      title: '产出',
      dataIndex: 'stats',
      width: 190,
      render: (_: any, j: Job) => <span className="mono">{output(j)}</span>,
    },
    {
      title: '耗时',
      width: 90,
      render: (_: any, j: Job) => <span className="mono">{duration(j)}</span>,
    },
    {
      title: '创建',
      width: 96,
      render: (_: any, j: Job) => <span className="mono">{when(j.created_at)}</span>,
    },
    {
      title: '操作',
      width: 210,
      render: (_: any, j: Job) => (
        <div className="row" style={{ gap: 6 }}>
          {isLive(j) && (
            <Button
              size="small"
              type="danger"
              theme="borderless"
              loading={busy === j.id}
              onClick={() => doCancel(j)}
            >
              取消
            </Button>
          )}
          {(j.status === 'FAILED' || j.status === 'CANCELED') && (
            <Button size="small" theme="borderless" type="primary" loading={busy === j.id} onClick={() => doRetry(j)}>
              重试
            </Button>
          )}
          {j.kind === 'graph' && (
            <Button size="small" theme="borderless" onClick={() => onGo('graph', j.novel_id)}>
              看图谱
            </Button>
          )}
          {j.kind !== 'graph' && (
            <Button size="small" theme="borderless" onClick={() => onGo('chunks', j.novel_id)}>
              看切片
            </Button>
          )}
        </div>
      ),
    },
  ]

  return (
    <>
      <PageHead
        title="任务中心"
        desc="入库、重算向量、图谱构建三类任务共用一条串行队列。失败或取消的任务可以原地重试；跑着的任务可以取消，已写入的数据不会被清掉。"
        extra={
          <>
            <Button icon={<IconRefresh />} onClick={load}>
              刷新
            </Button>
            <Button theme={auto ? 'solid' : 'light'} onClick={() => setAuto((v) => !v)}>
              {auto ? '自动刷新中' : '自动刷新关'}
            </Button>
          </>
        }
      />

      {err && <Banner type="danger" closeIcon={null} description={`后端未连通：${err}`} style={{ marginBottom: 16 }} />}

      <Card bodyStyle={{ padding: '12px 16px' }} style={{ marginBottom: 16 }}>
        <div className="row" style={{ flexWrap: 'wrap', gap: 12 }}>
          <Select value={bookId} onChange={(v) => setBookId(String(v))} style={{ width: 240 }} placeholder="全部书籍">
            <Select.Option value="">全部书籍</Select.Option>
            {novels.map((n) => (
              <Select.Option key={n.id} value={n.id}>
                {n.title || n.filename}
              </Select.Option>
            ))}
          </Select>
          <Select value={status} onChange={(v) => setStatus(String(v))} style={{ width: 150 }}>
            <Select.Option value="">全部状态</Select.Option>
            <Select.Option value="PENDING,RUNNING">进行中</Select.Option>
            <Select.Option value="FAILED">失败</Select.Option>
            <Select.Option value="CANCELED">已取消</Select.Option>
            <Select.Option value="COMPLETED">已完成</Select.Option>
            <Select.Option value="FAILED,CANCELED">可重试</Select.Option>
          </Select>
          <Select value={kind} onChange={(v) => setKind(String(v))} style={{ width: 140 }}>
            <Select.Option value="">全部类型</Select.Option>
            <Select.Option value="ingest">入库</Select.Option>
            <Select.Option value="rebuild">重算向量</Select.Option>
            <Select.Option value="graph">图谱构建</Select.Option>
          </Select>
          <Typography.Text type="tertiary" size="small">
            运行中 {counts.RUNNING} · 排队 {counts.PENDING} · 失败 {counts.FAILED} · 取消 {counts.CANCELED} · 完成{' '}
            {counts.COMPLETED}
          </Typography.Text>
        </div>
      </Card>

      <Card bodyStyle={{ padding: 0 }}>
        <Table
          columns={columns}
          dataSource={jobs}
          rowKey="id"
          size="small"
          pagination={{ pageSize: 15, showSizeChanger: false }}
          empty={<Typography.Text type="tertiary">还没有任务，去「数据上传」传一本书</Typography.Text>}
        />
      </Card>
    </>
  )
}
