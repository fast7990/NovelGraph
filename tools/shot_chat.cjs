/**
 * 截图验收脚本（问答页思考过程）。
 * 用系统 Edge 跑 playwright，避免下 chromium。
 *
 * 跑法：
 *   NODE_PATH=<npx缓存>/node_modules node tools/shot_chat.cjs
 */
const { chromium } = require('playwright')
const fs = require('fs')

const URL = process.env.SHOT_URL || 'http://127.0.0.1:5173/?novel=nv_ddef13761e244a2f#chat'
const OUT = process.env.SHOT_DIR || 'shots'
const QUESTION = process.env.SHOT_Q || '曹操和刘备是什么关系？'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  page.on('console', (m) => {
    if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200))
  })
  page.on('pageerror', (e) => console.log('[pageerror]', String(e).slice(0, 300)))

  await page.goto(URL, { waitUntil: 'domcontentloaded' })
  const box = page.getByPlaceholder('例如：第三章里谁救了主角？')
  await box.waitFor({ timeout: 60000 })
  await sleep(1500) // 等书籍下拉、空态渲染稳

  const t0 = Date.now()
  await box.fill(QUESTION)
  await box.press('Enter')

  // 阶段 0：检索中（发送后立刻，meta 还没到）
  await sleep(700)
  await page.screenshot({ path: `${OUT}/chat-retrieving.png` })

  // 阶段 1：思考中（等到思考文本有明显内容再截）
  const thinkText = page.locator('.think-text')
  await thinkText.waitFor({ timeout: 30000 })
  let len = 0
  for (let i = 0; i < 60; i++) {
    len = (await thinkText.innerText()).length
    if (len > 120) break
    await sleep(500)
  }
  console.log(`思考文本 ${len} 字，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`)
  await page.screenshot({ path: `${OUT}/chat-thinking.png` })

  // 阶段 2：正文开始（思考区应收起）
  const deadline = Date.now() + 120000
  let answered = false
  while (Date.now() < deadline) {
    answered = await page.evaluate(() => {
      const heads = Array.from(document.querySelectorAll('.think-head .think-title'))
      return heads.some((h) => (h.textContent || '').startsWith('已思考'))
    })
    if (answered) break
    await sleep(1000)
  }
  console.log(`正文开始（思考区收起）：${answered}，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`)
  await sleep(1200)
  await page.screenshot({ path: `${OUT}/chat-answering.png` })

  // 阶段 3：点开已收起的思考区，确认能回看
  const head = page.locator('.think-head').last()
  if (await head.count()) {
    await head.click()
    await sleep(600)
    await page.screenshot({ path: `${OUT}/chat-thinking-reopen.png` })
  }

  const summary = await page.evaluate(() => {
    const h = document.querySelector('.think-head')
    const t = document.querySelector('.think-text')
    const a = document.querySelector('.semi-chat-chatBox-content')
    return {
      head: h && h.innerText.replace(/\s+/g, ' ').trim(),
      thinkChars: t ? t.innerText.length : 0,
      answerChars: a ? a.innerText.length : 0,
    }
  })
  console.log('summary:', JSON.stringify(summary, null, 0))
  await browser.close()
})().catch(async (e) => {
  console.error('FAILED:', e)
  process.exit(1)
})
