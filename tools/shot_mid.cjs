/**
 * 中优先级三项的界面验收：重新生成 / 会话搜索 / 导出入口。
 *
 * 跑法：
 *   NODE_PATH=<node workspace>/node_modules node tools/shot_mid.cjs
 *   （默认打 5174，也就是临时指向 8001 的那个前端实例）
 *
 * 验收点：
 *   1. 侧栏搜索框存在，输入关键词后列表被过滤，且出现命中片段（.sess-hit 带 <b>）
 *   2. 最后一条答案下方有「重新生成」，点完消息数不增加（历史不翻倍）
 *   3. 每个会话都有导出入口
 *   4. 控制台 0 error / 0 warning
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
  await page.waitForFunction(() => document.querySelectorAll('.sess-item').length > 0, {
    timeout: 30000,
  })
  await sleep(1200)

  const beforeCount = await page.locator('.sess-item').count()
  console.log('搜索前会话数:', beforeCount)

  // ---- 1. 搜索 ----
  const search = page.getByPlaceholder('搜索对话标题或内容…')
  await search.waitFor({ timeout: 15000 })
  await search.fill('华佗')
  await sleep(1600) // 300ms 防抖 + 网络
  const afterCount = await page.locator('.sess-item').count()
  const hitCount = await page.locator('.sess-hit').count()
  const boldCount = await page.locator('.sess-hit b').count()
  console.log('搜索"华佗"后会话数:', afterCount, '| 命中片段行:', hitCount, '| 高亮词:', boldCount)
  const firstHit = hitCount ? await page.locator('.sess-hit').first().innerText() : ''
  console.log('首个命中片段:', JSON.stringify(firstHit.slice(0, 60)))
  await page.screenshot({ path: `${OUT}/mid-search.png` })

  await search.fill('')
  await sleep(1600)
  const cleared = await page.locator('.sess-item').count()
  console.log('清空搜索后会话数:', cleared)

  // ---- 2. 导出入口 ----
  const exportOps = await page.locator('.sess-op[title="导出为 Markdown"]').count()
  console.log('导出入口数量:', exportOps)

  // ---- 3. 重新生成 ----
  // 用最新一条会话（点第一条，它是按活跃时间倒序的）
  await page.locator('.sess-item').first().click()
  await page.waitForFunction(() => document.querySelectorAll('.semi-chat-chatBox').length > 0, {
    timeout: 30000,
  })
  await sleep(1500)
  const msgsBefore = await page.locator('.semi-chat-chatBox').count()
  const regenBtn = page.locator('.msg-op', { hasText: '重新生成' })
  const regenCount = await regenBtn.count()
  console.log('消息块数:', msgsBefore, '| 重新生成按钮数:', regenCount)
  await page.screenshot({ path: `${OUT}/mid-regen-before.png` })

  if (regenCount > 0) {
    let resolveChat
    const chatDone = new Promise((r) => (resolveChat = r))
    const onFinished = (req) => {
      if (req.url().includes('/api/v1/chat') && req.method() === 'POST') resolveChat()
    }
    page.on('requestfinished', onFinished)
    await regenBtn.last().click()
    console.log('已点击重新生成，等待回答…')
    await chatDone
    await waitTextStable(page)
    page.off('requestfinished', onFinished)
    await sleep(1500)
    const msgsAfter = await page.locator('.semi-chat-chatBox').count()
    console.log('重新生成后消息块数:', msgsAfter, '（应与点击前一致）')
    console.log('历史是否没有被撑大:', msgsAfter === msgsBefore)
    await page.screenshot({ path: `${OUT}/mid-regen-after.png` })
  } else {
    console.log('!! 没有找到重新生成入口')
  }

  console.log('console error:', errors.length ? errors : '无')
  console.log('console warning:', warns.length ? warns : '无')

  await browser.close()
})()
