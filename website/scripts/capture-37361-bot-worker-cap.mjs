/** Component-render evidence on capture/bot-worker-cap.html: a Bot's Settings
 * group shows "Workers at once", and its picker page offers the steps with the
 * stored limit checked. Mocked data, not the live page.
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Frames per theme (dark, light): bot-worker-cap-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-37361-bot-worker-cap.mjs ../temp-screenshots/37361
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/37361')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 840, height: 900 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/bot-worker-cap.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.click('[data-testid="crew-profile-settings-row"]')
  await page.waitForSelector('[data-testid="crew-profile-setting-max_workers"]', { timeout: 60000 })
  await page.click('[data-testid="crew-profile-setting-max_workers"]')
  await page.waitForSelector('[data-testid="crew-profile-setting-page-max_workers"]', { timeout: 60000 })
  await page.waitForTimeout(300)
  const value = (await page.locator('[data-testid="crew-profile-setting-max_workers-value"]').innerText()).trim()
  check(`[${theme}] the row reads the stored limit`, value === '3')
  const checkedName = await page.locator('[data-testid="crew-profile-setting-page-max_workers"] [role="radio"][aria-checked="true"]').innerText()
  check(`[${theme}] the picker checks the stored limit`, checkedName.trim().startsWith('3'))
  check(`[${theme}] no page errors`, errors.length === 0)
  await page.locator('[data-testid="capture-frame"]').screenshot({ path: `${OUT}/bot-worker-cap-${theme}.png` })
  await ctx.close()
}
await browser.close()
await server.close()
process.exit(failures ? 1 : 0)
