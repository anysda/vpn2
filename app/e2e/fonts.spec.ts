import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test } from './support/fixtures'

// Каждое начертание Manrope объявлено один раз: @nuxt/fonts вставляет весь
// набор @font-face в каждый CSS, где семейство названо по имени, и при переходах
// по разделам копии копятся в document.fonts. Страницы берут var(--font-sans).

// Каждое объявление с именем Manrope @nuxt/fonts при сборке разрешает отдельным
// запросом к Google. Когда разные ответы несут разные версии файлов (Google
// выкатывает новую), их @font-face не схлопываются и набор объявлен дважды -
// так упал checks на dev 19-10. Имя в одном объявлении - один запрос на сборку.

// Начертания 200-600 из nuxt.config.ts и 700 от @nuxt/ui × 2 подмножества
const FACES = 6 * 2
const PAGES = ['/clients', '/routes', '/dns', '/settings', '/']

const root = new URL('../app', import.meta.url).pathname

function namedManrope() {
  const found: string[] = []
  for (const f of readdirSync(root, { recursive: true, encoding: 'utf8' })) {
    if (!/\.(css|vue)$/.test(f)) continue
    const src = readFileSync(join(root, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
    for (const m of src.matchAll(/(?:font-family|font|--font-[\w-]+)\s*:[^;{}]*\bManrope\b[^;{}]*/g)) found.push(`${f}: ${m[0]}`)
  }
  return found
}

test('Manrope назван по имени в одном объявлении CSS на всю панель', () => {
  const found = namedManrope()
  expect(found, found.join('\n')).toHaveLength(1)
})

async function manrope(page: Page) {
  await page.evaluate(() => document.fonts.ready)
  return page.evaluate(() => {
    const faces = [...document.fonts].filter(f => f.family.replace(/["']/g, '').startsWith('Manrope'))
    const keys = faces.map(f => [f.family.replace(/["']/g, ''), f.weight, f.style, f.unicodeRange].join(' | '))
    const src = [...document.styleSheets].flatMap(s => [...s.cssRules])
      .filter((r): r is CSSFontFaceRule => r instanceof CSSFontFaceRule && r.style.fontFamily.replace(/["']/g, '') === 'Manrope')
      .map(r => r.style.getPropertyValue('src'))
    return {
      keys,
      faces: keys.filter(k => k.startsWith('Manrope |')).length,
      dupes: keys.filter((k, i) => keys.indexOf(k) !== i),
      src,
    }
  })
}

test('Manrope: каждое начертание объявлено один раз и отдаётся панелью с /_fonts', async ({ page }) => {
  await loginApi(page)
  await page.goto('/')
  await hydrated(page)
  const first = await manrope(page)
  expect(first.dupes, 'повторы на Обзоре').toEqual([])
  expect(first.faces, first.keys.join('\n')).toBe(FACES)
  expect(first.src.length).toBe(FACES)
  for (const src of first.src) expect(src).toMatch(/url\("?[^)"]*\/_fonts\/[^)"]+\.woff2"?\)/)
  const url = /url\("?([^)"]+)"?\)/.exec(first.src[0]!)![1]!
  const font = await page.request.get(new URL(url, page.url()).href)
  expect(font.status()).toBe(200)
  expect(font.headers()['content-type']).toContain('font/woff2')

  // Переходы без перезагрузки: CSS разделов догружается и копился бы рядом.
  for (const path of PAGES) {
    await page.evaluate(p => (document.querySelector('#__nuxt') as Element & {
      __vue_app__: { config: { globalProperties: { $router: { push: (p: string) => Promise<unknown> } } } }
    }).__vue_app__.config.globalProperties.$router.push(p), path)
    await expect(page).toHaveURL(path)
    await hydrated(page)
  }
  const after = await manrope(page)
  expect(after.dupes, 'повторы после обхода разделов').toEqual([])
  expect(after.faces, after.keys.join('\n')).toBe(FACES)
})
