// 图谱页右侧详情面板验收：小窗口（1080×620）下选刘备，检查面板能否滚到底。
// 跑法：NODE_PATH=<playwright 模块目录> node tools/shot_graph_detail.cjs
const { chromium } = require('playwright')

const NOVEL = 'nv_ddef13761e244a2f'
const NAME = process.argv[2] || '刘备'
const W = Number(process.argv[3] || 1080)
const H = Number(process.argv[4] || 620)

;(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: W, height: H } })
  const errors = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200))
  })

  await page.goto(`http://127.0.0.1:5173/?novel=${NOVEL}#graph`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('canvas', { timeout: 120000 })
  await page.waitForFunction(
    () => {
      const c = document.querySelector('canvas')
      return c && c.width > 200 && c.height > 200
    },
    { timeout: 120000 },
  )
  await new Promise((r) => setTimeout(r, 6000))

  // 左栏点实体（列表项文本 = 名字 + 提及数）
  await page.locator('.entity-item', { hasText: NAME }).first().click()
  await page.waitForSelector('.detail .rel-item', { timeout: 60000 })
  await new Promise((r) => setTimeout(r, 1500))

  const before = await page.evaluate(() => {
    const d = document.querySelector('.detail')
    const card = d?.closest('.semi-card')
    const rels = document.querySelectorAll('.rel-item').length
    return {
      viewport: { w: window.innerWidth, h: window.innerHeight },
      detail: d && {
        clientH: d.clientHeight,
        scrollH: d.scrollHeight,
        canScroll: d.scrollHeight > d.clientHeight + 2,
        overflowY: getComputedStyle(d).overflowY,
      },
      cardH: card ? Math.round(card.getBoundingClientRect().height) : null,
      cardBottom: card ? Math.round(card.getBoundingClientRect().bottom) : null,
      fitsViewport: card ? card.getBoundingClientRect().bottom <= window.innerHeight + 1 : null,
      pageScroll: document.querySelector('.app-content')?.scrollHeight >
        document.querySelector('.app-content')?.clientHeight,
      renderedRels: rels,
      moreBtn: !!Array.from(document.querySelectorAll('button')).find((b) =>
        b.textContent.includes('显示更多'),
      ),
      secTitle: document.querySelector('.detail-sec')?.textContent?.trim().slice(0, 40),
    }
  })
  console.log('顶部状态:', JSON.stringify(before, null, 1))

  await page.screenshot({ path: `shots/graph-detail-top-${W}x${H}.png` })

  // 滚到底，确认最后一条关系 + 出现章节都能看到
  const after = await page.evaluate(() => {
    const d = document.querySelector('.detail')
    d.scrollTop = d.scrollHeight
    return new Promise((res) =>
      setTimeout(() => {
        const rels = document.querySelectorAll('.rel-item')
        const last = rels[rels.length - 1]
        const box = d.getBoundingClientRect()
        const lb = last?.getBoundingClientRect()
        res({
          scrollTop: Math.round(d.scrollTop),
          atBottom: Math.abs(d.scrollTop + d.clientHeight - d.scrollHeight) < 3,
          lastVisible: !!(lb && lb.bottom <= box.bottom + 2),
          lastRel: last?.innerText.replace(/\s+/g, ' ').slice(0, 80),
          chapStrip: !!document.querySelector('.chap-strip'),
        })
      }, 300),
    )
  })
  console.log('滚到底:', JSON.stringify(after, null, 1))
  await page.screenshot({ path: `shots/graph-detail-bottom-${W}x${H}.png` })

  console.log('console errors:', errors.slice(0, 5))
  await browser.close()
})()
