// 图谱页截图验收：打开 ?novel=..#graph，等画布稳定后截一张。
// 跑法：NODE_PATH=<playwright 模块目录> node tools/shot_graph.cjs
const { chromium } = require('playwright')

const NOVEL = 'nv_ddef13761e244a2f'
const OUT = 'shots/graph-after-rebuild.png'

;(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1600, height: 960 } })
  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
  })

  await page.goto(`http://127.0.0.1:5173/?novel=${NOVEL}#graph`, { waitUntil: 'domcontentloaded' })
  // 图谱页是 lazy 加载 + fcose 同步布局，等 canvas 真正画出来
  await page.waitForSelector('canvas', { timeout: 120000 })
  await page.waitForFunction(
    () => {
      const c = document.querySelector('canvas')
      return c && c.width > 200 && c.height > 200
    },
    { timeout: 120000 },
  )
  await new Promise((r) => setTimeout(r, 8000))

  const info = await page.evaluate(() => {
    const c = document.querySelector('canvas')
    const tips = document.body.innerText.match(/另有\s*\d+\s*个/g) || []
    return {
      canvas: c ? { w: c.width, h: c.height } : null,
      tips,
      hasSearchBox: !!document.querySelector('input[type="search"], .semi-input'),
    }
  })
  console.log(JSON.stringify(info, null, 1))
  console.log('console errors:', errors.slice(0, 5))

  await page.screenshot({ path: OUT, fullPage: false })
  await browser.close()
})()
