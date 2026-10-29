/**
 * 流式截图：抓"正文正在长"的中途状态。
 *
 * 静态图证明不了"流式"，但能证明**中途屏幕上已经有字**——这正是修复前做不到的：
 * 那时 status 一直是 loading，生成过程中正文一个字都不显示。
 *
 * 跑法：NODE_PATH=<node_modules> node tools/shot_stream.cjs
 */
const { chromium } = require('playwright')
const fs = require('fs')

const NOVEL = process.env.SHOT_NOVEL || 'nv_ddef13761e244a2f'
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:5173'
const OUT = process.env.SHOT_DIR || 'shots'
const Q = process.env.SHOT_Q || '详细讲讲赤壁之战的经过，分几个阶段说'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  await page.goto(`${BASE}/?novel=${NOVEL}#chat`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('例如：第三章里谁救了主角？').waitFor({ timeout: 60000 })
  await sleep(2500)

  await page.getByPlaceholder('例如：第三章里谁救了主角？').fill(Q)
  await page.keyboard.press('Enter')

  let mid = false
  for (let i = 0; i < 1500; i++) {
    const s = await page.evaluate(() => {
      const boxes = Array.from(document.querySelectorAll('.semi-chat-chatBox:not(.semi-chat-chatBox-right)'))
      const last = boxes[boxes.length - 1]
      const md = last ? last.querySelector('.semi-markdownRender') : null
      return {
        len: md ? (md.innerText || '').length : 0,
        done: !!last && !!last.querySelector('.tok-bar'),
      }
    })
    if (!mid && s.len > 80) {
      mid = true
      await page.screenshot({ path: `${OUT}/stream-mid.png` })
      console.log('中途截图：正文', s.len, '字（此时仍在生成）')
      // 继续到结束，确认没有"答完消失再出现"
    }
    if (mid && s.done) break
    await sleep(200)
  }
  await sleep(1200)
  await page.screenshot({ path: `${OUT}/stream-done.png` })
  console.log('完成截图：stream-done.png')
  await browser.close()
})()
