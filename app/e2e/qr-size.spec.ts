import type { Locator, Page, Response } from '@playwright/test'
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
// на экране 390 px QR занимает не меньше 90 % ширины контента.
// QR_MODE=full - панель с NUXT_WG_SPLIT_LOCAL=false (край 2), иначе сплит;
// QR_ENTRY_IP - IP входа, который панель вырезает из AllowedIPs (худший - 45.x);
// QR_CONF_OUT - куда положить выданный .conf для теста бота.
const MIN_PX = 4
const MIN_SHARE = 0.9
const DESKTOP = { width: 1280, height: 800 }
const PHONE = { width: 390, height: 844 }
const MODE = process.env.QR_MODE === 'full' ? 'full' : 'split'
const ENTRY_IP = process.env.QR_ENTRY_IP || ''

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

test.describe('VPN2-598: размер QR на экране', () => {
  test('1. панель WG, сплит, худший вход: версия ≤ 27 (ecl L), ≥ 4 px/модуль, на 390 px ≥ 90 % ширины', async ({ page }) => {
    test.skip(MODE !== 'split', 'панель без сплита: край 2')
    const r = await wgDensity(page, 'split')
    expect(r.allowed).not.toBe('0.0.0.0/0, ::/0')
    if (ENTRY_IP) {
      // Худший конфиг: IP входа вырезан из своего префикса, иначе петля маршрута.
      expect(r.allowed).not.toContain('32.0.0.0/3')
      expect(r.allowed.split(', ').filter(p => p.includes('.')).length).toBe(72)
      const out = process.env.QR_CONF_OUT
      if (out) await writeFile(out, r.conf)
    }
    expect(r.qr.text, 'QR раскодируется ровно в .conf кнопки «Скачать»').toBe(r.conf)
    expect.soft(String(r.qr.ecl)).toBe('L')
    expect.soft(r.qr.version, 'версия QR').toBeLessThanOrEqual(27)
    expect.soft(r.pxDesk, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
    expect.soft(r.share, 'доля ширины контента при 390x844').toBeGreaterThanOrEqual(MIN_SHARE)
  })

  test('2. панель WG, сплит выключен: ≥ 4 px/модуль, на 390 px ≥ 90 % ширины', async ({ page }) => {
    test.skip(MODE !== 'full', 'нужна панель с NUXT_WG_SPLIT_LOCAL=false (QR_MODE=full)')
    const r = await wgDensity(page, 'full')
    expect(r.allowed).toBe('0.0.0.0/0, ::/0')
    expect(r.qr.text, 'QR раскодируется ровно в .conf кнопки «Скачать»').toBe(r.conf)
    expect.soft(r.pxDesk, 'CSS px на модуль при 1280x800').toBeGreaterThanOrEqual(MIN_PX)
    expect.soft(r.share, 'доля ширины контента при 390x844').toBeGreaterThanOrEqual(MIN_SHARE)
  })

  test('3. панель TOTP: ≥ 4 px/модуль при 1280x800, QR раскодируется в otpauth', async ({ page }) => {
    test.skip(MODE !== 'split', 'проверяется на основной панели')
    await page.setViewportSize(DESKTOP)
    await loginApi(page)
    await page.goto('/settings')
    await hydrated(page)
    await page.locator('#tfa').getByLabel('Текущий пароль', { exact: true }).fill(ADMIN_PASSWORD)
    const response = page.waitForResponse(r => r.url().endsWith('/api/auth/two-factor/enable'))
    await page.getByRole('button', { name: 'Включить 2FA', exact: true }).click()
    const { totpURI } = await (await response).json()
    const svg = page.locator('#tfa .qr > svg')
    await expect(svg).toBeVisible()
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

  test('4. аварийная панель hy2: ≥ 4 px/модуль при 1280x800, на 390 px ≥ 90 % ширины, PNG раскодируется в URI', async ({ browser }) => {
    test.skip(MODE !== 'split', 'проверяется один раз, на основной панели')
    test.setTimeout(180_000)
    const tools = ['qrencode', 'zbarimg'].filter(t => { try { execFileSync('which', [t]); return false } catch { return true } })
    test.skip(tools.length > 0, `нет ${tools.join(', ')}: PNG аварийки рисует qrencode, раскодирует zbarimg`)
    execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts'], { cwd: process.cwd(), stdio: 'pipe' })
    const directory = await mkdtemp(join(tmpdir(), 'emergency-qr-'))
    const fixture = spawn('python3', [resolve('../infra/tests/emergency_qr_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
    fixture.stderr.resume()
    const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
    const startup = JSON.parse((await lines.next()).value!) as { port: number, password: string, uri: string }
    const origin = `https://127.0.0.1:${startup.port}`
    const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: DESKTOP })
    const page = await context.newPage()
    try {
      await page.goto(origin)
      await page.getByLabel('Пароль', { exact: true }).fill(startup.password)
      await page.getByRole('button', { name: 'Войти', exact: true }).click()
      await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()

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
      fixture.stdin.end()
    }
  })

  test('7. перевыпуск ключей: QR в окне раскодируется в новый PrivateKey, не из кэша', async ({ page }) => {
    test.skip(MODE !== 'split', 'проверяется на основной панели')
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
})

// Край 6 для бота: тест бота берёт настоящий конфиг этой панели (QR_CONF_OUT),
// для запуска без панели - копию в telegram/tests/qr-worst-wg.conf. Копия не
// должна отставать от панели: AllowedIPs совпадают.
test('6. копия худшего конфига для теста бота совпадает с выданным панелью', async () => {
  test.skip(MODE !== 'split' || !ENTRY_IP || !process.env.QR_CONF_OUT, 'нужна панель со входом QR_ENTRY_IP и QR_CONF_OUT')
  const live = await readFile(process.env.QR_CONF_OUT!, 'utf8')
  const copy = await readFile(resolve('../telegram/tests/qr-worst-wg.conf'), 'utf8')
  expect(field(copy, 'AllowedIPs')).toBe(field(live, 'AllowedIPs'))
  expect(field(copy, 'Endpoint')).toBe(field(live, 'Endpoint'))
})
