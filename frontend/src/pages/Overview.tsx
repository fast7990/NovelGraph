import { useCallback, useEffect, useState } from 'react'
import { Banner, Button, Card, Col, Progress, Row, Tag, Typography } from '@douyinfe/semi-ui'
import { IconArrowRight } from '@douyinfe/semi-icons'
import PageHead from '../components/PageHead'
import { usePoll } from '../hooks/usePoll'
import { api, fmtNum } from '../api/client'

const TAG_COLOR = { ok: 'green', degraded: 'amber', error: 'red', down: 'red' } as const
const TAG_TEXT: Record<string, string> = { ok: '正常', degraded: '降级', error: '不可用', down: '不可用' }

function Stat({
  label,
  value,
  unit,
  foot,
}: {
  label: string
  value: React.ReactNode
  unit?: string
  foot?: React.ReactNode
}) {
  return (
    <Card bodyStyle={{ padding: '16px 18px' }} style={{ height: '100%' }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">
        {value}
        {unit && <span className="stat-unit">{unit}</span>}
      </div>
      <div className="stat-foot">{foot}</div>
    </Card>
  )
}

export default function Overview({ onGo }: { onGo: (t: any, id?: string) => void }) {
  const [health, setHealth] = useState<any>(null)
  const [stats, setStats] = useState<any>(null)
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    api.health().then(setHealth).catch((e) => setErr(String(e.message || e)))
    api.stats().then(setStats).catch(() => {})
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // 健康/统计属于"看着舒心"的数据，页面在后台时没必要一直打
  usePoll(load, 5000)

  const cfg = health?.config || {}
  const deps: any[] = health?.deps || []
  const readyRate = stats?.chunks ? Math.min(100, (stats.vectors / stats.chunks) * 100) : 0

  return (
    <>
      <PageHead
        title="概览"
        desc="P0 闭环：上传 → 解析切块 → 本地向量化 → pgvector 写入 → 混合检索 → RAG 问答。知识图谱在 P1 接入。"
        extra={
          <>
            <Button onClick={() => onGo('chunks')}>切片完成度</Button>
            <Button theme="solid" onClick={() => onGo('chat')}>
              开始问答
            </Button>
          </>
        }
      />

      {err && (
        <Banner
          type="danger"
          closeIcon={null}
          description={`后端未连通：${err}`}
          style={{ marginBottom: 16 }}
        />
      )}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} xl={6}>
          <Stat label="小说数" value={stats?.novels ?? '-'} foot="已入库书籍" />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat label="切片总数" value={stats ? fmtNum(stats.chunks) : '-'} foot="父块 + 子块" />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat
            label="累计字数"
            value={stats ? fmtNum(stats.chars) : '-'}
            unit="字"
            foot="原始文本规模"
          />
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Stat
            label="向量条数"
            value={stats ? fmtNum(stats.vectors) : '-'}
            foot={stats?.vector_backend ? `${stats.vector_backend} 后端` : '—'}
          />
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} xl={14}>
          <Card
            title="依赖健康"
            headerExtraContent={
              <Typography.Text type="tertiary" size="small">
                每 5 秒刷新
              </Typography.Text>
            }
            style={{ height: '100%' }}
          >
            {deps.length === 0 && (
              <Typography.Text type="tertiary">检测中…</Typography.Text>
            )}
            {deps.map((d) => (
              <div className="kv" key={d.name}>
                <span>{d.name}</span>
                <span className="row" style={{ justifyContent: 'flex-end' }}>
                  {[d.server_version, d.model, d.detail].filter(Boolean).length > 0 && (
                    <Typography.Text type="tertiary" size="small" ellipsis={{ showTooltip: true }}>
                      {[d.server_version, d.model, d.detail].filter(Boolean).join(' · ')}
                    </Typography.Text>
                  )}
                  <Tag color={TAG_COLOR[d.status as keyof typeof TAG_COLOR] || 'grey'} type="light" size="small">
                    {TAG_TEXT[d.status] || d.status}
                  </Tag>
                </span>
              </div>
            ))}
            <div style={{ marginTop: 14 }}>
              <div className="row-between" style={{ marginBottom: 6 }}>
                <Typography.Text type="tertiary" size="small">
                  向量化覆盖率
                </Typography.Text>
                <Typography.Text type="tertiary" size="small">
                  {readyRate.toFixed(1)}%
                </Typography.Text>
              </div>
              <Progress
                percent={Number(readyRate.toFixed(1))}
                showInfo={false}
                stroke="var(--semi-color-success)"
                size="small"
              />
            </div>
          </Card>
        </Col>

        <Col xs={24} xl={10}>
          <Card title="运行配置" style={{ height: '100%' }}>
            <div className="kv">
              <span>向量后端</span>
              <span>{cfg.vector_backend || '-'}</span>
            </div>
            <div className="kv">
              <span>Embedding</span>
              <span>
                {cfg.embedding} · {cfg.embedding_dim}d · {cfg.embedding_threads} 线程
              </span>
            </div>
            <div className="kv">
              <span>向量表</span>
              <span className="mono">{cfg.vector_table || 'chunk_vectors'}</span>
            </div>
            <div className="kv">
              <span>LLM</span>
              <span className="mono">{cfg.llm_model}</span>
            </div>
            <div className="kv">
              <span>切块粒度</span>
              <span>
                父 {cfg.chunk_parent_size} / 子 {cfg.chunk_child_size}
              </span>
            </div>
            <div className="kv">
              <span>运行时长</span>
              <span>
                {health?.uptime_sec ?? 0}s · 队列 {health?.queue ?? 0}
              </span>
            </div>
          </Card>
        </Col>
      </Row>

      <Card
        title="下一步"
        style={{ marginTop: 16 }}
        headerExtraContent={
          <Button
            theme="borderless"
            type="primary"
            icon={<IconArrowRight />}
            onClick={() => onGo('upload')}
          >
            上传小说
          </Button>
        }
      >
        <Typography.Paragraph type="tertiary" style={{ marginBottom: 12 }}>
          P1 计划：Neo4j 图谱抽取与写入、BM25 + RRF 混合检索、reranker 精排、图谱可视化、向量空间投影视图。
        </Typography.Paragraph>
        <div className="row">
          <Tag color="blue" type="light">
            稠密召回
          </Tag>
          <Tag color="violet" type="light">
            父块回溯
          </Tag>
          <Tag color="cyan" type="light">
            SSE 流式生成
          </Tag>
          <Tag color="grey" type="light">
            CPU 8 核 · 1024 维
          </Tag>
        </div>
      </Card>
    </>
  )
}
