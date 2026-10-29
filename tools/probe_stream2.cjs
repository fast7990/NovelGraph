/**
 * 时间轴对齐诊断：SSE 事件到达 vs DOM 上屏。
 *
 * 上一版只采样了 DOM，看到"正文 71 秒才出现"，但 done 里的 ttft 只有 7.3 秒 ——
 * 中间 60 多秒去向不明。要么事件没到，要么到了但没上屏。
 * 这里在页面里包一层 fetch，把每个 SSE chunk 的到达时刻和增量长度记下来，
 * 和 DOM 的字符数放在同一条时间轴上，一眼就能看出断在哪一段。
 *
 * 跑法：NODE_PATH=<node_modules> node tools/probe_stream2.cjs
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5173'
const OUT = process.env.SHOT_DIR || 'shots'
const Q = process.env.SHOT_Q || '用三句话说说曹操是个什么样的人'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

  // 包 fetch：把 SSE 流里每个 data: 行的时间戳与类型记到 window.__sse
  await page.addInitScript(() => {
    window.__sse = []
    const t0 = (window.__sseT0 = performance.now())
    const orig = window.fetch
    window.fetch = async (...args) => {
      const res = await orig(...args)
      const url = String(args[0])
      if (!url.includes('/api/v1/chat') || !res.body) return res
      const [a, b] = res.body.tee()
      ;(async () => {
        const reader = b.getReader()
        const dec = new TextDecoder()
        let buf = ''
        let ev = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += dec.decode(value, { stream: true })
          const parts = buf.split('\n\n')
          buf = parts.pop() || ''
          for (const p of parts) {
            let name = ev
            let payload = ''
            for (const line of p.split('\n')) {
              if (line.startsWith('event:')) name = line.slice(6).trim()
              else if (line.startsWith('data:')) payload += line.slice(5).trim()
            }
            ev = name
            let len = 0
            try {
              const o = JSON.parse(payload)
              len = name === 'delta' ? String(o.text || '').length : 0
              if (name === 'meta') window.__meta = o
            } catch { /* ignore */ }
            window.__sse.push({ t: Math.round(performance.now() - t0), ev: name, len })
          }
        }
      })()
      return res
    }
  })

  await page.goto(`${BASE}/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('例如：第三章里谁救了主角？').waitFor({ timeout: 60000 })
  await sleep(2000)
  // 干净页面上直接发：不点"新对话"，那个按钮是异步建会话，
  // 等不到就会把采样抓在旧会话上，白跑一轮
  const base = await page.locator('.semi-chat-chatBox').count()
  if (base > 0) console.log(`!! 页面初始已有 ${base} 个气泡，采样可能串台`)

  await page.getByPlaceholder('例如：第三章里谁救了主角？').fill(Q)
  await page.keyboard.press('Enter')

  const dom = []
  let lastLen = -1
  for (let i = 0; i < 1200; i++) {
    const s = await page.evaluate(() => {
      const boxes = Array.from(document.querySelectorAll('.semi-chat-chatBox:not(.semi-chat-chatBox-right)'))
      const last = boxes[boxes.length - 1]
      const md = last ? last.querySelector('.semi-markdownRender') : null
      return {
        t: Math.round(performance.now() - window.__sseT0),
        len: md ? (md.innerText || '').length : 0,
        finished: !!last && !!last.querySelector('.tok-bar'),
      }
    })
    if (s.len !== lastLen) {
      dom.push({ t: s.t, len: s.len })
      lastLen = s.len
    }
    if (s.finished && dom.length && s.t - dom[dom.length - 1].t > 800) break
    await sleep(120)
  }

  const sse = await page.evaluate(() => ({ sse: window.__sse, meta: window.__meta || {} }))
  await browser.close()

  const deltaAll = sse.sse.filter((e) => e.ev === 'delta')
  const lastEv = sse.sse[sse.sse.length - 1]
  const firstDelta = deltaAll[0]
  const acc = deltaAll.reduce((n, e) => n + e.len, 0)
  console.log(`SSE：共 ${sse.sse.length} 事件，delta ${deltaAll.length} 帧 / 累计 ${acc} 字`)
  console.log(`  首帧 ${firstDelta ? firstDelta.t + 'ms' : '—'}，末事件 ${lastEv.ev} @ ${lastEv.t}ms`)
  if (deltaAll.length > 1) {
    const gaps = deltaAll.slice(1).map((e, i) => e.t - deltaAll[i].t)
    gaps.sort((a, b) => b - a)
    console.log(`  delta 间隔：中位 ${gaps[Math.floor(gaps.length / 2)]}ms，最大 ${gaps[0]}ms`)
  }
  console.log(`\nDOM：正文长度变化 ${dom.length} 次`)
  console.log(`  首字上屏 ${dom.find((d) => d.len > 0)?.t ?? '—'}ms，末次变化 ${dom[dom.length - 1].t}ms`)
  const lag = firstDelta && dom.find((d) => d.len > 0) ? dom.find((d) => d.len > 0).t - firstDelta.t : null
  console.log(`  首帧 → 首字延迟：${lag == null ? '—' : lag + 'ms'}`)

  fs.writeFileSync(`${OUT}/stream2.json`, JSON.stringify({ sse: sse.sse, dom }, null, 2))
  console.log(`\n明细见 ${OUT}/stream2.json`)
})()
