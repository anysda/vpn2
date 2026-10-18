import { expect, test } from '@playwright/test'

// Ширины букв Manrope 500 14px по метрике шрифта (advance из hmtx при wght 500), как на телефоне
// и в webkit. Chromium на Linux с хинтингом округляет их до целых пикселей («т» 9 вместо 7.21),
// и переносы строк в e2e расходятся с телефоном. Флаг запуска - launchOptions в playwright.config.ts.
const METRIC: [string, number][] = [['С', 10.12], ['к', 7.14], ['р', 8.4], ['ы', 9.7], ['т', 7.21], ['о', 8.41]]

test('буквы Manrope 500 14px шириной по метрике шрифта, не хинтованные целые пиксели', async ({ page }) => {
  await page.goto('/login')
  const widths = await page.evaluate(async (letters) => {
    await document.fonts.load('500 14px Manrope')
    if (!document.fonts.check('500 14px Manrope')) return null
    const box = document.createElement('div')
    box.style.cssText = 'position:absolute;left:0;top:0;font:500 14px Manrope;font-kerning:none;white-space:pre'
    document.body.append(box)
    const out = letters.map((ch) => {
      const span = document.createElement('span')
      span.textContent = ch
      box.append(span)
      return [ch, Math.round(span.getBoundingClientRect().width * 100) / 100] as [string, number]
    })
    box.remove()
    return out
  }, METRIC.map(([ch]) => ch))
  expect(widths, 'Manrope загружен').not.toBeNull()
  const off = widths!.filter(([ch, w]) => Math.abs(w - METRIC.find(([c]) => c === ch)![1]) > 0.2)
  expect(off, `ширины ${JSON.stringify(widths)}, метрика ${JSON.stringify(METRIC)}`).toEqual([])
})
