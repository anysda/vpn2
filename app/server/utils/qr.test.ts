import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import jsQR from 'jsqr'
import { PNG } from 'pngjs'
import { describe, expect, it } from 'vitest'
import { qrPng, qrSvg } from './qr'

const worst = readFileSync(new URL('../../../telegram/tests/qr-worst-wg.conf', import.meta.url), 'utf8')
const short = worst.replace(/^AllowedIPs = .*$/m, 'AllowedIPs = 0.0.0.0/0, ::/0')

// Снято с qrSvg из wg-qrcode.svg.get.ts до переноса: ответ маршрута не меняется ни на байт.
const svgRef = {
  worst: { length: 58308, sha256: '5bc384be5de1d1226fba1c5fb08f253c72eea75f6106dfd8e7dbc9a5e5b7f953' },
  short: { length: 14974, sha256: '5be7ea24b7ddfd1aba7b9dd8df51a7d708f0ee478b13071c96ee3a57b50b8f58' },
}

// Формула _make_qr_png из telegram/bot.py: поле 4 модуля, Telegram ужимает фото больше 1280 px.
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
    const got = jsQR(new Uint8ClampedArray(png.data), png.width, png.height)
    expect(got?.data).toBe(conf)
  })
})
