import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import jsQR from 'jsqr'
import { PNG } from 'pngjs'
import QRCode from 'qrcode-svg'
import { describe, expect, it } from 'vitest'
import { qrPng, qrSvg } from './qr'

const worst = readFileSync(new URL('../../../telegram/tests/qr-worst-wg.conf', import.meta.url), 'utf8')
const short = worst.replace(/^AllowedIPs = .*$/m, 'AllowedIPs = 0.0.0.0/0, ::/0')

// Снято с qrSvg из wg-qrcode.svg.get.ts до переноса: ответ маршрута не меняется ни на байт.
const svgRef = {
  worst: { length: 58308, sha256: '5bc384be5de1d1226fba1c5fb08f253c72eea75f6106dfd8e7dbc9a5e5b7f953' },
  short: { length: 14974, sha256: '5be7ea24b7ddfd1aba7b9dd8df51a7d708f0ee478b13071c96ee3a57b50b8f58' },
}

// Формула прежнего генератора бота: поле 4 модуля, Telegram ужимает фото больше 1280 px.
const botBox = (n: number) => Math.max(1, Math.min(10, Math.floor(1280 / (n + 8))))

describe.each([['worst', worst], ['short', short]] as const)('QR %s', (name, conf) => {
  const svg = qrSvg(conf)
  const n = Number(/viewBox="0 0 (\d+) /.exec(svg)![1]) - 8
  const png = PNG.sync.read(qrPng(conf))

  it('SVG равен прежнему', () => {
    expect(svg.length).toBe(svgRef[name].length)
    expect(createHash('sha256').update(svg).digest('hex')).toBe(svgRef[name].sha256)
  })

  it('сторона PNG по формуле бота из той же матрицы', () => {
    expect(png.width).toBe(png.height)
    expect(png.width).toBe((n + 8) * botBox(n))
    expect(png.width).toBeLessThanOrEqual(1280)
  })

  it('PNG декодируется в исходный текст', () => {
    const got = jsQR(new Uint8ClampedArray(png.data), png.width, png.height, { inversionAttempts: 'dontInvert' })
    expect(got?.data).toBe(conf)
  })

  it('пиксели в центрах клеток совпадают с матрицей', () => {
    const modules = (new QRCode({ content: conf, ecl: 'L' }) as unknown as { qrcode: { modules: boolean[][] } }).qrcode.modules
    expect(modules.length).toBe(n)
    const box = botBox(n)
    let bad = 0
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        const x = Math.floor((c + 4) * box + box / 2)
        const y = Math.floor((r + 4) * box + box / 2)
        const px = png.data[(y * png.width + x) * 4]
        if (px !== (modules[r]![c] ? 0 : 255)) bad++
      }
    }
    expect(bad).toBe(0)
  })

  it('поле 4 модуля со всех сторон белое', () => {
    const edge = 4 * botBox(n)
    let nonWhite = 0
    for (let y = 0; y < png.height; y++) {
      for (let x = 0; x < png.width; x++) {
        const inField = y < edge || x < edge || y >= png.height - edge || x >= png.width - edge
        if (inField && png.data[(y * png.width + x) * 4] !== 255) nonWhite++
      }
    }
    expect(nonWhite).toBe(0)
  })
})

// Тест бота (telegram/tests/test_qr_size.py) без панели меряет записанный PNG:
// запись не должна отставать от генератора.
it('PNG худшего конфига в тестах бота равен нынешнему', () => {
  const saved = PNG.sync.read(readFileSync(new URL('../../../telegram/tests/fixtures/qr-worst-wg.png', import.meta.url)))
  const now = PNG.sync.read(qrPng(worst))
  expect([saved.width, saved.height]).toEqual([now.width, now.height])
  expect(saved.data.equals(now.data)).toBe(true)
})
