/**
 * 会话记忆验收：侧栏列表 + 切换恢复 + 追问上下文。
 *
 * 跑法：
 *   NODE_PATH=<npx缓存>/node_modules node tools/shot_session.cjs
 *
 * 验收点：
 *   1. 侧栏列出会话（标题/条数/时间）
 *   2. 点会话能恢复历史消息
 *   3. 追问时答案上方出现「承接上文」提示条
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const OUT = process.env.SHOT_DIR || 'shots'
const W = Number(process.env.SHOT_W || 1440)
const H = Number(process.env.SHOT_H || 900)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 等页面文本稳定（生成真正结束）。
 *
 * 流式回答期间文本一直在涨，此时刷新页面会切断 SSE —— 服务端生成器被取消，
 * 这一轮虽然会兜底存成"中断"，但验收要看的是完整回合，必须先等它跑完。
 */
async function waitTextStable(page, stableMs = 8000, timeoutMs = 240000) {
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
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
  })
  page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 300)))

  await page.goto(`http://127.0.0.1:5173/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  const box = page.getByPlaceholder('例如：第三章里谁救了主角？')
  await box.waitFor({ timeout: 60000 })
  await sleep(2000)

  // ---- 1. 侧栏是否列出会话 ----
  const sessCount = await page.locator('.sess-item').count()
  console.log('侧栏会话数:', sessCount)
  if (sessCount > 0) {
    const first = await page.locator('.sess-item').first().innerText()
    console.log('首条会话文本:', JSON.stringify(first.replace(/\n/g, ' | ')))
  }
  await page.screenshot({ path: `${OUT}/session-list.png` })

  // ---- 2. 新建会话并提问 ----
  await page.locator('.sess-head button').click()
  await sleep(1200)
  // 用"chat 请求结束"作为轮次完成的判据：SSE 的 done 事件后连接才关闭。
  // 比看 DOM 状态可靠 —— 消息元素在流式过程中一直存在，只有请求结束
  // 才代表这一轮真正落库（第二轮才能继承到上下文）。
  let chatCalls = 0
  let resolveChat
  const chatDone = new Promise((r) => {
    resolveChat = r
  })
  page.on('requestfinished', (req) => {
    if (req.url().includes('/api/v1/chat') && req.method() === 'POST') {
      chatCalls += 1
      resolveChat()
    }
  })

  await box.fill('华佗的著作有哪些')
  await box.press('Enter')
  console.log('等待第一轮回答…')
  await page.waitForSelector('.cite-chip', { timeout: 180000 })
  await chatDone
  await sleep(2500)
  await page.screenshot({ path: `${OUT}/session-turn1.png` })

  // ---- 3. 追问，检查「承接上文」提示条 ----
  const box2 = page.getByPlaceholder('例如：第三章里谁救了主角？')
  await box2.waitFor({ timeout: 60000 })
  await box2.fill('他后来怎么样了')
  await box2.press('Enter')
  console.log('追问已发送，等待回答…')
  try {
    await page.waitForSelector('.mem-bar', { timeout: 180000 })
    const ok = await waitTextStable(page)
    console.log('追问生成完成:', ok)
  } catch (e) {
    console.log('!! 未出现 .mem-bar')
    const memCount = await page.locator('.mem-bar').count()
    console.log('   .mem-bar 数量:', memCount)
    const msgCount = await page.locator('.semi-chat-chatBox').count()
    console.log('   消息块数:', msgCount, '| 已发起的 chat 请求数:', chatCalls)
    const texts = await page.locator('.semi-chat-chatBox').allInnerTexts()
    texts.forEach((t, i) => console.log(`   [${i}]`, t.replace(/\n/g, ' | ').slice(0, 110)))
  }
  await sleep(1500)

  const memCount2 = await page.locator('.mem-bar').count()
  if (memCount2 > 0) {
    const memText = await page.locator('.mem-bar').first().innerText()
    console.log('承接上文提示:', JSON.stringify(memText.replace(/\n/g, ' ')))
  }
  await page.screenshot({ path: `${OUT}/session-turn2-context.png` })

  // ---- 4. 刷新页面 → 验证对话自动恢复（URL 带 session，不该依赖手动点） ----
  const deepLink = await page.evaluate(() => window.location.search)
  console.log('刷新前 URL:', deepLink)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await box.waitFor({ timeout: 60000 })
  // 会话列表是异步加载的，等它稳定（数量不再变化）再统计
  await page.waitForFunction(
    () => {
      const items = document.querySelectorAll('.sess-item')
      const empties = document.querySelectorAll('.sess-empty')
      return items.length > 0 && empties.length === 0
    },
    { timeout: 30000 },
  ).catch(() => {})
  await sleep(2500)
  const afterReload = await page.locator('.sess-item').count()
  const autoRestored = await page.locator('.semi-chat-chatBox').count()
  console.log('刷新后侧栏会话数:', afterReload, '| 自动恢复的消息块数:', autoRestored)
  await page.screenshot({ path: `${OUT}/session-after-reload.png` })

  // 点第一条会话，验证历史恢复
  if (afterReload > 0) {
    await page.locator('.sess-item').first().click()
    // 等历史消息渲染出来
    await page.waitForFunction(
      () => document.querySelectorAll('.semi-chat-chatBox').length > 0,
      { timeout: 30000 },
    ).catch(() => {})
    await sleep(1500)
    const msgCount = await page.locator('.semi-chat-chatBox').count()
    console.log('刷新后点开会话的消息块数:', msgCount)
    await page.screenshot({ path: `${OUT}/session-restored.png` })
  }

  // ---- 5. 深链接：新开一个页面直接用 ?session= 打开 ----
  if (deepLink.includes('session=')) {
    await page.goto(`http://127.0.0.1:5173/${deepLink}#chat`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => document.querySelectorAll('.semi-chat-chatBox').length > 0,
      { timeout: 30000 },
    ).catch(() => {})
    await sleep(1200)
    const msgs = await page.locator('.semi-chat-chatBox').count()
    const cites = await page.locator('.cite-item').count()
    console.log('深链接打开消息块数:', msgs, '| 打开即显示的引用条数:', cites)
    await page.screenshot({ path: `${OUT}/session-deeplink.png` })
  }

  console.log('页面错误:', errors.length ? errors : '无')
  await browser.close()
})()
