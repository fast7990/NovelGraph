/**
 * token 用量展示验收。
 *
 * 跑法：
 *   NODE_PATH=<npx缓存>/node_modules node tools/shot_usage.cjs
 *   （默认打 5174，也就是临时指向 8001 的那个前端实例）
 *
 * 验收点：
 *   1. 一轮问答结束后，答案下方出现 .tok-bar，且数字非 0
 *   2. 侧栏该会话条目里也能看到 tok（累计口径）
 *   3. 控制台 0 error / 0 warning
 *
 * 为什么要两个位置都验：气泡里的是"这一轮"的即时反馈，
 * 侧栏是"这个会话总共花了多少"。只验一个，另一处静默失效就发现不了。
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5174'
const OUT = process.env.SHOT_DIR || 'shots'
const W = Number(process.env.SHOT_W || 1440)
const H = Number(process.env.SHOT_H || 900)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitTextStable(page, stableMs = 6000, timeoutMs = 240000) {
  const deadline = Date.now() + timeoutMs
  let last = ''
  let since = Date.now()
  while (Date.now() < deadline) {
    await sleep(1000)
    const cur = (await page.locator('.chat-main').innerText()).length
    if (cur !== last) {
      last = cur
      since = Date.now()
    } else if (Date.now() - since >= stableMs) {
      return true
    }
  }
  return false
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 })
  const errors = []
  const warns = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
    if (m.type() === 'warning') warns.push(m.text().slice(0, 200))
  })
  page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 300)))

  await page.goto(`${BASE}/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  const box = page.getByPlaceholder('例如：第三章里谁救了主角？')
  await box.waitFor({ timeout: 60000 })
  await sleep(2500)

  // 新建一个干净会话，避免受历史会话的累计数干扰
  await page.locator('.sess-head button').click()
  await sleep(1200)

  let resolveChat
  const chatDone = new Promise((r) => (resolveChat = r))
  page.on('requestfinished', (req) => {
    if (req.url().includes('/api/v1/chat') && req.method() === 'POST') resolveChat()
  })

  await box.fill('华佗是怎么死的')
  await box.press('Enter')
  console.log('等待回答…')
  await page.waitForSelector('.cite-chip', { timeout: 180000 })
  await chatDone
  await waitTextStable(page)
  await sleep(1500)

  // ---- 1. 答案下方用量条 ----
  const barCount = await page.locator('.tok-bar').count()
  console.log('.tok-bar 数量:', barCount)
  if (barCount > 0) {
    const txt = await page.locator('.tok-bar').last().innerText()
    console.log('用量条文本:', JSON.stringify(txt.replace(/\n/g, ' ')))
    const zero = /本轮\s+0\s+tok/.test(txt)
    console.log('本轮数字是否非 0:', !zero)
  } else {
    console.log('!! 答案下方没有用量条')
  }
  await page.screenshot({ path: `${OUT}/usage-inline.png` })

  // ---- 2. 侧栏累计 ----
  await sleep(1500)
  const firstMeta = await page.locator('.sess-item').first().innerText()
  console.log('侧栏首条:', JSON.stringify(firstMeta.replace(/\n/g, ' | ')))
  const hasTok = /tok/.test(firstMeta)
  console.log('侧栏是否显示 tok:', hasTok)
  await page.screenshot({ path: `${OUT}/usage-session.png` })

  // ---- 3. 刷新回放：历史消息没有"本轮"的账，但会话累计必须还在 ----
  await page.reload({ waitUntil: 'domcontentloaded' })
  await box.waitFor({ timeout: 60000 })
  await page.waitForFunction(
    () => document.querySelectorAll('.semi-chat-chatBox').length > 0,
    { timeout: 30000 },
  ).catch(() => {})
  await sleep(2500)
  const afterReload = await page.locator('.tok-bar').count()
  console.log('刷新后 .tok-bar 数量:', afterReload)
  if (afterReload > 0) {
    const t = await page.locator('.tok-bar').last().innerText()
    console.log('刷新后用量条:', JSON.stringify(t.replace(/\n/g, ' ')))
  } else {
    console.log('!! 刷新后用量条消失（会话累计没回显）')
  }
  await page.screenshot({ path: `${OUT}/usage-after-reload.png` })

  // ---- 4. 控制台 ----
  console.log('console error:', errors.length ? errors : '无')
  console.log('console warning:', warns.length ? warns : '无')

  await browser.close()
})()
