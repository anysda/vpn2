import type { Browser, Locator, Page, Response } from '@playwright/test'
import { BitMatrix, QRCodeReader } from '@zxing/library'
import { execFileSync, spawn } from 'node:child_process'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { ADMIN_PASSWORD, downloadText, expect, hydrated, loginApi, test, toast, uniq } from './support/fixtures'
import { addDevice, configDialog, deviceCard, openClient } from './support/clients'

// VPN2-598: камера телефона должна брать каждый QR с экрана с первого наведения.
// Предел: не меньше 4 CSS px на модуль (тихая зона входит в счёт модулей),
// на экране 390 px QR занимает не меньше 90 % ширины контента и не меньше
// 2,5 CSS px на модуль; на отрисованном снимке вокруг модулей не меньше 4
// модулей фона в светлой и тёмной теме.
// Гоняет только e2e/qr-size.sh: QR_MODE=split (теги @split) - панель со сплитом
// и вырезанным IP входа QR_ENTRY_IP (худший - 45.x), QR_MODE=full (@full) -
// панель с NUXT_WG_SPLIT_LOCAL=false; QR_CONF_OUT - куда положить выданный
// .conf для теста бота.
const MIN_PX = 4
const MIN_PHONE_PX = 2.5
const MIN_QUIET = 4
const MIN_SHARE = 0.9
const DESKTOP = { width: 1280, height: 800 }
const PHONE = { width: 390, height: 844 }
const ENTRY_IP = process.env.QR_ENTRY_IP || ''
const SCREENS = [
  { tag: '1280x800 светлая', viewport: DESKTOP, dpr: 1, scheme: 'light' },
  { tag: '1280x800 тёмная', viewport: DESKTOP, dpr: 1, scheme: 'dark' },
  { tag: '390x844 DPR 3 светлая', viewport: PHONE, dpr: 3, scheme: 'light' },
  { tag: '390x844 DPR 3 тёмная', viewport: PHONE, dpr: 3, scheme: 'dark' },
] as const
type Screen = typeof SCREENS[number]
const isPhone = (s: Screen) => s.viewport.width < 500

class ModuleReader extends QRCodeReader {
  decodeModules(matrix: BitMatrix) {
    return this.getDecoder().decodeBitMatrix(matrix)
  }
}

/** Модули QR из SVG панели: размер viewBox, тихая зона и раскодированный текст. */
function readWgSvg(svg: string) {
  const size = Number(/viewBox="0 0 (\d+) /.exec(svg)![1])
  const runs = [...svg.matchAll(/M(\d+) (\d+)h(\d+)v1h-\d+z/g)]
  const pad = Math.min(...runs.map(r => Number(r[1])))
  const modules = size - 2 * pad
  const matrix = new BitMatrix(modules)
  for (const [, x, y, length] of runs) matrix.setRegion(Number(x) - pad, Number(y) - pad, Number(length), 1)
  const decoded = new ModuleReader().decodeModules(matrix)
  return { units: size, modules, version: (modules - 17) / 4, ecl: decoded.getECLevel(), text: decoded.getText() }
}

/** Ширина области QR на экране без рамки и отступов и ширина контента родителя. */
async function box(el: Locator) {
  return el.evaluate((node) => {
    const own = getComputedStyle(node)
    const rect = node.getBoundingClientRect()
    const parent = node.parentElement!
    const ps = getComputedStyle(parent)
    const inner = rect.width - parseFloat(own.paddingLeft) - parseFloat(own.paddingRight)
      - parseFloat(own.borderLeftWidth) - parseFloat(own.borderRightWidth)
    const content = parent.clientWidth - parseFloat(ps.paddingLeft) - parseFloat(ps.paddingRight)
    return { inner, outer: rect.width, content }
  })
}

function field(conf: string, key: string): string | undefined {
  return new RegExp(`^${key}\\s*=\\s*(.+)$`, 'm').exec(conf)?.[1]?.trim()
}

/** Тела ответов wg-qrcode.svg, которые реально получил браузер. */
function qrResponses(page: Page) {
  const got: string[] = []
  page.on('response', async (r: Response) => {
    if (!/\/wg-qrcode\.svg/.test(r.url()) || r.status() !== 200) return
    got.push(await r.text().catch(() => ''))
  })
  return got
}

async function qrLoaded(img: Locator) {
  await expect(img).toBeVisible()
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0)
}

async function downloadConf(page: Page, dlg: Locator) {
  const wait = page.waitForEvent('download')
  await dlg.getByRole('button', { name: 'Скачать', exact: true }).click()
  return downloadText(await wait)
}

/** Клиент с девайсом через API, окно WG открыто в текущем размере окна. */
async function openWg(page: Page, client: string, device: string) {
  await page.goto('/clients')
  await hydrated(page)
  await openClient(page, client)
  await deviceCard(page, device).wg.click()
  const dlg = configDialog(page, device, 'WireGuard')
  const img = dlg.locator('img[alt="WireGuard QR"]')
  await qrLoaded(img)
  return { dlg, img }
}

async function wgDensity(page: Page, mode: 'split' | 'full') {
  const qrs = qrResponses(page)
  await loginApi(page)
  const client = uniq(`qr-${mode}`)
  const device = 'phone'
  const created = await page.request.post('/api/clients', { data: { name: client } })
  expect(created.ok()).toBeTruthy()
  const { id } = await created.json()
  expect((await page.request.post(`/api/clients/${id}/devices`, { data: { name: device } })).ok()).toBeTruthy()

  await page.setViewportSize(DESKTOP)
  const { dlg, img } = await openWg(page, client, device)
  const conf = await downloadConf(page, dlg)
  await expect.poll(() => qrs.length).toBeGreaterThan(0)
  const qr = readWgSvg(qrs.at(-1)!)
  const desk = await box(img)
  await page.keyboard.press('Escape')
  await expect(dlg).toBeHidden()

  await page.setViewportSize(PHONE)
  const phone = await openWg(page, client, device)
  const narrow = await box(phone.img)
  await page.keyboard.press('Escape')

  const allowed = field(conf, 'AllowedIPs')!
  const pxDesk = desk.inner / qr.units
  const share = narrow.outer / narrow.content
  console.log(`QR WG ${mode}: ${conf.length} байт, AllowedIPs ${allowed.split(', ').length}, версия ${qr.version}, `
    + `ecl ${qr.ecl}, модулей ${qr.modules} (+тихая зона ${qr.units}), 1280x800: ${desk.inner.toFixed(1)} px = `
    + `${pxDesk.toFixed(2)} px/модуль, 390x844: ${narrow.outer.toFixed(1)} из ${narrow.content.toFixed(1)} px = `
    + `${(share * 100).toFixed(1)} %, ${(narrow.inner / qr.units).toFixed(2)} px/модуль`)
  return { conf, allowed, qr, pxDesk, share }
}

/** Контекст браузера под экран: размер, плотность пикселей, тема, телефонный режим. */
function screenContext(browser: Browser, s: Screen, baseURL?: string) {
  return browser.newContext({
    baseURL, viewport: s.viewport, deviceScaleFactor: s.dpr, colorScheme: s.scheme,
    isMobile: isPhone(s), hasTouch: isPhone(s), locale: 'ru-RU', acceptDownloads: true, ignoreHTTPSErrors: true,
    extraHTTPHeaders: baseURL ? { origin: new URL(baseURL).origin } : {},
  })
}

/**
 * QR так, как его видит камера: снимок экрана вокруг элемента в пикселях
 * устройства. Модули - тёмные точки внутри элемента (без срезанных скруглением
 * углов), модуль - по внешнему кольцу искателя. Тихая зона - расстояние от
 * блока модулей до ближайшей тёмной точки снаружи или до края экрана, в модулях.
 */
async function rendered(page: Page, el: Locator) {
  await el.scrollIntoViewIfNeeded()
  await page.evaluate(() => Promise.all(document.getAnimations()
    .filter(a => a.effect?.getComputedTiming().endTime !== Infinity)
    .map(a => a.finished.catch(() => undefined))))
  const q = await el.evaluate((node) => {
    const r = node.getBoundingClientRect()
    return { x: r.x, y: r.y, w: r.width, h: r.height, radius: parseFloat(getComputedStyle(node).borderTopLeftRadius) || 0, vw: innerWidth, vh: innerHeight }
  })
  const margin = 40
  const x = Math.max(0, q.x - margin)
  const y = Math.max(0, q.y - margin)
  const clip = { x, y, width: Math.min(q.vw, q.x + q.w + margin) - x, height: Math.min(q.vh, q.y + q.h + margin) - y }
  const png = await page.screenshot({ clip })
  const edges = { left: x === 0, top: y === 0, right: clip.x + clip.width >= q.vw, bottom: clip.y + clip.height >= q.vh }
  const scratch = await page.context().newPage()
  try {
    return await scratch.evaluate(async ({ b64, q, clip, edges }) => {
      const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
      const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
      const W = bitmap.width
      const H = bitmap.height
      const g = new OffscreenCanvas(W, H).getContext('2d')!
      g.drawImage(bitmap, 0, 0)
      const d = g.getImageData(0, 0, W, H).data
      const dark = (x: number, y: number) => d[(y * W + x) * 4]! + d[(y * W + x) * 4 + 1]! + d[(y * W + x) * 4 + 2]! < 384
      const k = W / clip.width
      const e = 2 * k
      const x0 = Math.round((q.x - clip.x) * k + e)
      const y0 = Math.round((q.y - clip.y) * k + e)
      const x1 = Math.round((q.x - clip.x + q.w) * k - e) - 1
      const y1 = Math.round((q.y - clip.y + q.h) * k - e) - 1
      const rad = q.radius * k
      const inside = (x: number, y: number) => {
        if (x < x0 || x > x1 || y < y0 || y > y1) return false
        const cx = x < x0 + rad ? x0 + rad : x > x1 - rad ? x1 - rad : x
        const cy = y < y0 + rad ? y0 + rad : y > y1 - rad ? y1 - rad : y
        return (x - cx) ** 2 + (y - cy) ** 2 <= rad ** 2
      }
      let bx0 = W, by0 = H, bx1 = -1, by1 = -1
      for (let y = y0; y <= y1; y++) {
        for (let x = x0; x <= x1; x++) {
          if (!dark(x, y) || !inside(x, y)) continue
          bx0 = Math.min(bx0, x); bx1 = Math.max(bx1, x); by0 = Math.min(by0, y); by1 = Math.max(by1, y)
        }
      }
      if (bx1 < 0) return null
      let run = 0
      while (bx0 + run <= bx1 && dark(bx0 + run, by0)) run++
      // Модули неровные при дробном масштабе: сторона QR - ближайшая допустимая, 17 + 4 x версия.
      const modules = 4 * Math.round(((bx1 - bx0 + 1) / (run / 7) - 17) / 4) + 17
      let near = Infinity
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          if (x >= bx0 && x <= bx1 && y >= by0 && y <= by1) continue
          if (!dark(x, y)) continue
          const dx = Math.max(0, bx0 - x, x - bx1)
          const dy = Math.max(0, by0 - y, y - by1)
          near = Math.min(near, Math.hypot(dx, dy))
        }
      }
      if (edges.left) near = Math.min(near, bx0 + 1)
      if (edges.top) near = Math.min(near, by0 + 1)
      if (edges.right) near = Math.min(near, W - bx1)
      if (edges.bottom) near = Math.min(near, H - by1)
      return { modules, css: (bx1 - bx0 + 1) / k / modules, quiet: near / ((bx1 - bx0 + 1) / modules) }
    }, { b64: png.toString('base64'), q, clip, edges })
  }
  finally {
    await scratch.close()
  }
}

/** Края 2 и 3: тихая зона на любом экране, плотность на телефоне. */
async function checkRendered(what: string, s: Screen, page: Page, el: Locator) {
  const r = await rendered(page, el)
  expect(r, `${what}, ${s.tag}: на снимке нет модулей QR`).not.toBeNull()
  console.log(`QR ${what}, ${s.tag}: на снимке ${r!.modules} модулей, ${r!.css.toFixed(2)} CSS px/модуль, `
    + `тихая зона ${r!.quiet.toFixed(2)} модуля`)
  expect.soft(r!.quiet, `${what}, ${s.tag}: тихая зона на снимке, модулей`).toBeGreaterThanOrEqual(MIN_QUIET)
  if (isPhone(s)) expect.soft(r!.css, `${what}, ${s.tag}: CSS px на модуль`).toBeGreaterThanOrEqual(MIN_PHONE_PX)
}

/** Аварийная панель с настоящим профилем hy2 (infra/tests/emergency_qr_fixture.py). */
async function emergencyFixture() {
  const directory = await mkdtemp(join(tmpdir(), 'emergency-qr-'))
  const fixture = spawn('python3', [resolve('../infra/tests/emergency_qr_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
  fixture.stderr.resume()
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
  const startup = JSON.parse((await lines.next()).value!) as { port: number, password: string, uri: string }
  return { directory, origin: `https://127.0.0.1:${startup.port}`, ...startup, stop: () => fixture.stdin.end() }
}

async function emergencyLogin(page: Page, origin: string, password: string) {
  await page.goto(origin)
  await page.getByLabel('Пароль', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()
}

async function startTotp(page: Page) {
  await page.goto('/settings')
  await hydrated(page)
  await page.locator('#tfa').getByLabel('Текущий пароль', { exact: true }).fill(ADMIN_PASSWORD)
  const response = page.waitForResponse(r => r.url().endsWith('/api/auth/two-factor/enable'))
  await page.getByRole('button', { name: 'Включить 2FA', exact: true }).click()
  const { totpURI } = await (await response).json() as { totpURI: string }
  await expect(page.locator('#tfa .qr > svg')).toBeVisible()
  return totpURI
}

test.describe('VPN2-598: размер QR на экране', () => {
  test('1. панель WG, сплит, худший вход: версия ≤ 27 (ecl L), ≥ 4 px/модуль, на 390 px ≥ 90 % ширины', { tag: '@split' }, async ({ page }) => {
    expect(ENTRY_IP, 'худший конфиг: QR_ENTRY_IP - вход 45.x').toMatch(/^45\./)
    const r = await wgDensity(page, 'split')
    expect(r.allowed).not.toBe('0.0.0.0/0, ::/0')
    // Худший конфиг: IP входа вырезан из своего префикса, иначе петля маршрута.
    expect(r.allowed).not.toContain('32.0.0.0/3')
    expect(r.allowed.split(', ').filter(p => p.includes('.')).length).toBe(72)
    await writeFile(process.env.QR_CONF_OUT!, r.conf)
    expect(r.qr.text, 'QR раскодируется ровно в .conf кнопки «Скачать»').toBe(r.conf)
    expect.soft(String(r.qr.ecl)).toBe('L')
    expect.soft(r.qr.version, 'версия QR').toBeLessThanOrEqual(27)
    expect.soft(r.pxDesk, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
    expect.soft(r.share, 'доля ширины контента при 390x844').toBeGreaterThanOrEqual(MIN_SHARE)
  })

  test('2. панель WG, сплит выключен: ≥ 4 px/модуль, на 390 px ≥ 90 % ширины', { tag: '@full' }, async ({ page }) => {
    const r = await wgDensity(page, 'full')
    expect(r.allowed).toBe('0.0.0.0/0, ::/0')
    expect(r.qr.text, 'QR раскодируется ровно в .conf кнопки «Скачать»').toBe(r.conf)
    expect.soft(r.pxDesk, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
    expect.soft(r.share, 'доля ширины контента при 390x844').toBeGreaterThanOrEqual(MIN_SHARE)
  })

  test('3. панель TOTP: ≥ 4 px/модуль при 1280x800, QR раскодируется в otpauth', { tag: '@split' }, async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await loginApi(page)
    const totpURI = await startTotp(page)
    const svg = page.locator('#tfa .qr > svg')
    const { units, cells } = await svg.evaluate(el => ({
      units: Number(el.getAttribute('viewBox')!.split(' ')[2]),
      cells: [...el.querySelectorAll('rect')].slice(1).map(r => [Number(r.getAttribute('x')), Number(r.getAttribute('y'))] as const),
    }))
    const pad = Math.min(...cells.map(c => c[0]))
    const matrix = new BitMatrix(units - 2 * pad)
    for (const [x, y] of cells) matrix.set(x - pad, y - pad)
    const decoded = new ModuleReader().decodeModules(matrix)
    // Квадрат QR - сам svg внутри белой рамки.
    const width = await svg.evaluate(el => el.getBoundingClientRect().width)
    const px = width / units
    console.log(`QR TOTP: версия ${(units - 2 * pad - 17) / 4}, модулей ${units - 2 * pad} (+тихая зона ${units}), `
      + `1280x800: ${width.toFixed(1)} px = ${px.toFixed(2)} px/модуль`)
    await page.getByRole('button', { name: 'Отмена', exact: true }).click()
    expect(decoded.getText(), 'QR раскодируется ровно в otpauth').toBe(totpURI)
    expect.soft(px, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
  })

  test('4. аварийная панель hy2: ≥ 4 px/модуль при 1280x800, на 390 px ≥ 90 % ширины, PNG раскодируется в URI', { tag: '@split' }, async ({ browser }) => {
    test.setTimeout(180_000)
    // PNG аварийки рисует qrencode, раскодирует zbarimg: без них e2e/qr-size.sh не стартует.
    execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts'], { cwd: process.cwd(), stdio: 'pipe' })
    const startup = await emergencyFixture()
    const { directory, origin } = startup
    const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: DESKTOP })
    const page = await context.newPage()
    try {
      await emergencyLogin(page, origin, startup.password)

      const measure = async () => {
        await page.getByRole('button', { name: 'Hysteria2', exact: true }).first().click()
        const img = page.locator('img.qr')
        await qrLoaded(img)
        // Модуль PNG по внешнему кольцу искателя слева сверху: 7 модулей тёмного подряд.
        const units = await img.evaluate((el: HTMLImageElement) => {
          const c = document.createElement('canvas')
          c.width = el.naturalWidth
          c.height = el.naturalHeight
          const g = c.getContext('2d')!
          g.drawImage(el, 0, 0)
          const d = g.getImageData(0, 0, c.width, c.height).data
          const dark = (x: number, y: number) => d[(y * c.width + x) * 4]! < 128
          for (let y = 0; y < c.height; y++) {
            for (let x = 0; x < c.width; x++) {
              if (!dark(x, y)) continue
              let run = 0
              while (x + run < c.width && dark(x + run, y)) run++
              return c.width / (run / 7)
            }
          }
          return 0
        })
        return { units, size: await box(img) }
      }
      const desk = await measure()
      await page.setViewportSize(PHONE)
      await page.reload()
      await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()
      await page.getByTestId('client-row').first().click()
      const phone = await measure()

      const png = await page.request.get(`${origin}/api/profile/11/qr`)
      const file = join(directory, 'shown.png')
      await writeFile(file, await png.body())
      const text = execFileSync('zbarimg', ['-q', '--raw', file], { encoding: 'utf8' }).replace(/\n$/, '')
      const modules = desk.units - 8
      const pxDesk = desk.size.inner / desk.units
      const share = phone.size.outer / phone.size.content
      console.log(`QR аварийки hy2: URI ${startup.uri.length} байт, версия ${(modules - 17) / 4}, модулей ${modules} `
        + `(+тихая зона ${desk.units}), 1280x800: ${desk.size.inner.toFixed(1)} px = ${pxDesk.toFixed(2)} px/модуль, `
        + `390x844: ${phone.size.outer.toFixed(1)} из ${phone.size.content.toFixed(1)} px = ${(share * 100).toFixed(1)} %, `
        + `${(phone.size.inner / phone.units).toFixed(2)} px/модуль`)
      expect(text, 'PNG раскодируется ровно в URI профиля').toBe(startup.uri)
      expect.soft(pxDesk, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
      expect.soft(share, 'доля ширины контента при 390x844').toBeGreaterThanOrEqual(MIN_SHARE)
    }
    finally {
      await context.close()
      startup.stop()
    }
  })

  test('7. перевыпуск ключей: QR в окне раскодируется в новый PrivateKey, не из кэша', { tag: '@split' }, async ({ page }) => {
    const qrs = qrResponses(page)
    await page.setViewportSize(DESKTOP)
    await loginApi(page)
    const client = uniq('qr-rekey')
    const device = 'phone'
    await page.goto('/clients')
    await hydrated(page)
    const created = await page.request.post('/api/clients', { data: { name: client } })
    expect(created.ok()).toBeTruthy()
    await page.reload()
    await hydrated(page)
    await openClient(page, client)
    await addDevice(page, device)
    const dc = deviceCard(page, device)

    await dc.wg.click()
    const dlg = configDialog(page, device, 'WireGuard')
    await qrLoaded(dlg.locator('img[alt="WireGuard QR"]'))
    const key1 = field(await downloadConf(page, dlg), 'PrivateKey')
    await expect.poll(() => qrs.length).toBeGreaterThan(0)
    expect(field(readWgSvg(qrs.at(-1)!).text, 'PrivateKey')).toBe(key1)
    await page.keyboard.press('Escape')
    await expect(dlg).toBeHidden()

    await dc.reissue.click()
    const confirm = page.getByRole('dialog', { name: 'Перевыпустить ключи девайса?' })
    await confirm.getByRole('button', { name: 'Перевыпустить' }).click()
    await expect(toast(page, 'Перевыпуск выполняется')).toBeVisible()
    await expect(page.getByTestId('client-detail').getByRole('status')).toContainText('Ключи перевыпущены', { timeout: 45_000 })

    const before = qrs.length
    await dc.wg.click()
    await qrLoaded(dlg.locator('img[alt="WireGuard QR"]'))
    const key2 = field(await downloadConf(page, dlg), 'PrivateKey')
    expect(key2).not.toBe(key1)
    await expect.poll(() => qrs.length, { message: 'QR не перезапрошен после перевыпуска' }).toBeGreaterThan(before)
    const shown = field(readWgSvg(qrs.at(-1)!).text, 'PrivateKey')
    console.log(`QR после перевыпуска: старый ключ ${key1?.slice(0, 6)}…, новый ${key2?.slice(0, 6)}…, в QR ${shown?.slice(0, 6)}…`)
    expect(shown, 'QR после перевыпуска').toBe(key2)
    await page.keyboard.press('Escape')
  })

  test('8. панель WG, худший конфиг: тихая зона ≥ 4 модулей на снимке (светлая и тёмная, 1280 и 390), на 390x844 DPR 3 ≥ 2,5 px/модуль', { tag: '@split' }, async ({ browser, page, baseURL }) => {
    test.setTimeout(180_000)
    await loginApi(page)
    const client = uniq('qr-shot')
    const created = await page.request.post('/api/clients', { data: { name: client } })
    expect(created.ok()).toBeTruthy()
    const { id } = await created.json()
    expect((await page.request.post(`/api/clients/${id}/devices`, { data: { name: 'phone' } })).ok()).toBeTruthy()
    for (const s of SCREENS) {
      const context = await screenContext(browser, s, baseURL)
      try {
        const shot = await context.newPage()
        await loginApi(shot)
        const { img } = await openWg(shot, client, 'phone')
        await checkRendered('WG', s, shot, img)
      }
      finally {
        await context.close()
      }
    }
  })

  test('9. аварийная панель hy2: тихая зона ≥ 4 модулей на снимке (светлая и тёмная, 1280 и 390), на 390x844 DPR 3 ≥ 2,5 px/модуль', { tag: '@split' }, async ({ browser }) => {
    test.setTimeout(180_000)
    execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts'], { cwd: process.cwd(), stdio: 'pipe' })
    const startup = await emergencyFixture()
    try {
      for (const s of SCREENS) {
        const context = await screenContext(browser, s)
        try {
          const shot = await context.newPage()
          await emergencyLogin(shot, startup.origin, startup.password)
          if (isPhone(s)) await shot.getByTestId('client-row').first().click()
          await shot.getByRole('button', { name: 'Hysteria2', exact: true }).first().click()
          const img = shot.locator('img.qr')
          await qrLoaded(img)
          await checkRendered('hy2', s, shot, img)
        }
        finally {
          await context.close()
        }
      }
    }
    finally {
      startup.stop()
    }
  })

  test('10. панель TOTP: тихая зона ≥ 4 модулей на снимке (светлая и тёмная, 1280 и 390), на 390x844 DPR 3 ≥ 2,5 px/модуль', { tag: '@split' }, async ({ browser, baseURL }) => {
    test.setTimeout(180_000)
    for (const s of SCREENS) {
      const context = await screenContext(browser, s, baseURL)
      try {
        const shot = await context.newPage()
        await loginApi(shot)
        await startTotp(shot)
        await checkRendered('TOTP', s, shot, shot.locator('#tfa .qr'))
        await shot.getByRole('button', { name: 'Отмена', exact: true }).click()
      }
      finally {
        await context.close()
      }
    }
  })
})

// Край 6 для бота: тест бота берёт настоящий конфиг этой панели (QR_CONF_OUT),
// для запуска без панели - копию в telegram/tests/qr-worst-wg.conf. Копия не
// должна отставать от панели: AllowedIPs совпадают.
test('6. копия худшего конфига для теста бота совпадает с выданным панелью', { tag: '@split' }, async () => {
  const live = await readFile(process.env.QR_CONF_OUT!, 'utf8')
  const copy = await readFile(resolve('../telegram/tests/qr-worst-wg.conf'), 'utf8')
  expect(field(copy, 'AllowedIPs')).toBe(field(live, 'AllowedIPs'))
  expect(field(copy, 'Endpoint')).toBe(field(live, 'Endpoint'))
})
