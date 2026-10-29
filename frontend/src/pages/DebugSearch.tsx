import { useEffect, useRef, useState } from 'react'
import {
  Banner,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Select,
  Tag,
  Typography,
} from '@douyinfe/semi-ui'
import { IconSearch } from '@douyinfe/semi-icons'
import CardTitle from '../components/CardTitle'
import PageHead from '../components/PageHead'
import { api, Novel } from '../api/client'

const MODE_LABEL: Record<string, string> = {
  hybrid: '混合召回',
  dense_only: '仅稠密（词法未命中）',
  sparse_only: '仅词法（稠密失败）',
  empty: '空结果',
}
const MODE_COLOR: Record<string, string> = {
  hybrid: 'green',
  dense_only: 'amber',
  sparse_only: 'amber',
  empty: 'red',
}
const CHANNEL_LABEL: Record<string, string> = {
  dense: '稠密',
  lexical: '词法',
}

type Item = {
  chunk_id: string
  chapter_no: number
  title: string
  char_start: number
  char_end: number
  score: number
  snippet: string
  content?: string
  channels: Record<string, number>
  rerank_score: number | null
  rrf_score: number
}

function Stat({ label, value, foot }: { label: string; value: React.ReactNode; foot?: string }) {
  return (
    <Card bodyStyle={{ padding: '14px 16px' }} style={{ height: '100%' }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      <div className="stat-foot">{foot || ''}</div>
    </Card>
  )
}

export default function DebugSearch({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  // 支持 ?q=xxx 进页面直接跑一次检索——排查时把链接发出去就能复现同一次召回
  const [q, setQ] = useState(() => new URLSearchParams(window.location.search).get('q') || '')
  const [topK, setTopK] = useState(8)
  const [res, setRes] = useState<any>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<string>('')

  useEffect(() => {
    api.novels().then((r) => {
      setNovels(r.items)
      if (!novelId && r.items.length) onPick(r.items[0].id)
    })
  }, [])

  const run = async () => {
    if (!novelId || !q.trim()) return
    setBusy(true)
    setErr('')
    try {
      setRes(await api.search(novelId, q.trim(), topK))
      setOpen('')
    } catch (e: any) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  // ?q= 带进来的查询等 novelId 就绪后自动跑一次，只跑一次
  const autoRan = useRef(false)
  useEffect(() => {
    if (autoRan.current || !novelId || !q.trim()) return
    autoRan.current = true
    void run()
  }, [novelId, q])

  const d = res?.detail || {}
  const items: Item[] = res?.items || []
  return (
    <>
      <PageHead
        title="检索调试台"
        desc="不生成答案，只看召回：问题经过稠密 / 词法两路并行召回，RRF 融合后按 parent 去重回溯。这里能直接看出是哪一路断了、精排有没有生效。"
        extra={
          <Select
            value={novelId || undefined}
            onChange={(v) => onPick(String(v))}
            style={{ width: 240 }}
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

      <Card bodyStyle={{ padding: '14px 16px' }}>
        <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
          <Input
            value={q}
            onChange={setQ}
            onEnterPress={run}
            placeholder="输入一个问题，例如：沈鹤和苏晚是什么关系？"
            style={{ flex: 1, minWidth: 320 }}
            showClear
          />
          <Select value={topK} onChange={(v) => setTopK(Number(v))} style={{ width: 120 }}>
            {[4, 6, 8, 12, 16, 24].map((n) => (
              <Select.Option key={n} value={n}>
                取 {n} 条
              </Select.Option>
            ))}
          </Select>
          <Button theme="solid" icon={<IconSearch />} loading={busy} onClick={run}>
            检索
          </Button>
        </div>
      </Card>

      {res && (
        <>
          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="召回模式"
                value={
                  <Tag color={(MODE_COLOR[res.mode] || 'grey') as any} type="light" size="large">
                    {MODE_LABEL[res.mode] || res.mode}
                  </Tag>
                }
                foot={res.degraded ? '已降级（不是完整 hybrid）' : '稠密 + 词法都在'}
              />
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat label="稠密召回" value={d.dense ?? 0} foot="pgvector · HNSW + iterative_scan" />
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat label="词法召回" value={d.lexical ?? 0} foot="tsvector · bigram" />
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Stat
                label="融合 / 回表"
                value={`${d.fused ?? 0} → ${d.candidates ?? 0}`}
                foot={`RRF 后取 ${d.candidates ?? 0} 条进精排，父块去重剩 ${d.parents ?? 0}`}
              />
            </Col>
          </Row>

          <Card
            title={<CardTitle text={`结果 ${items.length} 条`} />}
            style={{ marginTop: 16 }}
            headerExtraContent={
              <Tag color={d.reranked ? 'green' : 'amber'} type="light" size="small">
                {d.reranked ? `已精排 · ${d.rerank_model || 'reranker'}` : `未精排${d.rerank_error ? `（${d.rerank_error}）` : ''}`}
              </Tag>
            }
            bodyStyle={{ padding: items.length ? '8px 16px 16px' : 16 }}
          >
            {items.length === 0 && <Empty title="没有召回结果" description="换一个问题，或确认这本书已完成向量化" />}
            {items.map((it, i) => (
              <div key={it.chunk_id} style={{ padding: '12px 0', borderBottom: '1px solid var(--semi-color-border)' }}>
                <div className="row" style={{ gap: 10, alignItems: 'baseline' }}>
                  <span
                    className="mono"
                    style={{
                      width: 26,
                      height: 22,
                      textAlign: 'center',
                      lineHeight: '22px',
                      borderRadius: 6,
                      background: i < 3 ? 'var(--semi-color-primary-light-active)' : 'var(--semi-color-fill-0)',
                      fontWeight: 600,
                      flex: '0 0 auto',
                    }}
                  >
                    {i + 1}
                  </span>
                  {/* 不用 ellipsis={{showTooltip}}：Semi 内部 ResizeObserver 会 findDOMNode，
                      React 18 StrictMode 下每次挂载都刷弃用告警。CSS 省略 + 原生 title 等价 */}
                  <Typography.Text
                    strong
                    title={it.title || `第 ${it.chapter_no} 章`}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {it.title || `第 ${it.chapter_no} 章`}
                  </Typography.Text>
                  <span className="mono" style={{ color: 'var(--semi-color-text-2)' }}>
                    {it.char_start}-{it.char_end}
                  </span>
                  <Tag type="light" size="small" color={it.rerank_score !== null ? 'green' : 'grey'}>
                    {it.rerank_score !== null ? `rerank ${it.rerank_score.toFixed(3)}` : `score ${it.score.toFixed(3)}`}
                  </Tag>
                  <Tag type="light" size="small" color="blue">
                    rrf {it.rrf_score.toFixed(4)}
                  </Tag>
                  {Object.entries(it.channels || {}).map(([ch, rank]) => (
                    <Tag key={ch} type="light" size="small" color={ch === 'dense' ? 'blue' : 'cyan'}>
                      {CHANNEL_LABEL[ch] || ch} #{rank}
                    </Tag>
                  ))}
                </div>
                <div style={{ marginLeft: 36, marginTop: 6, fontSize: 13, color: 'var(--semi-color-text-1)' }}>
                  {open === it.chunk_id ? it.content : it.snippet}
                  <Button
                    theme="borderless"
                    size="small"
                    style={{ marginLeft: 6 }}
                    onClick={() => setOpen(open === it.chunk_id ? '' : it.chunk_id)}
                  >
                    {open === it.chunk_id ? '收起' : '展开父块'}
                  </Button>
                </div>
              </div>
            ))}
          </Card>
        </>
      )}
    </>
  )
}
