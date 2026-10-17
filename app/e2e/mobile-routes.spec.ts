import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test, toast } from './support/fixtures'
import { noHorizontalOverflow } from './support/overflow'

// Маршруты на телефоне: доска в одну колонку, перенос правила тапом и шторкой
// вместо перетаскивания — тем же PATCH, что drag&drop на десктопе.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

const stamp = () => `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`
const regions = new Intl.DisplayNames(['ru'], { type: 'region' })
const exitName = (tag: string) => regions.of(tag.toUpperCase())!
const exact = (text: string) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)
const PLACEHOLDER = 'netflix.com / *.openai.com / 8.8.8.8/32'

const chip = (page: Page, value: string) => page.locator('.rc').filter({ hasText: exact(value) })
const sheet = (page: Page) => page.getByRole('dialog', { name: 'Перенос правила' })

/** Ячейка «Напрямую» экзита по имени страны. */
function directCell(page: Page, country: string) {
  return page.locator('.ex').filter({ has: page.locator('.eh b', { hasText: exact(country) }) })
    .locator('.cell').filter({ has: page.locator('.cl span', { hasText: exact('Напрямую') }) })
}

async function openRoutes(page: Page) {
  await loginApi(page)
  await page.goto('/routes')
  await hydrated(page)
  await expect(page.locator('form').getByRole('combobox')).toContainText(exitName('ru'))
}

async function addRule(page: Page, value: string) {
  await page.getByPlaceholder(PLACEHOLDER).fill(value)
  await page.getByRole('button', { name: 'Добавить' }).tap()
  await expect(toast(page, 'Правило добавлено')).toBeVisible()
  await expect(chip(page, value)).toBeVisible()
}

test.describe('маршруты на телефоне', () => {
  test('тап по правилу переносит его на другой выход тем же PATCH, перенос переживает перезагрузку', async ({ page }) => {
    await openRoutes(page)
    const value = `e2e-${stamp()}.example.org`
    await addRule(page, value)
    await expect(directCell(page, exitName('ru')).filter({ has: chip(page, value) })).toHaveCount(1)

    await chip(page, value).tap()
    const dlg = sheet(page)
    await expect(dlg).toBeVisible()
    await expect(page.locator('.hint')).toContainText('Нажми на правило')
    await expect(dlg.getByRole('heading', { name: value })).toBeVisible()
    await expect(dlg.locator('[aria-pressed="true"] span').first()).toHaveText(exitName('ru'))

    const target = exitName('nl')
    const patched = page.waitForRequest(r => r.method() === 'PATCH' && /\/api\/routes\/\d+$/.test(r.url()))
    await dlg.getByRole('button').filter({ has: page.locator('span', { hasText: exact(target) }) }).tap()
    const req = await patched
    expect(req.postDataJSON()).toEqual({ outbound: 'hy2-nl-direct' })
    expect((await req.response())?.status()).toBe(200)
    await expect(dlg).toBeHidden()
    await expect(toast(page, `«${value}» → ${target}`)).toBeVisible()
    await expect(directCell(page, target).filter({ has: chip(page, value) })).toHaveCount(1)

    await page.reload()
    await hydrated(page)
    await expect(directCell(page, target).filter({ has: chip(page, value) })).toHaveCount(1)

    // Проверка адреса ведёт на выбранный экзит; цепочка в одну строку с переносом, «почему» под ней.
    // План роутера в e2e статичный, ответ проверки подставляем, как в routes.spec.
    const stored: { id: number, value: string }[] = await (await page.request.get('/api/routes')).json()
    const id = stored.find(r => r.value === value)!.id
    await page.route(/\/api\/routes\/check\?/, async (route) => {
      if (new URL(route.request().url()).searchParams.get('q') !== value) return route.continue()
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ query: value, ip: null, reason: 'manual', rule: value, routeId: id, outbound: 'hy2-nl-direct', delay: 41 }) })
    })
    await page.getByRole('textbox', { name: 'Проверить адрес' }).fill(value)
    const chain = page.getByTestId('route-check')
    await expect(chain.locator('.ch')).toHaveText([value, `правило ${value}`, `${target}41 мс`])
    await expect(chip(page, value)).toHaveClass(/\bhit\b/)
    const boxes = await chain.locator('.ch, .why').evaluateAll(els => els.map((e) => {
      const r = e.getBoundingClientRect()
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height) }
    }))
    const why = boxes.pop()!
    expect(boxes.map(b => b.h)).toEqual([28, 28, 28])
    expect(why.top).toBeGreaterThanOrEqual(Math.max(...boxes.map(b => b.bottom)))

    // Удаление — из той же шторки, тем же DELETE.
    await chip(page, value).tap()
    const deleted = page.waitForRequest(r => r.method() === 'DELETE' && /\/api\/routes\/\d+$/.test(r.url()))
    await dlg.getByRole('button', { name: 'Удалить' }).tap()
    expect((await (await deleted).response())?.status()).toBe(200)
    await expect(toast(page, `«${value}» удалено`)).toBeVisible()
    await expect(chip(page, value)).toHaveCount(0)
    await page.reload()
    await hydrated(page)
    await expect(page.locator('.ex').first()).toBeVisible()
    await expect(chip(page, value)).toHaveCount(0)
  })

  for (const width of [360, 402]) {
    for (const scheme of ['dark', 'light'] as const) {
      test(`${width}px, тема ${scheme}: экзиты списком карточек, ячейки рядом, без горизонтальной прокрутки`, async ({ page }) => {
        await page.setViewportSize({ width, height: 874 })
        await page.emulateMedia({ colorScheme: scheme })
        await openRoutes(page)
        await expect(page.locator('.ex')).toHaveCount(4)
        await noHorizontalOverflow(page, `/routes ${width}px`)

        const cards = await page.locator('.ex').evaluateAll(els => els.map((e) => {
          const r = e.getBoundingClientRect()
          return { x: Math.round(r.x), w: Math.round(r.width), cells: [...e.querySelectorAll('.cell')].map(c => Math.round(c.getBoundingClientRect().top)) }
        }))
        // Одна колонка на всю ширину контента (поля 16px).
        for (const c of cards) expect(c, 'карточка экзита').toMatchObject({ x: 16, w: width - 32 })
        // Россия — одна ячейка; у экзитов «Напрямую» и «WARP» в одном ряду.
        expect(cards[0]!.cells).toHaveLength(1)
        for (const c of cards.slice(1)) {
          expect(c.cells).toHaveLength(2)
          expect(c.cells[0]).toBe(c.cells[1])
        }
        for (const name of ['Проверить адрес', 'Новое правило']) {
          const h = await page.getByRole('textbox', { name }).locator('xpath=..').evaluate(e => Math.round(e.getBoundingClientRect().height))
          expect(h, name).toBe(44)
        }
      })
    }
  }
})
