/**
 * 流式观感诊断：正文到底是「逐字长出来」还是「一次性糊上去」。
 *
 * 后端已经确认是流式的（curl 能看到每秒 2~3 帧 delta），所以问题只可能在三处：
 *   1. 网关把正文攒成几大块推（帧率稀疏，肉眼看着像跳变）
 *   2. Semi Chat 的 content 更新没进 MarkdownRender（受控/非受控没接上）
 *   3. 思考期太长，正文一到就很快结束，主观上"不是流式"
 * 这个脚本按 150ms 采样 DOM 里的正文字符数与思考面板状态，用时间序列区分三者。
 *
 * 跑法：NODE_PATH=<node_modules> node tools/probe_stream.cjs
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

  const frames = []
  page.on('response', (r) => {
    if (r.url().includes('/api/v1/chat') && r.request().method() === 'POST') {
      frames.push({ t: Date.now(), kind: 'request-finished' })
    }
  })

  await page.goto(`${BASE}/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('例如：第三章里谁救了主角？').waitFor({ timeout: 60000 })
  await sleep(2000)

  // 不点"新对话"：那个按钮是异步建会话，等不等得到全看时序，采样很容易
  // 抓在旧会话上（表现为"正文一次都没变"，其实是根本没看新气泡）。
  // 干净页面上直接发送最省事——session_id 为空时后端会隐式建一个。
  const base = await page.locator('.semi-chat-chatBox').count()
  if (base > 0) console.log(`!! 页面初始已有 ${base} 个气泡，采样可能串台`)

  const t0 = Date.now()
  await page.getByPlaceholder('例如：第三章里谁救了主角？').fill(Q)
  await page.keyboard.press('Enter')

  const samples = []
  let lastLen = -1
  let still = 0
  for (let i = 0; i < 1600; i++) {
    const s = await page.evaluate(() => {
      // 最后一条助手气泡：Semi 用 chatBox 类名区分左右，助手在左侧
      const boxes = Array.from(document.querySelectorAll('.semi-chat-chatBox:not(.semi-chat-chatBox-right)'))
      const last = boxes[boxes.length - 1]
      const md = last ? last.querySelector('.semi-markdownRender') : null
      const think = last ? last.querySelector('.think') : null
      // done 事件落 usage 后才会出现 .tok-bar，用它当「这一轮真的结束了」的信号。
      // 别拿"停止生成按钮消失"判断：那个按钮的 class 随版本变，容易永远为 false，
      // 于是采样在首帧就 break，看起来像"只有一次变化"，把真问题掩盖掉。
      return {
        len: md ? (md.innerText || '').length : 0,
        thinkTitle: think ? (think.querySelector('.think-title') || {}).textContent || '' : '',
        thinkOpen: think ? !!think.querySelector('.think-body') : false,
        finished: !!last && !!last.querySelector('.tok-bar'),
      }
    })
    const t = Date.now() - t0
    if (s.len !== lastLen) {
      samples.push({ t, len: s.len, think: s.thinkTitle, open: s.thinkOpen })
      lastLen = s.len
      still = 0
    } else if (s.len > 0) {
      still += 1
    }
    if (s.finished && still >= 2) break
    await sleep(150)
  }

  const tEnd = Date.now() - t0
  await page.screenshot({ path: `${OUT}/stream.png`, fullPage: false })
  await browser.close()

  console.log('=== 正文长度变化（只记变化点）===')
  for (const p of samples) {
    console.log(`  +${String(p.t).padStart(6)}ms  len=${String(p.len).padStart(5)}  think=${p.think}`)
  }
  const grow = samples.filter((p) => p.len > 0)
  console.log('\n=== 结论 ===')
  console.log('总耗时', tEnd, 'ms；长度变化点', samples.length, '个；正文变化点', grow.length, '个')
  if (grow.length >= 5) {
    const span = grow[grow.length - 1].t - grow[0].t
    console.log(`正文从首字到收尾跨 ${span}ms，平均每 ${(span / Math.max(1, grow.length - 1)).toFixed(0)}ms 长一次`)
  } else if (grow.length > 0) {
    console.log('!! 正文只有 1~4 次变化 → 前端没吃到流式增量')
  }
  fs.writeFileSync(`${OUT}/stream.json`, JSON.stringify(samples, null, 2))
})()
