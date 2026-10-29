import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Select,
  SideSheet,
  Table,
  Tag,
  Typography,
} from '@douyinfe/semi-ui'
import type { ColumnProps } from '@douyinfe/semi-ui/lib/es/table/interface'
import CardTitle from '../components/CardTitle'
import PageHead from '../components/PageHead'
import { usePoll } from '../hooks/usePoll'
import { api, Chunk, ChunkReport, fmtNum, Novel } from '../api/client'

const SEV_LABEL: Record<string, string> = { info: '提示', warn: '警告', error: '严重' }
const SEV_COLOR = { info: 'grey', warn: 'amber', error: 'red' } as const

const CODE_DESC: Record<string, string> = {
  NO_CHAPTER_STRUCTURE: '未识别到章节结构',
  CHAPTER_TOO_SHORT: '章节过短（疑似目录页）',
  CHAPTER_TOO_LONG: '章节过长（疑似漏切）',
  CHUNK_TOO_LONG: '块超出模型长度上限',
  CHUNK_TOO_SHORT: '块过短（信息量不足）',
  PARENT_MISSING: '父块无子块',
  OVERLAP_ANOMALY: '重叠异常',
  DUP_CHUNK: '重复块',
  ENCODING_SUSPECT: '编码疑似错误',
}

function Stat({
  label,
  value,
  unit,
  foot,
  children,
}: {
  label: string
  value: React.ReactNode
  unit?: string
  foot?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <Card bodyStyle={{ padding: '16px 18px' }} style={{ height: '100%' }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {value}
        {unit && <span className="stat-unit">{unit}</span>}
      </div>
      {children}
      <div className="stat-foot">{foot}</div>
    </Card>
  )
}

export default function ChunkReview({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [report, setReport] = useState<ChunkReport | null>(null)
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [total, setTotal] = useState(0)
  const [level, setLevel] = useState<number | undefined>(1)
  const [chapter, setChapter] = useState<string>('')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState<Chunk | null>(null)

  useEffect(() => {
    api.novels().then((r) => {
      setNovels(r.items)
      if (!novelId && r.items.length) onPick(r.items[0].id)
    })
  }, [])

  const novel = useMemo(() => novels.find((n) => n.id === novelId), [novels, novelId])

  const loadReport = useCallback(async () => {
    if (!novelId) return
    try {
      setReport(await api.chunkReport(novelId))
    } catch {
      /* 可能正在切片 */
    }
  }, [novelId])

  const loadChunks = useCallback(async () => {
    if (!novelId) return
    setLoading(true)
    try {
      const r = await api.chunks(novelId, {
        level,
        chapter_no: chapter ? Number(chapter) : undefined,
        page,
        page_size: 10,
      })
      setChunks(r.items)
      setTotal(r.total)
    } finally {
      setLoading(false)
    }
  }, [novelId, level, chapter, page])

  const running = novel && ['PROCESSING', 'PENDING', 'VECTORIZING'].includes(novel.status)

  useEffect(() => {
    loadReport()
    loadChunks()
  }, [loadReport, loadChunks])

  // 报告只在本书仍在处理时才值得刷；处理完（或切到后台）就停手
  usePoll(loadReport, 4000, !!running)

  const histMax = Math.max(1, ...(report?.token_hist || [1]))

  const issueColumns: ColumnProps<any>[] = [
    {
      title: '章节',
      dataIndex: 'chapter_no',
      width: 80,
      render: (v: number) => <span className="mono">{v || '-'}</span>,
    },
    {
      title: '级别',
      dataIndex: 'severity',
      width: 90,
      render: (v: string) => (
        <Tag color={SEV_COLOR[v as keyof typeof SEV_COLOR] || 'grey'} type="light" size="small">
          {SEV_LABEL[v] || v}
        </Tag>
      ),
    },
    {
      title: '说明',
      dataIndex: 'code',
      render: (v: string, it: any) => (
        <>
          <div>{CODE_DESC[v] || v}</div>
          <Typography.Text type="tertiary" size="small">
            {it.detail}
          </Typography.Text>
        </>
      ),
    },
    {
      title: '位置',
      dataIndex: 'char_start',
      width: 110,
      align: 'right',
      render: (v: number) => <span className="mono">@{v}</span>,
    },
  ]

  const chunkColumns: ColumnProps<Chunk>[] = [
    { title: '章', dataIndex: 'chapter_no', width: 64, render: (v: number) => <span className="mono">{v}</span> },
    { title: '序', dataIndex: 'seq', width: 56, render: (v: number) => <span className="mono">{v}</span> },
    {
      title: '层级',
      dataIndex: 'level',
      width: 74,
      render: (v: number) => (
        <Tag color={v === 0 ? 'blue' : 'grey'} type="light" size="small">
          {v === 0 ? '父' : '子'}
        </Tag>
      ),
    },
    {
      title: '内容',
      dataIndex: 'content',
      render: (v: string) => <div className="ellipsis" style={{ maxWidth: 620 }}>{v.slice(0, 90)}</div>,
    },
    {
      title: 'token',
      dataIndex: 'token_est',
      width: 80,
      align: 'right',
      render: (v: number) => <span className="mono">{v}</span>,
    },
  ]

  return (
    <>
      <PageHead
        title="切片完成度"
        desc="进度按「已切字符 / 总字符」计算 —— 块数在流式切分中事前未知，用它算会出现越跑越退的假进度。"
        extra={
          <Select
            value={novelId || undefined}
            onChange={(v) => {
              onPick(String(v))
              setPage(1)
            }}
            style={{ width: 260 }}
            placeholder="选择小说…"
          >
            {novels.map((n) => (
              <Select.Option key={n.id} value={n.id}>
                {n.title || n.filename}
              </Select.Option>
            ))}
          </Select>
        }
      />

      {!novelId && (
        <Card>
          <Empty title="先选择一本小说" description="左侧下拉里挑一本已入库的书" />
        </Card>
      )}

      {novelId && report && (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="切块完成度"
                value={(report.completeness * 100).toFixed(1)}
                unit="%"
                foot={`${fmtNum(report.covered_chars)} / ${fmtNum(report.char_count)} 字已覆盖`}
              >
                <div style={{ margin: '8px 0 2px' }}>
                  <div
                    style={{
                      height: 6,
                      borderRadius: 99,
                      background: 'var(--semi-color-fill-1)',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        width: `${report.completeness * 100}%`,
                        height: '100%',
                        background: 'var(--semi-color-success)',
                        transition: 'width .35s ease',
                      }}
                    />
                  </div>
                </div>
              </Stat>
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="切片数量"
                value={fmtNum(report.chunk_count)}
                foot={`父块 ${fmtNum(report.parent_count)} · 子块 ${fmtNum(report.chunk_count)}`}
              />
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="块长度"
                value={report.avg_tokens}
                unit="tok 均值"
                foot={`P95 ${report.p95_tokens} tok（上限 512）`}
              />
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="质量告警"
                value={report.issues.length}
                foot={`严重 ${report.issues_by_severity?.error || 0} · 警告 ${report.issues_by_severity?.warn || 0}`}
              />
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} xl={12}>
              <Card
                title={<CardTitle text="阶段进度" />}
                headerExtraContent={
                  <Button size="small" onClick={async () => { await api.rebuild(novelId); loadReport() }}>
                    重跑全流程
                  </Button>
                }
                style={{ height: '100%' }}
              >
                <div className="kv">
                  <span>解析分章</span>
                  <span>{report.chapter_count} 章</span>
                </div>
                <div className="kv">
                  <span>切块覆盖</span>
                  <span>{(report.completeness * 100).toFixed(1)}%</span>
                </div>
                <div className="kv">
                  <span>向量化</span>
                  <span>{report.chunk_count} 条</span>
                </div>
                <Typography.Paragraph type="tertiary" size="small" style={{ marginTop: 12, marginBottom: 0 }}>
                  {running
                    ? '任务进行中：数字每 4 秒刷新一次，可切到「数据上传」页看实时进度。'
                    : '已完成。若改了切块参数，点「重跑全流程」会先清空旧切片再重建，不会产生重复数据。'}
                </Typography.Paragraph>
              </Card>
            </Col>

            <Col xs={24} xl={12}>
              <Card title={<CardTitle text="子块长度分布（token 估算）" />} style={{ height: '100%' }}>
                {report.chunk_count === 0 ? (
                  <Empty title="还没有切片" description="切块完成后这里会显示长度分布" style={{ padding: '24px 0' }} />
                ) : (
                  <div className="hist">
                    {report.token_hist.map((v, i) => (
                      <div className="hist-col" key={i}>
                        <span className="hist-n">{v}</span>
                        <div className="hist-bar" style={{ height: `${(v / histMax) * 100}%` }} />
                        <span className="hist-x">{report.token_buckets[i]}+</span>
                      </div>
                    ))}
                  </div>
                )}
                <Typography.Paragraph type="tertiary" size="small" style={{ marginBottom: 0 }}>
                  落在 512 之后的块会被 embedding 截断，是需要处理的信号。
                </Typography.Paragraph>
              </Card>
            </Col>
          </Row>

          {report.issues.length > 0 && (
            <Card
              title={<CardTitle text={`质量告警（${report.issues.length}）`} />}
              headerExtraContent={
                <Typography.Text type="tertiary" size="small">
                  按章节 / 类型排序，前 200 条
                </Typography.Text>
              }
              style={{ marginTop: 16 }}
              bodyStyle={{ padding: 0 }}
            >
              <Table
                columns={issueColumns}
                dataSource={report.issues}
                rowKey={(it: any, i?: number) => `${it.code}-${it.chapter_no}-${i}`}
                size="small"
                pagination={{ pageSize: 8, showSizeChanger: false }}
              />
            </Card>
          )}

          {report.sample_chunks.length > 0 && (
            <Card title={<CardTitle text="人工抽检（每 20 章抽 1 块）" />} style={{ marginTop: 16 }}>
              <Row gutter={[16, 16]}>
                {report.sample_chunks.map((c) => (
                  <Col xs={24} xl={12} key={c.id}>
                    <div className="row-between" style={{ marginBottom: 6 }}>
                      <Typography.Text type="tertiary" size="small">
                        第 {c.chapter_no} 章 · 第 {c.seq} 块
                      </Typography.Text>
                      <Tag color="grey" type="light" size="small">
                        {c.token_est} tok
                      </Tag>
                    </div>
                    <div className="chunk-box">{c.content.slice(0, 500)}</div>
                  </Col>
                ))}
              </Row>
            </Card>
          )}

          <Card
            title={<CardTitle text={`切片浏览（${total}）`} />}
            headerExtraContent={
              <div className="row">
                <Select
                  value={level === undefined ? 'all' : String(level)}
                  onChange={(v) => {
                    setLevel(v === 'all' ? undefined : Number(v))
                    setPage(1)
                  }}
                  style={{ width: 110 }}
                  size="small"
                >
                  <Select.Option value="all">全部层级</Select.Option>
                  <Select.Option value="0">父块</Select.Option>
                  <Select.Option value="1">子块</Select.Option>
                </Select>
                <Input
                  style={{ width: 100 }}
                  size="small"
                  placeholder="章节号"
                  value={chapter}
                  onChange={(v) => {
                    setChapter(v)
                    setPage(1)
                  }}
                />
              </div>
            }
            style={{ marginTop: 16 }}
            bodyStyle={{ padding: 0 }}
          >
            <Table
              columns={chunkColumns}
              dataSource={chunks}
              loading={loading}
              rowKey="id"
              size="small"
              pagination={{
                currentPage: page,
                pageSize: 10,
                total,
                showSizeChanger: false,
                style: { marginBottom: 12 },
                onPageChange: (p) => setPage(p),
              }}
              empty={<Empty title="暂无切片" description="这本书还没有切块数据" />}
              onRow={(c) => ({ onClick: () => c && setOpen(c), style: { cursor: 'pointer' } })}
            />
          </Card>
        </>
      )}

      <SideSheet
        visible={!!open}
        onCancel={() => setOpen(null)}
        title={open ? `第 ${open.chapter_no} 章 · 第 ${open.seq} 块` : ''}
        width={680}
        bodyStyle={{ padding: '4px 20px 24px' }}
      >
        {open && (
          <>
            <Typography.Paragraph type="tertiary" size="small" className="mono">
              {open.id} · 字符 {open.char_start}-{open.char_end} · {open.token_est} tok
            </Typography.Paragraph>
            <div className="chunk-box" style={{ maxHeight: '62vh' }}>
              {open.content}
            </div>
            {open.parent_id && (
              <Typography.Paragraph type="tertiary" size="small" className="mono" style={{ marginTop: 12 }}>
                parent: {open.parent_id}
              </Typography.Paragraph>
            )}
          </>
        )}
      </SideSheet>
    </>
  )
}
