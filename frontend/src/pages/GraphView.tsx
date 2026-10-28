import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Banner,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Progress,
  Row,
  Select,
  Spin,
  Switch,
  Tag,
  Typography,
} from '@douyinfe/semi-ui'
import { IconRefresh, IconSearch } from '@douyinfe/semi-icons'
import PageHead from '../components/PageHead'
import GraphCanvas, { TYPE_COLOR, TYPE_LABEL } from '../components/GraphCanvas'
import {
  api,
  GraphEntity,
  GraphEntityDetail,
  GraphOverview,
  GraphStatus,
  GraphSubgraph,
  Novel,
} from '../api/client'

const TYPES = ['Character', 'Faction', 'Location', 'Item', 'Event', 'Concept']

function Stat({ label, value, foot }: { label: string; value: React.ReactNode; foot?: React.ReactNode }) {
  return (
    <Card bodyStyle={{ padding: '14px 16px' }} style={{ height: '100%' }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      <div className="stat-foot">{foot}</div>
    </Card>
  )
}

function TypeTag({ type }: { type: string }) {
  const color = TYPE_COLOR[type] || '#868e96'
  return (
    <Tag
      size="small"
      type="light"
      style={{ color, background: `${color}1a`, border: `1px solid ${color}33` }}
    >
      {TYPE_LABEL[type] || type}
    </Tag>
  )
}

export default function GraphView({ novelId, onPick }: { novelId: string; onPick: (id: string) => void }) {
  const [novels, setNovels] = useState<Novel[]>([])
  const [status, setStatus] = useState<GraphStatus | null>(null)
  const [ov, setOv] = useState<GraphOverview | null>(null)
  const [sub, setSub] = useState<GraphSubgraph | null>(null)
  const [entities, setEntities] = useState<GraphEntity[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<GraphEntityDetail | null>(null)
  const [hops, setHops] = useState(2)
  const [showPending, setShowPending] = useState(true)
  const [q, setQ] = useState('')
  const [etype, setEtype] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [subLoading, setSubLoading] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    api
      .novels()
      .then((r) => {
        setNovels(r.items)
        if (!novelId && r.items.length) onPick(r.items[0].id)
      })
      .catch(() => {})
  }, [])

  // 换书：清掉上一本的选中态，否则会拿旧 entity id 去查新书
  useEffect(() => {
    setSelected(null)
    setDetail(null)
    setOv(null)
    setSub(null)
  }, [novelId])

  const loadMeta = useCallback(async () => {
    if (!novelId) return
    try {
      const [s, o] = await Promise.all([api.graphStatus(novelId), api.graphOverview(novelId)])
      setStatus(s)
      setOv(o)
      setErr('')
    } catch (e: any) {
      setErr(String(e?.message || e))
    }
  }, [novelId])

  useEffect(() => {
    loadMeta()
  }, [loadMeta, tick])

  useEffect(() => {
    if (!novelId) return
    setSubLoading(true)
    const p = selected
      ? { center: selected, hops, include_pending: showPending }
      : { include_pending: showPending }
    api
      .graphSubgraph(novelId, p)
      .then((r) => {
        setSub(r)
        setErr('')
      })
      .catch((e) => setErr(String(e?.message || e)))
      .finally(() => setSubLoading(false))
  }, [novelId, selected, hops, showPending, tick])

  useEffect(() => {
    if (!novelId) return
    api
      .graphEntities(novelId, { q, type: etype, page_size: 60, order: 'importance' })
      .then((r) => setEntities(r.items))
      .catch(() => {})
  }, [novelId, q, etype, tick])

  useEffect(() => {
    if (!selected || !novelId) {
      setDetail(null)
      return
    }
    api
      .graphEntity(novelId, selected)
      .then(setDetail)
      .catch(() => setDetail(null))
  }, [selected, novelId, tick])

  // 构建中轮询：进度变化要反映在进度条上，跑完自动刷新图
  const running = status?.job?.running
  useEffect(() => {
    if (!running || !novelId) return
    const t = setInterval(async () => {
      try {
        const s = await api.graphStatus(novelId)
        setStatus(s)
        if (!s.job?.running) setTick((x) => x + 1)
      } catch {
        /* 轮询失败不打扰用户，下一轮再试 */
      }
    }, 3000)
    return () => clearInterval(t)
  }, [running, novelId])

  const build = async (force: boolean) => {
    if (!novelId) return
    setBusy(true)
    try {
      const r = await api.graphBuild(novelId, force)
      setErr(r.message)
      setTick((x) => x + 1)
    } catch (e: any) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  const nodeIndex = useMemo(() => {
    const m = new Map<string, GraphEntity>()
    for (const n of sub?.nodes || []) m.set(n.id, n)
    return m
  }, [sub])

  const typeOptions = useMemo(
    () =>
      TYPES.map((t) => ({
        value: t,
        label: `${TYPE_LABEL[t]} ${
          (ov?.by_type || []).find((x) => x.type === t)?.count ?? 0
        }`,
      })),
    [ov],
  )

  const job = status?.job
  const hasGraph = (ov?.entities || 0) > 0

  return (
    <div className="stack" style={{ minHeight: 0 }}>
      <PageHead
        title="知识图谱"
        desc="实体与关系由大模型逐块抽取，经证据回查、置信度、关系枚举三道闸门后入库"
        extra={
          <>
            <Select
              value={novelId || undefined}
              onChange={(v) => onPick(String(v))}
              style={{ width: 220 }}
              placeholder="选择小说"
              optionList={novels.map((n) => ({ value: n.id, label: n.title }))}
            />
            <Button
              icon={<IconRefresh />}
              loading={busy}
              onClick={() => build(hasGraph)}
              disabled={running}
            >
              {hasGraph ? '重建图谱' : '构建图谱'}
            </Button>
          </>
        }
      />

      {running && (
        <Card bodyStyle={{ padding: '12px 16px' }}>
          <div className="row-between" style={{ marginBottom: 6 }}>
            <span>
              正在抽取 · <span className="mono">{job?.stage}</span> · 第 {job?.chapters_done}/
              {job?.chapters_total} 章
            </span>
            <span className="mono">{job?.progress?.toFixed(1)}%</span>
          </div>
          <Progress percent={Number(job?.progress || 0)} showInfo={false} stroke="var(--semi-color-primary)" />
          <div className="stat-foot" style={{ marginTop: 6 }}>
            抽取在后台串行执行，可离开本页；完成后自动刷新
          </div>
        </Card>
      )}

      {job?.status === 'FAILED' && job.error && (
        <Banner type="danger" description={job.error} closeIcon={null} />
      )}
      {err && !running && <Banner type="info" description={err} closeIcon={null} />}

      {!hasGraph && !running ? (
        <Card>
          <Empty
            title="这本还没有知识图谱"
            description="点右上角「构建图谱」，按 parent 块逐块抽取实体与关系。一本 120 章的书大约需要 10-20 分钟。"
          />
        </Card>
      ) : (
        <>
          <Row gutter={12}>
            <Col span={6}>
              <Stat label="实体" value={ov?.entities ?? '-'} foot={`${ov?.by_type?.length || 0} 类`} />
            </Col>
            <Col span={6}>
              <Stat
                label="关系"
                value={ov?.relations ?? '-'}
                foot={`${ov?.pending_relations || 0} 条待确认`}
              />
            </Col>
            <Col span={6}>
              <Stat label="实体提及" value={ov?.mentions ?? '-'} foot="块 × 实体" />
            </Col>
            <Col span={6}>
              <Stat
                label="高关联实体"
                value={ov?.top_entities?.[0]?.name || '-'}
                foot={ov?.top_entities?.[0] ? `${ov.top_entities[0].mention_count} 次提及` : ''}
              />
            </Col>
          </Row>

          <Row gutter={12}>
            {/* 左：实体列表 */}
            <Col span={5}>
              <Card
                bodyStyle={{ padding: 12, display: 'flex', flexDirection: 'column', height: '100%' }}
                style={{ height: 620 }}
              >
                <Input
                  prefix={<IconSearch />}
                  placeholder="搜实体 / 别名 / 描述"
                  value={q}
                  onChange={(v) => setQ(v)}
                  showClear
                  style={{ marginBottom: 8 }}
                />
                <Select
                  placeholder="全部类型"
                  value={etype || undefined}
                  onChange={(v) => setEtype(v ? String(v) : '')}
                  optionList={typeOptions}
                  showClear
                  style={{ width: '100%', marginBottom: 8 }}
                />
                <div className="entity-list">
                  {entities.map((e) => (
                    <div
                      key={e.id}
                      className={`entity-item ${selected === e.id ? 'on' : ''}`}
                      onClick={() => setSelected(selected === e.id ? null : e.id)}
                    >
                      <span className="dot" style={{ background: TYPE_COLOR[e.type] || '#868e96' }} />
                      <span className="name ellipsis">{e.name}</span>
                      <span className="mono cnt">{e.mention_count}</span>
                    </div>
                  ))}
                  {!entities.length && <div className="stat-foot" style={{ padding: 8 }}>无匹配实体</div>}
                </div>
              </Card>
            </Col>

            {/* 中：画布 */}
            <Col span={12}>
              <Card
                bodyStyle={{ padding: 0 }}
                style={{ height: 620 }}
                title={
                  <div className="row" style={{ gap: 10 }}>
                    <span style={{ fontSize: 13 }}>{selected ? '邻域视图' : '全书主图'}</span>
                    <Select
                      size="small"
                      value={hops}
                      onChange={(v) => setHops(Number(v))}
                      style={{ width: 96 }}
                      optionList={[
                        { value: 1, label: '1 跳' },
                        { value: 2, label: '2 跳' },
                        { value: 3, label: '3 跳' },
                      ]}
                    />
                    <span className="stat-foot">待确认</span>
                    <Switch size="small" checked={showPending} onChange={setShowPending} />
                  </div>
                }
                headerExtraContent={
                  selected ? (
                    <Button size="small" theme="borderless" onClick={() => setSelected(null)}>
                      回到主图
                    </Button>
                  ) : null
                }
              >
                <Spin spinning={subLoading}>
                  {sub && sub.nodes.length ? (
                    <GraphCanvas
                      nodes={sub.nodes}
                      edges={sub.edges}
                      selected={selected}
                      onSelect={setSelected}
                      height={560}
                      showPending={showPending}
                    />
                  ) : (
                    <div style={{ padding: 60 }}>
                      <Empty title="没有可画的节点" description="先构建图谱，或在左侧选一个实体" />
                    </div>
                  )}
                </Spin>
              </Card>
              <div className="graph-legend">
                {TYPES.map((t) => (
                  <span key={t}>
                    <i style={{ background: TYPE_COLOR[t] }} />
                    {TYPE_LABEL[t]}
                  </span>
                ))}
                <span><i className="dash" />虚线 = 待确认</span>
                {sub?.truncated && <span style={{ color: 'var(--semi-color-warning)' }}>已截断，缩小跳数看全</span>}
              </div>
            </Col>

            {/* 右：详情 */}
            <Col span={7}>
              <Card bodyStyle={{ padding: 0 }} style={{ height: 620 }}>
                {detail ? (
                  <div className="detail">
                    <div className="detail-head">
                      <div>
                        <div style={{ fontSize: 17, fontWeight: 600 }}>{detail.name}</div>
                        <div className="row" style={{ gap: 6, marginTop: 4 }}>
                          <TypeTag type={detail.type} />
                          <span className="stat-foot">提及 {detail.mention_count}</span>
                          <span className="stat-foot">首现第 {detail.first_chapter} 章</span>
                        </div>
                      </div>
                    </div>
                    {detail.aliases.length > 0 && (
                      <div className="row" style={{ gap: 4, flexWrap: 'wrap', padding: '0 14px 8px' }}>
                        {detail.aliases.map((a) => (
                          <Tag key={a} size="small" type="ghost">
                            {a}
                          </Tag>
                        ))}
                      </div>
                    )}
                    {detail.desc && (
                      <Typography.Paragraph style={{ padding: '0 14px', fontSize: 12.5, color: 'var(--semi-color-text-1)' }}>
                        {detail.desc}
                      </Typography.Paragraph>
                    )}
                    <div className="detail-sec">关系 {detail.relations.length}</div>
                    <div className="rel-list">
                      {detail.relations.map((r) => (
                        <div
                          key={r.id}
                          className="rel-item"
                          onClick={() => setSelected(r.other_id)}
                          title={r.evidence}
                        >
                          <div className="row-between">
                            <span className="ellipsis" style={{ fontSize: 12.5 }}>
                              <span className="stat-foot">{r.direction === 'out' ? '→' : '←'} </span>
                              {r.other_name}
                            </span>
                            <Tag size="small" type={r.status === 'pending' ? 'ghost' : 'light'}>
                              {r.relation}
                            </Tag>
                          </div>
                          <div className="rel-ev ellipsis">{r.evidence || '（无证据）'}</div>
                        </div>
                      ))}
                      {!detail.relations.length && (
                        <div className="stat-foot" style={{ padding: '4px 14px' }}>
                          这个实体没有已确认的关系（可能全被判为待确认）
                        </div>
                      )}
                    </div>
                    {detail.chapters.length > 0 && (
                      <>
                        <div className="detail-sec">出现章节</div>
                        <div className="chap-strip">
                          {detail.chapters.map((c) => (
                            <div
                              key={c.chapter_no}
                              className="chap-cell"
                              style={{ opacity: Math.min(1, 0.25 + c.count / 6) }}
                              title={`第 ${c.chapter_no} 章 · ${c.count} 次`}
                            >
                              {c.chapter_no}
                            </div>
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                ) : (
                  <div style={{ padding: 40 }}>
                    <Empty
                      title="选一个实体"
                      description={
                        hasGraph
                          ? '点左侧列表或图上的圆点，这里会显示它的关系与证据原文'
                          : '还没有图谱数据'
                      }
                    />
                    {ov?.by_relation?.length ? (
                      <div style={{ marginTop: 24 }}>
                        <div className="detail-sec" style={{ padding: '0 0 8px' }}>
                          关系类型分布
                        </div>
                        {ov.by_relation.slice(0, 10).map((r) => (
                          <div key={r.relation} className="row-between" style={{ padding: '3px 0', fontSize: 12.5 }}>
                            <span>{r.relation}</span>
                            <span className="mono">{r.count}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )}
              </Card>
            </Col>
          </Row>
        </>
      )}
      {nodeIndex.size === 0 && hasGraph && <div style={{ display: 'none' }} />}
    </div>
  )
}
