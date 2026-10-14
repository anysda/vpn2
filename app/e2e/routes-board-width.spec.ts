import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test } from './support/fixtures'

// Доска «Маршрутов» на подменённых данных: от одной до пяти колонок, худший
// случай по ширине - четырёхзначные задержки, длинные названия стран и правила
// без точек переноса. Ни при какой ширине окна от 1024 содержимое колонки
// экзита не выходит за её край: длинное обрезается многоточием, а не краем.

const WIDTHS = [1024, 1100, 1279, 1280, 1400, 1920]
// Россия всегда первая; дальше экзиты, у каждого прямой выход и WARP.
const EXITS = [
  { tag: 'cf', delay: 4999, warp: 1450 },
  { tag: 'us', delay: 145, warp: null },
  { tag: 'at', delay: 120, warp: 3210 },
  { tag: 'se', delay: null, warp: 999 },
]

function outbounds(columns: number) {
  const list = [{ name: 'direct-ru', type: 'direct', delay: null, isWarp: false, isDirectRu: true }]
  for (const e of EXITS.slice(0, columns - 1)) {
    list.push({ name: `hy2-${e.tag}-direct`, type: 'hysteria2', delay: e.delay, isWarp: false, isDirectRu: false })
    list.push({ name: `hy2-${e.tag}-warp`, type: 'hysteria2', delay: e.warp, isWarp: true, isDirectRu: false })
  }
  return list
}

function rules(columns: number) {
  let id = 1
  return outbounds(columns).flatMap(o => Array.from({ length: 6 }, (_, i) => ({
    id: id++,
    type: 'domain',
    value: i % 2
      ? `e2e-${o.name}-${i}-unbreakablelonglabelwithoutanydots.example.org`
      : `e2e-${i}.${o.name}.very-long-subdomain.example-domain.org`,
    outbound: o.name,
    createdAt: '2026-01-01T00:00:00.000Z',
  })))
}

/** Всё, что вылезло за колонку или срезано без многоточия. */
function overflows(page: Page) {
  return page.locator('.board').evaluate((board) => {
    const bad: string[] = []
    const name = (el: Element) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`
    for (const ex of board.querySelectorAll<HTMLElement>('.ex')) {
      const col = ex.querySelector('.eh b')?.textContent ?? '?'
      if (ex.scrollWidth > ex.clientWidth) bad.push(`${col}: .ex sw=${ex.scrollWidth} cw=${ex.clientWidth}`)
      const box = ex.getBoundingClientRect()
      for (const el of ex.querySelectorAll<HTMLElement>('*')) {
        const r = el.getBoundingClientRect()
        if (!r.width) continue
        if (r.right > box.right + 0.5 || r.left < box.left - 0.5) {
          bad.push(`${col}: ${name(el)} [${Math.round(r.left)}, ${Math.round(r.right)}] вне [${Math.round(box.left)}, ${Math.round(box.right)}]`)
        }
        if (el.scrollWidth <= el.clientWidth || el instanceof SVGElement) continue
        // Срезать можно только многоточием, а оно работает лишь в блочном
        // контейнере: у flex-контейнера текст - анонимный элемент, его режет край.
        const s = getComputedStyle(el)
        const ellipsis = s.textOverflow === 'ellipsis' && s.overflowX === 'hidden' && /^(block|inline-block)$/.test(s.display)
        if (!ellipsis) bad.push(`${col}: ${name(el)} sw=${el.scrollWidth} cw=${el.clientWidth} срезан без многоточия`)
      }
    }
    return bad
  })
}

test.describe('доска маршрутов: ширина колонок', () => {
  for (let columns = 1; columns <= 5; columns++) {
    test(`${columns} колонок: содержимое не шире колонки при окне ${WIDTHS.join('/')}`, async ({ page }) => {
      await page.route(/\/api\/routes\/outbounds(\?.*)?$/, route => route.fulfill({ json: outbounds(columns) }))
      await page.route(/\/api\/routes(\?.*)?$/, route => route.request().method() === 'GET'
        ? route.fulfill({ json: rules(columns) })
        : route.fallback())
      await loginApi(page)
      await page.goto('/routes')
      await hydrated(page)
      await expect(page.locator('.board .ex')).toHaveCount(columns)
      await expect(page.locator('.board .rc')).toHaveCount(columns === 1 ? 6 : 6 + (columns - 1) * 12)

      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 900 })
        await expect.poll(() => overflows(page), { message: `окно ${width}, колонок ${columns}` }).toEqual([])
      }
    })
  }
})
