/**
 * 字号一致性回归检查：用户提问 vs AI 回答。
 *
 * 背景：Semi 的 MarkdownRender 只给 p / strong 套了 Typography（14px），
 * 列表（ul / ol / li）是裸元素，继承 body 的 16px 且 line-height: normal。
 * 于是同一个回答里"正文 14px、编号列表 16px"，用户看到的就是
 * "AI 回答的字比我的提问大"。styles.css 里已经把列表拉回 14px / 20px，
 * 这个脚本用来防止将来换 Semi 版本或改样式时又退回去。
 *
 * 跑法：NODE_PATH=<node_modules> node tools/probe_font.cjs
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5174'
const OUT = process.env.SHOT_DIR || 'shots'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  await page.goto(`${BASE}/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('例如：第三章里谁救了主角？').waitFor({ timeout: 60000 })
  await sleep(2500)

  // 挑一个带编号列表的会话（有 li 才验得出这条）
  const items = page.locator('.sess-item')
  const n = await items.count()
  let picked = false
  for (let i = 0; i < n; i++) {
    await items.nth(i).click()
    await sleep(2000)
    if ((await page.locator('.semi-chat .semi-markdownRender li').count()) > 0) {
      picked = true
      console.log('选用会话:', (await items.nth(i).innerText()).split('\n')[0])
      break
    }
  }
  if (!picked) console.log('!! 没找到含列表的会话，只验正文')

  const out = await page.evaluate(() => {
    const fs = (el) => (el ? getComputedStyle(el).fontSize : null)
    const lh = (el) => (el ? getComputedStyle(el).lineHeight : null)
    const userP = document.querySelector('.semi-chat-chatBox-right .semi-markdownRender p')
    const aiP = document.querySelector(
      '.semi-chat-chatBox:not(.semi-chat-chatBox-right) .semi-markdownRender p',
    )
    const aiLi = document.querySelector(
      '.semi-chat-chatBox:not(.semi-chat-chatBox-right) .semi-markdownRender li',
    )
    const aiH = document.querySelector(
      '.semi-chat-chatBox:not(.semi-chat-chatBox-right) .semi-markdownRender h1, .semi-chat-chatBox:not(.semi-chat-chatBox-right) .semi-markdownRender h2, .semi-chat-chatBox:not(.semi-chat-chatBox-right) .semi-markdownRender h3',
    )
    return {
      userP: [fs(userP), lh(userP)],
      aiP: [fs(aiP), lh(aiP)],
      aiLi: [fs(aiLi), lh(aiLi)],
      aiH: [fs(aiH), lh(aiH)],
    }
  })

  console.log('用户提问 p :', out.userP.join(' / '))
  console.log('AI 正文  p :', out.aiP.join(' / '))
  console.log('AI 列表  li:', out.aiLi.join(' / '))
  console.log('AI 标题  h :', out.aiH.join(' / '), '(标题本来就该更大，不作比较)')

  const same = out.userP[0] === out.aiP[0] && out.aiP[0] === out.aiLi[0]
  console.log('\n字号一致:', same, same ? '✓' : '✗ 列表和正文又不一样高了')

  await page.screenshot({ path: `${OUT}/fonts.png` })
  await browser.close()
  if (!same) process.exitCode = 1
})()
