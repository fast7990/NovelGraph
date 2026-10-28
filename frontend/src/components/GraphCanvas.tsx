import { useEffect, useMemo, useRef, useState } from 'react'

/**
 * 零依赖力导向图。
 *
 * 为什么不用 G6 / ECharts：这两个包各 800KB+，而这里只需要"节点散开 + 拖拽 +
 * 缩放 + 点选"四件事。自己写还能完全控制视觉（比如按 mention_count 决定半径、
 * 按 relation 决定边色）。
 *
 * 性能上的关键决定：**绕过 React 渲染**。每帧对 200 个节点算完位置后，
 * 如果走 setState，React 要 diff 600 个 SVG 元素，20fps 都悬。
 * 所以坐标存在 ref 里，直接改 DOM 的 transform / x1y1x2y2。
 * React 只负责结构（哪些节点、哪些边），不负责动画。
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

const W = 1000
const H = 680
const REP = 22000 // 斥力系数
const LINK_DIST = 90
const LINK_ATT = 0.02
const GRAVITY = 0.006
const DAMPING = 0.86

type Particle = { x: number; y: number; vx: number; vy: number; fx: number | null; fy: number | null }

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
  const wrapRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const nodeRefs = useRef<(SVGGElement | null)[]>([])
  const lineRefs = useRef<(SVGLineElement | null)[]>([])
  const parts = useRef<Particle[]>([])
  const raf = useRef<number>(0)
  const alpha = useRef(1)
  const drag = useRef<{ idx: number; active: boolean }>({ idx: -1, active: false })
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const viewRef = useRef(view)
  viewRef.current = view

  const visibleEdges = useMemo(
    () => (showPending ? edges : edges.filter((e) => e.status !== 'pending')),
    [edges, showPending],
  )

  const index = useMemo(() => {
    const m = new Map<string, number>()
    nodes.forEach((n, i) => m.set(n.id, i))
    return m
  }, [nodes])

  const links = useMemo(
    () =>
      visibleEdges
        .map((e) => ({ ...e, a: index.get(e.src), b: index.get(e.dst) }))
        .filter((e) => e.a !== undefined && e.b !== undefined) as (GraphEdge & { a: number; b: number })[],
    [visibleEdges, index],
  )

  const linksRef = useRef(links)
  linksRef.current = links

  // 节点集合变化时重置物理状态（换书、切跳数都会走到这里）
  useEffect(() => {
    parts.current = nodes.map((_, i) => {
      const a = (i / Math.max(nodes.length, 1)) * Math.PI * 2
      const r = 120 + (i % 7) * 26
      return {
        x: W / 2 + Math.cos(a) * r,
        y: H / 2 + Math.sin(a) * r,
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
      }
    })
    nodeRefs.current = nodeRefs.current.slice(0, nodes.length)
    lineRefs.current = lineRefs.current.slice(0, links.length)
    alpha.current = 1
    setView({ k: 1, x: 0, y: 0 })
  }, [nodes])

  const paint = () => {
    const ps = parts.current
    for (let i = 0; i < ps.length; i++) {
      const g = nodeRefs.current[i]
      if (g) g.setAttribute('transform', `translate(${ps[i].x.toFixed(2)},${ps[i].y.toFixed(2)})`)
    }
    const ls = linksRef.current
    for (let i = 0; i < ls.length; i++) {
      const el = lineRefs.current[i]
      if (!el) continue
      const a = ps[ls[i].a]
      const b = ps[ls[i].b]
      if (!a || !b) continue
      el.setAttribute('x1', a.x.toFixed(1))
      el.setAttribute('y1', a.y.toFixed(1))
      el.setAttribute('x2', b.x.toFixed(1))
      el.setAttribute('y2', b.y.toFixed(1))
    }
  }

  useEffect(() => {
    let alive = true
    const step = () => {
      if (!alive) return
      const ps = parts.current
      const n = ps.length
      if (n === 0) return

      if (alpha.current > 0.004) {
        // 斥力：O(n²)。n 上限 200，每帧 4 万次浮点运算，60fps 没有压力。
        for (let i = 0; i < n; i++) {
          const p = ps[i]
          for (let j = i + 1; j < n; j++) {
            const q = ps[j]
            let dx = p.x - q.x
            let dy = p.y - q.y
            let d2 = dx * dx + dy * dy
            if (d2 < 1) {
              dx = Math.random() - 0.5
              dy = Math.random() - 0.5
              d2 = 1
            }
            const d = Math.sqrt(d2)
            const f = (REP / d2) * alpha.current
            const ux = (dx / d) * f
            const uy = (dy / d) * f
            p.vx += ux
            p.vy += uy
            q.vx -= ux
            q.vy -= uy
          }
        }

        // 引力：只连有边的节点，孤立节点靠重力聚到中心
        for (const l of linksRef.current) {
          const p = ps[l.a]
          const q = ps[l.b]
          if (!p || !q) continue
          const dx = q.x - p.x
          const dy = q.y - p.y
          const d = Math.sqrt(dx * dx + dy * dy) || 0.01
          const f = (d - LINK_DIST) * LINK_ATT * (0.4 + l.strength * 0.8) * alpha.current
          const ux = (dx / d) * f
          const uy = (dy / d) * f
          p.vx += ux
          p.vy += uy
          q.vx -= ux
          q.vy -= uy
        }

        for (const p of ps) {
          p.vx += (W / 2 - p.x) * GRAVITY * alpha.current
          p.vy += (H / 2 - p.y) * GRAVITY * alpha.current
          p.vx *= DAMPING
          p.vy *= DAMPING
          // 速度上限：防止某个节点被弹到天外
          const sp = Math.hypot(p.vx, p.vy)
          if (sp > 40) {
            p.vx = (p.vx / sp) * 40
            p.vy = (p.vy / sp) * 40
          }
          if (p.fx !== null) {
            p.x = p.fx
            p.y = p.fy ?? p.y
            p.vx = 0
            p.vy = 0
          } else {
            p.x += p.vx
            p.y += p.vy
          }
          p.x = Math.max(24, Math.min(W - 24, p.x))
          p.y = Math.max(24, Math.min(H - 24, p.y))
        }
        alpha.current *= 0.994
      }

      paint()
      raf.current = requestAnimationFrame(step)
    }
    raf.current = requestAnimationFrame(step)
    return () => {
      alive = false
      cancelAnimationFrame(raf.current)
    }
  }, [nodes, links])

  // 拖拽
  const onNodeDown = (e: React.MouseEvent, i: number) => {
    e.stopPropagation()
    drag.current = { idx: i, active: true }
    alpha.current = Math.max(alpha.current, 0.35) // 拖动时给点活力，别让图僵死
    const p = parts.current[i]
    if (p) {
      p.fx = p.x
      p.fy = p.y
    }
  }

  const toWorld = (clientX: number, clientY: number) => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const r = svg.getBoundingClientRect()
    const v = viewRef.current
    // getBoundingClientRect 是屏幕像素，viewBox 是 1000×680，先换成 viewBox 坐标
    const bx = ((clientX - r.left) / r.width) * W
    const by = ((clientY - r.top) / r.height) * H
    return { x: (bx - v.x) / v.k, y: (by - v.y) / v.k }
  }

  const onMove = (e: React.MouseEvent) => {
    if (!drag.current.active) return
    const w = toWorld(e.clientX, e.clientY)
    const p = parts.current[drag.current.idx]
    if (p) {
      p.fx = w.x
      p.fy = w.y
    }
  }

  const endDrag = () => {
    const i = drag.current.idx
    if (i >= 0 && parts.current[i]) {
      parts.current[i].fx = null
      parts.current[i].fy = null
    }
    drag.current = { idx: -1, active: false }
  }

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const svg = svgRef.current
    if (!svg) return
    const r = svg.getBoundingClientRect()
    const bx = ((e.clientX - r.left) / r.width) * W
    const by = ((e.clientY - r.top) / r.height) * H
    const v = viewRef.current
    const k2 = Math.min(4, Math.max(0.25, v.k * (e.deltaY < 0 ? 1.12 : 0.89)))
    const ratio = k2 / v.k
    // 以光标为锚点缩放，否则放大后目标会跑出视野
    setView({ k: k2, x: bx - (bx - v.x) * ratio, y: by - (by - v.y) * ratio })
  }

  const maxImp = useMemo(() => Math.max(0.2, ...nodes.map((n) => n.importance || 0)), [nodes])
  const labelVisible = view.k > 0.75

  return (
    <div className="graph-wrap" ref={wrapRef} style={{ height }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        className="graph-svg"
        onMouseMove={onMove}
        onMouseUp={endDrag}
        onMouseLeave={endDrag}
        onWheel={onWheel}
        onClick={() => onSelect?.(null)}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="5" markerHeight="5" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--graph-edge, #adb5bd)" />
          </marker>
        </defs>
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {links.map((l, i) => {
            const active = selected && (nodes[l.a]?.id === selected || nodes[l.b]?.id === selected)
            return (
              <line
                key={l.id}
                ref={(el) => {
                  lineRefs.current[i] = el
                }}
                stroke={active ? 'var(--semi-color-primary)' : 'var(--graph-edge, #adb5bd)'}
                strokeWidth={(0.6 + l.strength * 1.6) * (active ? 1.6 : 1)}
                strokeOpacity={selected && !active ? 0.16 : active ? 0.9 : 0.42}
                strokeDasharray={l.status === 'pending' ? '4 3' : undefined}
                markerEnd={l.relation !== 'OTHER' ? 'url(#arrow)' : undefined}
              />
            )
          })}
          {nodes.map((n, i) => {
            const r = 6 + (n.importance / maxImp) * 15
            const color = TYPE_COLOR[n.type] || '#868e96'
            const active = selected === n.id
            const dim = selected && !active && !links.some((l) => (nodes[l.a]?.id === selected && l.b === i) || (nodes[l.b]?.id === selected && l.a === i))
            return (
              <g
                key={n.id}
                ref={(el) => {
                  nodeRefs.current[i] = el
                }}
                className="graph-node"
                onMouseDown={(e) => onNodeDown(e, i)}
                onClick={(e) => {
                  e.stopPropagation()
                  onSelect?.(active ? null : n.id)
                }}
                style={{ cursor: 'pointer', opacity: dim ? 0.25 : 1 }}
              >
                <circle r={r} fill={color} fillOpacity={active ? 0.95 : 0.75} stroke={active ? '#fff' : color} strokeWidth={active ? 3 : 1.2} />
                {n.type === 'Event' && <circle r={r + 3} fill="none" stroke={color} strokeWidth={1} strokeDasharray="2 2" />}
                {(labelVisible || active) && (
                  <text
                    y={r + 12}
                    textAnchor="middle"
                    className="graph-label"
                    style={{ fontSize: active ? 13 : 11, fontWeight: active ? 600 : 400 }}
                  >
                    {n.name}
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
      <div className="graph-tips">
        {nodes.length} 节点 · {links.length} 关系 · 滚轮缩放 · 拖拽移动
      </div>
    </div>
  )
}
