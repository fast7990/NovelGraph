/**
 * 消息栏四个图标的真实功能验收。
 *
 * 用户反馈三件事：
 *   1. "重新生成"文字按钮和图标行里的 ⟳ 重复 → 现在只留图标
 *   2. 点赞是假的（点了没反应）→ 现在落库
 *   3. 删除是假删除（界面删了、库里还在）→ 现在真删
 *
 * 三个都是同一类问题：**Semi 的 Chat 组件不接回调也照样把按钮画出来**，
 * 看起来有入口、点下去没反应，比不显示更糟。所以验收不能只看"按钮在不在"，
 * 必须查库。
 *
 * 关键：不能用"文本不再变化"判断回答结束——思考阶段正文一直是空的，
 * 会被误判成已完成（实测因此只等到 2 个图标、库里只有提问）。必须等
 * POST /api/v1/chat 这个 SSE 请求真正关闭。
 *
 * 跑法：NODE_PATH=<node_modules> node tools/shot_actions.cjs
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5174'
const API = process.env.SHOT_API || 'http://127.0.0.1:8001/api/v1'
const OUT = process.env.SHOT_DIR || 'shots'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const AI = '.semi-chat-chatBox:not(.semi-chat-chatBox-right)'

async function apiGet(path) {
  const r = await fetch(`${API}${path}`)
  return r.json()
}

/** 等一次问答的 SSE 流真正关闭（= 服务端已落库）。必须在触发前调用。 */
function armChatDone(page, timeoutMs = 240000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      page.off('requestfinished', h)
      reject(new Error('等回答超时'))
    }, timeoutMs)
    const h = (req) => {
      if (req.url().includes('/api/v1/chat') && req.method() === 'POST') {
        clearTimeout(t)
        page.off('requestfinished', h)
        resolve()
      }
    }
    page.on('requestfinished', h)
  })
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
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

  // 用全新的临时会话，别在用户自己的对话上做删除实验
  await page.locator('.sess-head button').click()
  await sleep(1500)

  const done1 = armChatDone(page)
  await box.fill('华佗有几个徒弟')
  await box.press('Enter')
  console.log('等待回答…')
  await done1
  await sleep(2000)

  const sid = new URL(page.url()).searchParams.get('session') || ''
  console.log('会话:', sid)
  const msgs = async () => (await apiGet(`/chat/sessions/${sid}/messages`)).items || []
  console.log('回答后消息数:', (await msgs()).length, '(应为 2)')

  // ---- 1. 旧的「⟳ 重新生成」文字按钮应当消失 ----
  console.log('残留 .msg-op:', await page.locator('.msg-op').count())
  console.log(
    '文本里还有"重新生成":',
    await page.locator('.chat-main').innerText().then((t) => t.includes('重新生成')),
  )

  const lastAi = page.locator(AI).last()
  const act = () => lastAi.locator('.semi-chat-chatBox-action button')
  await lastAi.hover()
  await sleep(500)
  console.log('图标数量:', await act().count(), '(应为 5: 复制/赞/踩/⟳/删)')
  await page.screenshot({ path: `${OUT}/act-icons.png` })

  // ---- 2. 点赞 → 落库 ----
  await lastAi.hover()
  await act().nth(1).dispatchEvent('click')
  await sleep(1500)
  let after = await msgs()
  console.log('点赞落库数:', after.filter((m) => m.feedback === 'up').length, '(应为 1)')

  // 刷新后图标必须还亮：回放时把 feedback 映射回 like/dislike
  await page.reload({ waitUntil: 'domcontentloaded' })
  await box.waitFor({ timeout: 60000 })
  await page
    .waitForFunction((sel) => document.querySelectorAll(sel).length > 0, AI, { timeout: 30000 })
    .catch(() => {})
  await sleep(2500)
  const likeSvg = await page
    .locator(AI)
    .last()
    .locator('.semi-chat-chatBox-action button')
    .nth(1)
    .locator('svg')
    .first()
    .evaluate((el) => el.getAttribute('class') || '')
    .catch(() => '')
  console.log('刷新后点赞图标 class:', JSON.stringify(likeSvg), '(含 like_thumb 才算亮)')
  await page.screenshot({ path: `${OUT}/act-liked.png` })

  // ---- 3. 点踩：赞要自动取消（互斥） ----
  await page.locator(AI).last().hover()
  await sleep(400)
  await page.locator(AI).last().locator('.semi-chat-chatBox-action button').nth(2).dispatchEvent('click')
  await sleep(1500)
  after = await msgs()
  console.log(
    '点踩后 up/down:',
    after.filter((m) => m.feedback === 'up').length,
    '/',
    after.filter((m) => m.feedback === 'down').length,
    '(应为 0 / 1)',
  )

  // ---- 4. 重新生成：走图标，历史不多一条 ----
  const before = (await msgs()).length
  const done2 = armChatDone(page)
  await page.locator(AI).last().hover()
  await sleep(400)
  await page.locator(AI).last().locator('.semi-chat-chatBox-action button').nth(3).dispatchEvent('click')
  console.log('已点 ⟳，等待重新生成…')
  await done2
  await sleep(2000)
  const afterRegen = (await msgs()).length
  console.log(`重新生成前后消息数: ${before} → ${afterRegen} (应相等)`)

  // ---- 5. 删除消息：库里要真的少一条 ----
  await page.locator(AI).last().hover()
  await sleep(400)
  await page.locator(AI).last().locator('.semi-chat-chatBox-action button').last().dispatchEvent('click')
  await sleep(800)
  const ok = page.locator('.semi-popconfirm .semi-button-primary').first()
  const okCount = await ok.count()
  console.log('确认弹层:', okCount > 0 ? '已出现' : '没出现')
  if (okCount > 0) {
    await ok.click()
    await sleep(2000)
  }
  const afterDel = (await msgs()).length
  console.log(`删除后消息数: ${afterDel} (应为 ${afterRegen - 1})`)
  await page.screenshot({ path: `${OUT}/act-deleted.png` })

  // ---- 6. 收尾：删掉临时会话 ----
  await fetch(`${API}/chat/sessions/${sid}`, { method: 'DELETE' })
  console.log('临时会话已清理:', sid)

  console.log('console error:', errors.length ? errors : '无')
  console.log('console warning:', warns.length ? warns : '无')
  await browser.close()
})()
