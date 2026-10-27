import { useEffect, useMemo, useState } from 'react'
import { Banner, Card, Col, Empty, Row, Select, Table, Tag, Typography } from '@douyinfe/semi-ui'
import type { ColumnProps } from '@douyinfe/semi-ui/lib/es/table/interface'
import PageHead from '../components/PageHead'
import { api, fmtNum, Novel } from '../api/client'

function Stat({ label, value, foot }: { label: string; value: React.ReactNode; foot?: React.ReactNode }) {
  return (
    <Card bodyStyle={{ padding: '16px 18px' }} style={{ height: '100%' }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      <div className="stat-foot">{foot}</div>
    </Card>
  )
}

export default function VectorView({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [data, setData] = useState<any>(null)
  const [stats, setStats] = useState<any>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.novels().then((r) => {
      setNovels(r.items)
      if (!novelId && r.items.length) onPick(r.items[0].id)
    })
    api.stats().then(setStats).catch(() => {})
  }, [])

  useEffect(() => {
    if (!novelId) return
    api.vectorSample(novelId, 400).then(setData).catch((e) => setErr(String(e.message || e)))
  }, [novelId])

  const byChapter = useMemo(() => {
    const items: any[] = data?.items || []
    const map: Record<number, number> = {}
    for (const it of items) map[it.chapter_no] = (map[it.chapter_no] || 0) + 1
    return Object.entries(map)
      .slice(0, 40)
      .map(([k, v]) => ({ chapter: Number(k), count: v }))
  }, [data])

  const maxCount = Math.max(1, ...byChapter.map((b) => b.count))

  const columns: ColumnProps<any>[] = [
    {
      title: 'chunk_id',
      dataIndex: 'id',
      width: 280,
      render: (v: string) => <div className="mono ellipsis" style={{ maxWidth: 260 }}>{v}</div>,
    },
    { title: '章节', dataIndex: 'chapter_no', width: 80, render: (v: number) => <span className="mono">{v}</span> },
    { title: '序', dataIndex: 'seq', width: 64, render: (v: number) => <span className="mono">{v}</span> },
    {
      title: '层级',
      dataIndex: 'level',
      width: 76,
      render: (v: number) => (
        <Tag color={v === 1 ? 'grey' : 'blue'} type="light" size="small">
          {v === 1 ? '子' : '父'}
        </Tag>
      ),
    },
    {
      title: '字符区间',
      dataIndex: 'char_start',
      width: 140,
      render: (_: any, it: any) => (
        <span className="mono">
          {it.char_start}-{it.char_end}
        </span>
      ),
    },
    {
      title: 'parent',
      dataIndex: 'parent_id',
      render: (v: string) => <div className="mono ellipsis" style={{ maxWidth: 260 }}>{v || '-'}</div>,
    },
  ]

  return (
    <>
      <PageHead
        title="向量库"
        desc="查看 Milvus 集合状态与已写入的向量样本。二维投影散点图（UMAP）排在 P1。"
        extra={
          <Select
            value={novelId || undefined}
            onChange={(v) => onPick(String(v))}
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

      {err && <Banner type="danger" closeIcon={null} description={err} style={{ marginBottom: 16 }} />}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} xl={6}>
          <Stat
            label="后端"
            value={stats?.vector_backend || data?.backend || '-'}
            foot="Milvus 3.0 · COSINE"
          />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat label="全库向量" value={stats ? fmtNum(stats.vectors) : '-'} foot="跨所有小说" />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat label="本书样本" value={data ? data.count : '-'} foot="拉取上限 400 条" />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat
            label="向量维度"
            value={stats?.embedding_dim ?? data?.dim ?? '-'}
            foot={stats?.embedding_model?.split('/').pop() ?? '—'}
          />
        </Col>
      </Row>

      <Card title="按章节分布（样本）" style={{ marginTop: 16 }}>
        <div className="hist" style={{ height: 150 }}>
          {byChapter.length === 0 && (
            <Typography.Text type="tertiary">暂无数据</Typography.Text>
          )}
          {byChapter.map((b) => (
            <div className="hist-col" key={b.chapter} title={`第 ${b.chapter} 章：${b.count} 块`}>
              <span className="hist-n">{b.count}</span>
              <div className="hist-bar" style={{ height: `${(b.count / maxCount) * 100}%` }} />
              <span className="hist-x">{b.chapter}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="向量记录样本" style={{ marginTop: 16 }} bodyStyle={{ padding: 0 }}>
        <Table
          columns={columns}
          dataSource={(data?.items || []).slice(0, 30)}
          rowKey="id"
          size="small"
          pagination={false}
          empty={<Empty title="暂无向量" description="这本书还没有写入向量" />}
        />
      </Card>
    </>
  )
}
