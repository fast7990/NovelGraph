import { useEffect, useMemo, useRef } from 'react'
import cytoscape from 'cytoscape'
import fcose from 'cytoscape-fcose'

/**
 * 图谱画布：Cytoscape.js + fcose 力导向布局。
 *
 * 为什么换成 Cytoscape 而不是继续自己写 SVG：
 * 自研版本能跑，但拖拽、缩放、动画、布局全要自己维护，加个"邻域展开/边标签/
 * 框选"就得改一堆坐标逻辑。Cytoscape 是图谱领域的标准库（MIT），canvas 渲染，
 * 200 节点轻松，布局算法（fcose/cose/concentric/breadthfirst）直接换参数就行。
 *
 * 与 React 的分工：React 只负责"有哪些节点哪些边"，把元素丢给 cytoscape；
 * 位置、动画、命中测试全在 canvas 里，不经过 React 的 diff。
 */

export type GraphNode = {
  id: string
  name: string
  type: string
  importance: number
  mention_count: number
  desc?: string
  aliases?: string[]
}

export type GraphEdge = {
  id: string
  src: string
  dst: string
  relation: string
  strength: number
  confidence: number
  status?: string
}

export const TYPE_COLOR: Record<string, string> = {
  Character: '#4c6ef5',
  Faction: '#9c36b5',
  Location: '#2f9e44',
  Item: '#e8590c',
  Event: '#e03131',
  Concept: '#868e96',
}

export const TYPE_LABEL: Record<string, string> = {
  Character: '人物',
  Faction: '势力',
  Location: '地点',
  Item: '物品',
  Event: '事件',
  Concept: '设定',
}

const LABEL_ZOOM = 0.65 // 缩到这个倍率以下就不画标签，否则糊成一片
const LABEL_BUDGET = 60 // 节点数超过这个值时，标签只给最重要的那批

function cssVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

export default function GraphCanvas({
  nodes,
  edges,
  selected,
  onSelect,
  height = 560,
  showPending = true,
}: {
  nodes: GraphNode[]
  edges: GraphEdge[]
  selected?: string | null
  onSelect?: (id: string | null) => void
  height?: number
  showPending?: boolean
}) {
  const boxRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<cytoscape.Core | null>(null)
  const nodeIdsRef = useRef('')

  // 事件回调只在挂载时绑定一次，用 ref 拿最新 props，避免每次变化重建 cy
  const selectRef = useRef(onSelect)
  selectRef.current = onSelect
  const selectedRef = useRef(selected)
  selectedRef.current = selected

  const visibleEdges = useMemo(
    () => (showPending ? edges : edges.filter((e) => e.status !== 'pending')),
    [edges, showPending],
  )

  // 有边的实体集合：力布局对零度节点会排成方格（cose 的 tiling），
  // 所以画布只画有关系的实体，孤立实体在提示里说明数量。
  // 用未过滤的 edges 算连通性，这样切"待确认"开关不会触发重排。
  const connected = useMemo(() => {
    const s = new Set<string>()
    for (const e of edges) {
      s.add(e.src)
      s.add(e.dst)
    }
    return s
  }, [edges])

  const linkedNodes = useMemo(() => nodes.filter((n) => connected.has(n.id)), [nodes, connected])
  // 全都没关系（比如点开一个还没抽到关系的实体）：那就退回把它们平铺出来，
  // 有东西可点总比空画布好
  const allLoose = linkedNodes.length === 0 && nodes.length > 0
  const drawnNodes = allLoose ? nodes : linkedNodes
  const isolatedCount = nodes.length - drawnNodes.length

  // 半径沿用旧版的视觉：按 importance 归一化到 6-21px
  const maxImp = useMemo(() => Math.max(0.2, ...nodes.map((n) => n.importance || 0)), [nodes])

  const labelBudget = useMemo(() => {
    if (drawnNodes.length <= LABEL_BUDGET) return null
    const ranked = [...drawnNodes].sort(
      (a, b) => (b.importance || 0) - (a.importance || 0) || (b.mention_count || 0) - (a.mention_count || 0),
    )
    return new Set(ranked.slice(0, LABEL_BUDGET).map((n) => n.id))
  }, [drawnNodes])

  const drawnIds = useMemo(() => new Set(drawnNodes.map((n) => n.id)), [drawnNodes])

  const elements = useMemo(() => {
    const ns = drawnNodes.map((n) => {
      const r = 6 + ((n.importance || 0) / maxImp) * 15
      return {
        group: 'nodes' as const,
        data: {
          id: n.id,
          label: n.name,
          type: n.type,
          size: Math.round(r * 2),
          color: TYPE_COLOR[n.type] || '#868e96',
        },
        classes: labelBudget && !labelBudget.has(n.id) ? 'nolabel' : '',
      }
    })
    const es = visibleEdges
      .filter((e) => drawnIds.has(e.src) && drawnIds.has(e.dst))
      .map((e) => ({
        group: 'edges' as const,
        data: {
          id: e.id,
          source: e.src,
          target: e.dst,
          label: e.relation,
          strength: e.strength || 0.5,
          width: Number((0.6 + (e.strength || 0.5) * 1.6).toFixed(2)),
          arrow: e.relation && e.relation !== 'OTHER' ? 'triangle' : 'none',
        },
        classes: e.status === 'pending' ? 'pending' : '',
      }))
    return [...ns, ...es]
  }, [drawnNodes, drawnIds, visibleEdges, maxImp, labelBudget])

  // 建 cy 实例（只做一次，StrictMode 下初始化的两次会正确销毁一次）
  useEffect(() => {
    const box = boxRef.current
    if (!box) return

    // 新实例必须重新布局：清掉签名，否则下面的元素 effect 会以为"只是换了边"
    nodeIdsRef.current = ''

    const text1 = cssVar('--semi-color-text-1', '#495057')
    const bg = cssVar('--semi-color-bg-1', '#ffffff')
    const edge = cssVar('--semi-color-border', '#adb5bd')
    const primary = cssVar('--semi-color-primary', '#4c6ef5')

    cytoscape.use(fcose)

    const cy = cytoscape({
      container: box,
      elements: [],
      minZoom: 0.15,
      maxZoom: 4,
      // 不设 wheelSensitivity：非默认值会让 cytoscape 每次初始化都打印
      // "You have set a custom wheel sensitivity…" 告警，且官方明确说这会
      // 让滚轮缩放手感与主流鼠标不一致。保持默认，靠 min/maxZoom 兜住范围。
      boxSelectionEnabled: false,
      style: [
        {
          selector: 'node',
          style: {
            width: 'data(size)',
            height: 'data(size)',
            'background-color': 'data(color)',
            'background-opacity': 0.8,
            'border-width': 1.2,
            'border-color': 'data(color)',
            label: 'data(label)',
            'font-size': 11,
            'font-family': 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
            color: text1,
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 5,
            'text-outline-color': bg,
            'text-outline-width': 3,
            'min-zoomed-font-size': 10,
            'overlay-opacity': 0,
          },
        },
        { selector: 'node.nolabel', style: { 'text-opacity': 0 } },
        { selector: 'node.tiny', style: { 'text-opacity': 0 } },
        {
          selector: 'node.hover',
          style: { 'background-opacity': 1, 'border-width': 2 },
        },
        {
          selector: 'node.hl',
          style: {
            'border-width': 2.5,
            'border-color': '#ffffff',
            'background-opacity': 0.95,
            'z-index': 20,
          },
        },
        {
          // 只有被点中的那个节点放大标签，邻居只描边
          selector: 'node.sel',
          style: {
            'border-width': 3,
            'border-color': '#ffffff',
            'font-size': 13,
            'font-weight': 600,
            'text-opacity': 1,
            'z-index': 30,
          },
        },
        { selector: 'node.dim', style: { opacity: 0.18 } },
        {
          selector: 'edge',
          style: {
            width: 'data(width)',
            'line-color': edge,
            'target-arrow-color': edge,
            'target-arrow-shape': 'data(arrow)',
            'arrow-scale': 0.7,
            'curve-style': 'bezier',
            opacity: 0.45,
            'overlay-opacity': 0,
          },
        },
        { selector: 'edge.pending', style: { 'line-style': 'dashed' } },
        {
          selector: 'edge.hl',
          style: {
            'line-color': primary,
            'target-arrow-color': primary,
            opacity: 0.6,
            'z-index': 15,
          },
        },
        { selector: 'edge.dim', style: { opacity: 0.1 } },
      ] as any,
    })

    cyRef.current = cy
    let alive = true
    let tiny = false

    cy.on('tap', 'node', (evt) => {
      const id = evt.target.id()
      selectRef.current?.(selectedRef.current === id ? null : id)
    })
    cy.on('tap', (evt) => {
      if (evt.target === cy) selectRef.current?.(null)
    })
    cy.on('mouseover', 'node', (evt) => {
      evt.target.addClass('hover')
      if (box) box.style.cursor = 'pointer'
    })
    cy.on('mouseout', 'node', (evt) => {
      evt.target.removeClass('hover')
      if (box) box.style.cursor = ''
    })
    cy.on('zoom', () => {
      const next = cy.zoom() < LABEL_ZOOM
      if (next !== tiny) {
        tiny = next
        cy.nodes().toggleClass('tiny', tiny)
      }
    })
    // fcose 的 fit 在部分场景下不生效（容器尺寸与布局计算时的视口不一致），
    // 布局结束时兜一次，保证算完的图一定落在可视区内
    cy.on('layoutstop', () => {
      if (alive) cy.fit(undefined, 30)
    })

    const ro = new ResizeObserver(() => cy.resize())
    ro.observe(box)

    return () => {
      alive = false
      nodeIdsRef.current = ''
      ro.disconnect()
      cy.destroy()
      cyRef.current = null
    }
  }, [])

  // 元素变化：节点集合变了才重排，只切边（比如待确认开关）时保留原位置
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return

    const sig = nodes.map((n) => n.id).join('|')
    const structural = sig !== nodeIdsRef.current
    nodeIdsRef.current = sig

    const pos = new Map<string, cytoscape.Position>()
    cy.nodes().forEach((n) => {
      pos.set(n.id(), { ...n.position() })
    })

    cy.batch(() => {
      cy.elements().remove()
      cy.add(elements as cytoscape.ElementDefinition[])
      if (!structural) {
        cy.nodes().forEach((n) => {
          const p = pos.get(n.id())
          if (p) n.position(p)
        })
      }
    })

    if (nodes.length === 0) return

    if (structural) {
      cy.resize() // 容器可能在布局期间变过宽高（响应式/侧栏加载），先对齐再算

      if (allLoose || cy.nodes().length === 0) {
        // 没有一条关系：平铺，别指望力导向给出结构
        cy.layout({ name: 'grid', animate: 'end', padding: 30, avoidOverlap: true } as any).run()
        cy.fit(undefined, 30)
        return
      }

      cy.layout({
        name: 'fcose',
        quality: 'default',
        // 不用逐帧动画：fcose 的动画阶段在组件快速重挂载时会被打断，导致
        // layoutstop 永不到达、节点停在初始位置。'end' 是算完一次性落位。
        animate: 'end',
        randomize: true,
        fit: true,
        padding: 30,
        nodeSeparation: 70,
        nodeRepulsion: 8000,
        gravity: 0.2,
        // 关系越强，两端越近
        idealEdgeLength: (edge: cytoscape.EdgeSingular) => 48 + (1 - (edge.data('strength') || 0.5)) * 32,
      } as any).run()
    } else {
      cy.fit(undefined, 30)
    }
  }, [nodes, elements, allLoose])

  // 选中态：自身 + 一跳邻居高亮，其余压暗
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.elements().removeClass('hl dim sel')
    if (!selected) return
    const node = cy.getElementById(selected)
    if (node.empty()) return
    const nb = node.closedNeighborhood()
    nb.addClass('hl')
    node.addClass('sel')
    cy.elements().difference(nb).addClass('dim')
  }, [selected, elements])

  return (
    <div className="graph-wrap" style={{ height }}>
      <div className="graph-canvas" ref={boxRef} />
      <div className="graph-tips">
        {drawnNodes.length} 节点 · {visibleEdges.length} 关系 · 滚轮缩放 · 拖拽平移
        {isolatedCount > 0 && ` · 另有 ${isolatedCount} 个无关系实体未画出`}
      </div>
    </div>
  )
}
