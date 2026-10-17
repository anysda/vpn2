import type { Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'

// DNS на телефоне: восемь подразделов в одну колонку, окна — нижней шторкой.
// Длинные имена клиентов и адреса не должны вылезать за край экрана.

const SUBS = ['Сводка', 'Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы', 'Клиенты', 'Настройки DNS']

const CLIENTS = [
  { name: 'Дмитрий Козлов', tg: '@dkozlov', ips: ['10.66.66.2', 'fd42:42:42::2', '10.66.66.12'], filters: 'common', safesearch: 'global', services: null, queries: 12480 },
  { name: 'Анна Петрова-Водкина-Длинная-Фамилия', tg: '@anna_petrova_with_a_very_long_handle', ips: ['10.66.66.3'], filters: 'off', safesearch: 'on', services: 'TikTok, YouTube, Instagram', queries: 914 },
]

// Видимые элементы подраздела, которые торчат за край окна или срезаны краем
// своей карточки. Ряды со своей горизонтальной прокруткой не в счёт.
function stickingOut(page: Page) {
  return page.evaluate(() => {
    const w = document.documentElement.clientWidth
    const cut = (el: Element, r: DOMRect) => {
      for (let p = el.parentElement; p; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX
        if (o === 'auto' || o === 'scroll') return false
        if (o === 'hidden' || o === 'clip') {
          const b = p.getBoundingClientRect()
          if (r.right > b.right + 1 || r.left < b.left - 1) return true
        }
      }
      return r.right > w + 0.5 || r.left < -0.5
    }
    return [...document.querySelectorAll('.dns .dpan.on *')]
      .filter((el) => {
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.height > 0 && cut(el, r)
      })
      .slice(0, 5)
      .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} «${(el.textContent ?? '').trim().slice(0, 40)}»`)
  })
}

const sheet = (page: Page, name: string) => page.locator('.bsh[role="dialog"]').and(page.getByRole('dialog', { name }))

for (const width of [360, 402]) {
  test.describe(`DNS на телефоне ${width}`, () => {
    test.use({ viewport: { width, height: 874 }, isMobile: true, hasTouch: true })

    for (const scheme of ['dark', 'light'] as const) {
      test(`восемь подразделов в ширину экрана, ${scheme}`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: scheme })
        await page.route('**/api/dns/clients', route => route.fulfill({ json: CLIENTS }))
        await openHome(page, '/dns')
        const seg = page.locator('.dns .dseg')
        expect(await seg.evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true)

        for (const name of SUBS) {
          const tab = seg.getByRole('tab', { name, exact: true })
          await tab.tap()
          await expect(tab).toHaveAttribute('aria-selected', 'true')
          await expect(tab).toBeInViewport({ ratio: 1 })
          await expect(page.locator('.dns .dpan.on')).toBeVisible()
          await page.waitForLoadState('networkidle')
          // Снимок всей страницы перекладывает её по полной высоте: так видно
          // раскладку, которая держится только на первом проходе.
          await page.screenshot({ fullPage: true })
          await noHorizontalOverflow(page, name)
          expect(await stickingOut(page), name).toEqual([])
        }
      })
    }
  })
}

test.describe('DNS на телефоне: раскладка и шторки', () => {
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true })

  test('сводка 2×2, журнал строкой с деталями, сервисы в три колонки', async ({ page }) => {
    await openHome(page, '/dns')
    const kpi = page.locator('.dns .dk > .card')
    await expect(kpi).toHaveCount(4)
    // offset*, а не boundingBox: карточки въезжают с transform.
    const [a, b] = await kpi.evaluateAll(l => l.slice(0, 2).map(e => ({ top: (e as HTMLElement).offsetTop, w: (e as HTMLElement).offsetWidth })))
    expect(a!.top).toBe(b!.top)
    expect(a!.w).toBeLessThan(402 / 2)

    await page.locator('.dns .dseg').getByRole('tab', { name: 'Журнал' }).tap()
    const row = page.locator('.dns .lt.scroll > div').filter({ has: page.locator('time') }).first()
    await expect(row.locator('.lm')).toBeVisible()
    await expect(row.locator('time')).toBeHidden()

    await page.locator('.dns .dseg').getByRole('tab', { name: 'Сервисы' }).tap()
    const sv = page.locator('.dns .sv')
    await expect(sv.nth(2)).toBeVisible()
    const tops = await sv.evaluateAll(l => l.slice(0, 3).map(e => (e as HTMLElement).offsetTop))
    expect(new Set(tops).size).toBe(1)
  })

  test('правило и проверка, перезапись, список и сброс — в нижней шторке', async ({ page, guard }) => {
    guard.allow(409, /\/api\/dns\/rewrites$/, 'POST')
    await openHome(page, '/dns')
    const seg = page.locator('.dns .dseg')
    const before = (await (await page.request.get('/api/dns/rules')).json()).text as string
    try {
      await seg.getByRole('tab', { name: 'Свои правила' }).tap()
      await page.getByRole('textbox', { name: 'Свои правила' }).fill(`${before}\n||example.org^`)
      await page.getByRole('button', { name: 'Применить' }).tap()
      await expect(toast(page, 'Свои правила применены')).toBeVisible()
      await page.getByRole('textbox', { name: 'Домен для проверки' }).fill('example.org')
      await expect(page.locator('.dns .fres')).toContainText('Заблокирован')
      await expect(page.locator('.dns .fres code')).toHaveText('||example.org^')
    }
    finally {
      const r = await page.request.put('/api/dns/rules', { data: { text: before } })
      expect(r.status(), await r.text()).toBe(200)
    }

    await seg.getByRole('tab', { name: 'Перезаписи' }).tap()
    const domain = `${uniq('rw')}.lan`
    await page.getByRole('button', { name: 'Добавить перезапись' }).tap()
    const add = sheet(page, 'Добавить перезапись')
    await expect(add).toBeVisible()
    await add.getByRole('textbox', { name: 'Домен' }).fill(domain)
    await add.getByRole('textbox', { name: 'Ответ' }).fill('10.0.0.1')
    await add.getByRole('button', { name: 'Добавить', exact: true }).tap()
    await expect(toast(page, `Перезапись «${domain}» добавлена`)).toBeVisible()
    await expect(add).toBeHidden()
    const rw = page.locator('.dns .rwt > div').filter({ hasText: domain })
    await expect(rw).toContainText('10.0.0.1')
    await rw.getByRole('button', { name: 'Удалить' }).tap()
    const del = sheet(page, 'Удалить перезапись?')
    await expect(del).toBeVisible()
    await del.getByRole('button', { name: 'Удалить', exact: true }).tap()
    await expect(toast(page, `Перезапись «${domain}» удалена`)).toBeVisible()
    await expect(rw).toHaveCount(0)

    await seg.getByRole('tab', { name: 'Фильтры' }).tap()
    await page.getByRole('button', { name: 'Добавить список' }).first().tap()
    const list = sheet(page, 'Добавить список')
    await expect(list.getByRole('textbox', { name: 'Адрес списка' })).toBeVisible()
    await page.locator('.bso.on').tap({ position: { x: 20, y: 20 } })
    await expect(list).toBeHidden()

    await seg.getByRole('tab', { name: 'Настройки DNS' }).tap()
    await page.getByRole('button', { name: 'Журнал', exact: true }).tap()
    const ask = sheet(page, 'Сбросить журнал?')
    await expect(ask).toBeVisible()
    await ask.getByRole('button', { name: 'Отмена' }).tap()
    await expect(ask).toBeHidden()
  })
})
