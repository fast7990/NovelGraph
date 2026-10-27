import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Banner,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Popconfirm,
  Progress,
  Row,
  Table,
  Tag,
  Typography,
} from '@douyinfe/semi-ui'
import { IconUpload } from '@douyinfe/semi-icons'
import type { ColumnProps } from '@douyinfe/semi-ui/lib/es/table/interface'
import PageHead from '../components/PageHead'
import { api, fmtBytes, fmtNum, Job, Novel, STAGE_LABEL } from '../api/client'

const STATUS_COLOR = {
  READY: 'green',
  FAILED: 'red',
  PENDING: 'grey',
  PROCESSING: 'blue',
  VECTORIZING: 'blue',
} as const

export default function Upload({ onGo }: { onGo: (t: any, id?: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [jobs, setJobs] = useState<Record<string, Job>>({})
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [hot, setHot] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [info, setInfo] = useState<any>(null)
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
    // 入库参数只在进入页面时取一次，不跟着进度轮询
    api.stats().then(setInfo).catch(() => {})
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

  const remove = async (n: Novel) => {
    try {
      await api.deleteNovel(n.id)
      await refresh()
    } catch (e: any) {
      setErr(String(e.message || e))
    }
  }

  const activeCount = Object.values(jobs).filter(
    (j) => j.status === 'RUNNING' || j.status === 'PENDING',
  ).length

  const columns: ColumnProps<Novel>[] = [
    {
      title: '小说',
      dataIndex: 'title',
      width: 260,
      render: (_: any, n: Novel) => (
        <>
          <div style={{ fontWeight: 500 }}>{n.title || n.filename}</div>
          <Typography.Text type="tertiary" size="small">
            {n.filename} · {fmtBytes(n.file_size)} · {n.encoding || '?'}
          </Typography.Text>
        </>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (v: string, n: Novel) => {
        const job = jobs[n.id]
        const running = job && (job.status === 'RUNNING' || job.status === 'PENDING')
        return (
          <>
            <Tag color={STATUS_COLOR[v as keyof typeof STATUS_COLOR] || 'grey'} type="light" size="small">
              {STAGE_LABEL[v] || v}
            </Tag>
            {job?.stage && running && (
              <div>
                <Typography.Text type="tertiary" size="small">
                  {STAGE_LABEL[job.stage] || job.stage}
                </Typography.Text>
              </div>
            )}
          </>
        )
      },
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 210,
      render: (_: any, n: Novel) => {
        const job = jobs[n.id]
        const running = job && (job.status === 'RUNNING' || job.status === 'PENDING')
        const pct = job?.status === 'COMPLETED' ? 100 : job?.progress || 0
        const text = running && job
          ? `${pct.toFixed(0)}% · ${fmtNum(job.chars_done)}/${fmtNum(job.chars_total || n.char_count)} 字`
          : job?.status === 'FAILED'
            ? '任务失败'
            : job
              ? `${pct.toFixed(0)}%`
              : '-'
        return (
          <>
            <Progress
              percent={Number(Math.min(pct, 100).toFixed(0))}
              showInfo={false}
              size="small"
              stroke={job?.status === 'COMPLETED' ? 'var(--semi-color-success)' : 'var(--semi-color-primary)'}
            />
            <Typography.Text type="tertiary" size="small">
              {text}
            </Typography.Text>
          </>
        )
      },
    },
    {
      title: '字数',
      dataIndex: 'char_count',
      width: 92,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtNum(v)}</span>,
    },
    {
      title: '章节',
      dataIndex: 'chapter_count',
      width: 76,
      align: 'right',
      render: (v: number) => <span className="mono">{v || '-'}</span>,
    },
    {
      title: '切片',
      dataIndex: 'chunk_count',
      width: 76,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtNum(v)}</span>,
    },
    {
      title: '向量',
      dataIndex: 'id',
      width: 76,
      align: 'right',
      render: (_: any, n: Novel) => (
        <span className="mono">{jobs[n.id] ? fmtNum(jobs[n.id].vectors_written) : '-'}</span>
      ),
    },
    {
      title: '操作',
      dataIndex: 'op',
      width: 190,
      render: (_: any, n: Novel) => (
        <div className="row" onClick={(e) => e.stopPropagation()}>
          <Button size="small" theme="borderless" type="primary" onClick={() => onGo('chunks', n.id)}>
            切片
          </Button>
          <Button size="small" theme="borderless" type="primary" onClick={() => onGo('chat', n.id)}>
            问答
          </Button>
          <Popconfirm
            title={`删除《${n.title || n.filename}》？`}
            content="切片与向量会一并清空，不可恢复"
            okType="danger"
            okText="删除"
            onConfirm={() => remove(n)}
          >
            <Button size="small" theme="borderless" type="danger">
              删除
            </Button>
          </Popconfirm>
        </div>
      ),
    },
  ]

  return (
    <>
      <PageHead
        title="数据上传"
        desc="支持 txt / md，自动识别 UTF-8 / GB18030。上传后立即触发解析、切块、向量化全流程。"
      />

      <Row gutter={[16, 16]} style={{ alignItems: 'stretch' }}>
        <Col xs={24} xl={15}>
          <Card style={{ height: '100%' }} bodyStyle={{ padding: 18 }}>
            <div
              className={`dropzone ${hot ? 'hot' : ''}`}
              onDragOver={(e) => {
                e.preventDefault()
                setHot(true)
              }}
              onDragLeave={() => setHot(false)}
              onDrop={onDrop}
              onClick={() => inputRef.current?.click()}
            >
              <div className="dropzone-icon">
                <IconUpload />
              </div>
              <h3>{busy ? '上传中…' : '拖入小说文件，或点击选择'}</h3>
              <p>单个文件 ≤ 200MB；建议先按章整理，纯文本效果最好</p>
              <input
                type="file"
                accept=".txt,.md,.text"
                hidden
                ref={inputRef}
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) doUpload(f)
                  e.target.value = ''
                }}
              />
            </div>
            {msg && (
              <Banner
                type="success"
                closeIcon={null}
                description={msg}
                style={{ marginTop: 12 }}
              />
            )}
            {err && (
              <Banner
                type="danger"
                closeIcon={null}
                description={err}
                style={{ marginTop: 12 }}
              />
            )}
          </Card>
        </Col>

        <Col xs={24} xl={9}>
          <Card title="入库参数" style={{ height: '100%' }}>
            <div style={{ marginBottom: 12 }}>
              <Typography.Text type="tertiary" size="small">
                书名（可选，默认取文件名）
              </Typography.Text>
              <Input
                value={title}
                placeholder="例如：长夜将明"
                onChange={(v) => setTitle(v)}
                style={{ marginTop: 6 }}
              />
            </div>
            <div className="kv">
              <span>切块粒度</span>
              <span>父块 1400 / 子块 400 字</span>
            </div>
            <div className="kv">
              <span>重叠</span>
              <span>60 字</span>
            </div>
            <div className="kv">
              <span>Embedding</span>
              <span>
                本地 ONNX · {info?.embedding_model?.split('/').pop() ?? '—'} ·{' '}
                {info?.embedding_dim ?? '—'} 维 · {info?.embedding_threads ?? '—'} 线程
              </span>
            </div>
            <div className="kv">
              <span>向量库</span>
              <span>
                PostgreSQL pgvector · {info?.vector_table ?? 'chunk_vectors'}
              </span>
            </div>
            {activeCount > 0 && (
              <Typography.Paragraph type="tertiary" size="small" style={{ marginTop: 12, marginBottom: 0 }}>
                队列中还有 {activeCount} 个任务在跑。入库是串行的 —— {info?.embedding_threads ?? 8}{' '}
                线程 CPU 上并行只会互相抢线程。
              </Typography.Paragraph>
            )}
          </Card>
        </Col>
      </Row>

      <Card style={{ marginTop: 16 }} bodyStyle={{ padding: 0 }} title="入库记录">
        <Table
          columns={columns}
          dataSource={novels}
          rowKey="id"
          pagination={false}
          size="small"
          empty={<Empty title="还没有数据" description="先上传一本试试" style={{ padding: 32 }} />}
          onRow={(n) => ({ onClick: () => n && onGo('chunks', n.id), style: { cursor: 'pointer' } })}
        />
      </Card>
    </>
  )
}
