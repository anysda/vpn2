import type { Locator, Page } from '@playwright/test'
import { expect, hydrated, loginApi, toast, uniq, test } from './support/fixtures'
import { clientCard, clientName, openHome } from './support/clients'
import { waitAccess } from './support/access'
import { TAB_PAGES } from '../app/utils/tab-pages'

test.describe('mobile-history', { tag: '@mobile' }, () => {
  // Телефон: экраны поверх раздела (карточка клиента, нижние шторки) живут в
  // истории браузера. Системный «назад» (Android, жест iOS) закрывает верхний
  // слой, а не уводит со страницы; закрытие кнопкой не оставляет пустых шагов.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  const panel = (page: Page) => page.getByTestId('client-detail')
  const path = (page: Page) => { const u = new URL(page.url()); return u.pathname + u.search }

  async function newClient(page: Page) {
    await loginApi(page)
    const name = uniq('hist')
    const res = await page.request.post('/api/clients', { data: { name } })
    expect(res.status()).toBe(200)
    return { name, id: (await res.json() as { id: number }).id }
  }

  const step = (page: Page) => page.evaluate(() => Number(history.state?.position))

  /** Открыть шторку: она видна и легла в историю шагом сверху (успела, как успевает палец). */
  async function openSheet(page: Page, sheet: Locator, tap: () => Promise<void>) {
    const at = await step(page)
    await tap()
    await expect(sheet).toBeVisible()
    await expect.poll(() => step(page), { message: 'шторка — шаг истории' }).toBe(at + 1)
  }

  /** С Обзора в Клиенты таб-баром: у раздела есть куда уходить «назад». */
  async function clientsFromOverview(page: Page, name: string) {
    await openHome(page, '/')
    await page.locator('.mbar').getByRole('link', { name: 'Клиенты' }).tap()
    await expect(page).toHaveURL(/\/clients$/)
    await page.getByPlaceholder('Поиск...').fill(name)
    await expect(clientCard(page, name)).toHaveCount(1)
  }

  test('«назад» с карточки клиента возвращает к списку Клиентов', async ({ page }) => {
    const c = await newClient(page)
    await clientsFromOverview(page, c.name)
    await clientCard(page, c.name).tap()
    await expect(clientName(panel(page))).toHaveText(c.name)
    expect(path(page)).toBe(`/clients?id=${c.id}`)

    await page.goBack()
    expect(path(page)).toBe('/clients')
    await expect(clientCard(page, c.name)).toBeVisible()
    await expect(panel(page)).toBeHidden()
    await expect(page.getByRole('button', { name: 'Назад' })).toHaveCount(0)

    await page.goForward()
    await expect(clientName(panel(page))).toHaveText(c.name)
    await expect(clientCard(page, c.name)).toBeHidden()

    // Обновление страницы оставляет на карточке; «Назад» в шапке — к списку, без лишнего шага.
    await page.reload()
    await expect(clientName(panel(page))).toHaveText(c.name)
    await page.getByRole('button', { name: 'Назад' }).tap()
    await expect(clientCard(page, c.name)).toBeVisible()
    expect(path(page)).toBe('/clients')
    await page.goBack()
    await expect(page).toHaveURL(/\/$/)
  })

  test('прямая ссылка на клиента открывает карточку, «Назад» ведёт к списку', async ({ page }) => {
    const c = await newClient(page)
    await openHome(page, `/clients?id=${c.id}`)
    await expect(clientName(panel(page))).toHaveText(c.name)
    await page.getByRole('button', { name: 'Назад' }).tap()
    await expect(page.getByTestId('client-row').first()).toBeVisible()
    expect(path(page)).toBe('/clients')
  })

  test('шторка на карточке клиента: «назад» закрывает шторку, затем карточку', async ({ page }) => {
    const c = await newClient(page)
    await clientsFromOverview(page, c.name)
    await clientCard(page, c.name).tap()
    await expect(clientName(panel(page))).toHaveText(c.name)
    const add = page.getByRole('dialog', { name: 'Новый девайс' })
    const plus = () => page.getByRole('button', { name: '+ девайс', exact: true }).tap()
    await openSheet(page, add, plus)

    await page.goBack()
    await expect(add).toBeHidden()
    await expect(clientName(panel(page))).toHaveText(c.name)
    expect(path(page)).toBe(`/clients?id=${c.id}`)

    // Шторка, закрытая кнопкой, не оставляет в истории пустого шага.
    await openSheet(page, add, plus)
    await add.getByRole('button', { name: 'Отмена' }).tap()
    await expect(add).toBeHidden()
    await page.goBack()
    await expect(clientCard(page, c.name)).toBeVisible()
    expect(path(page)).toBe('/clients')
  })

  test('удаление клиента со шторки возвращает к списку, «назад» уходит с раздела', async ({ page }) => {
    const c = await newClient(page)
    await clientsFromOverview(page, c.name)
    await clientCard(page, c.name).tap()
    await panel(page).getByRole('button', { name: 'Удалить клиента' }).tap()
    const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
    const accepted = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().endsWith(`/api/clients/${c.id}`))
    await del.getByRole('button', { name: 'Удалить' }).tap()
    const response = await accepted
    expect(response.status()).toBe(202)
    const { operation } = await response.json()
    expect(operation.status).toBe('pending')
    await expect(toast(page, 'Отключение выполняется')).toBeVisible()
    await waitAccess(page.request, operation)
    await expect(del).toBeHidden()
    await expect(page).toHaveURL(/\/clients$/)
    await expect(page.getByPlaceholder('Поиск...')).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(/\/$/)
  })

  test('шторка ноды на Обзоре закрывается «назад», страница остаётся', async ({ page }) => {
    await openHome(page, '/')
    const sh = page.getByRole('dialog', { name: 'Entry' })
    await openSheet(page, sh, () => page.locator('.ov-nodes .node[data-tag="ru"]').tap())
    await page.goBack()
    await expect(sh).toBeHidden()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('.ov-nodes')).toBeVisible()
  })

  test('пункт меню профиля в шторке: переход без лишнего шага назад', async ({ page }) => {
    await openHome(page, '/')
    await page.getByRole('button', { name: /^Меню @/ }).tap()
    const menu = page.getByRole('dialog', { name: 'Меню профиля' })
    await expect(menu).toBeVisible()
    await menu.getByRole('menuitem', { name: 'Мой профиль' }).tap()
    await expect(page).toHaveURL(/\/settings$/)
    await page.goBack()
    await expect(page).toHaveURL(/\/$/)
    await expect(menu).toBeHidden()
    await page.goForward()
    await expect(page).toHaveURL(/\/settings$/)
  })

  test('«Выйти» в шторке меню ведёт на вход: закрытие шторки не перебивает переход', async ({ page }) => {
    // Гонка закрытия шторки и перехода ловилась не каждый раз: несколько выходов подряд.
    for (let i = 0; i < 5; i++) {
      await loginApi(page)
      await page.goto('/settings')
      await hydrated(page)
      await page.getByRole('button', { name: /^Меню @/ }).tap()
      await page.getByRole('menuitem', { name: 'Выйти' }).tap()
      await expect(page, `выход ${i + 1}`).toHaveURL(/\/login/)
      await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
    }
  })
})

test.describe('mobile-focus', { tag: '@mobile' }, () => {
  // Телефон: меню профиля и фокус под шапкой.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  /** Пункты меню профиля: подписи и адреса по порядку. */
  function menuItems(menu: ReturnType<Page['locator']>) {
    return menu.locator('[role="menuitem"]').evaluateAll(els => els.map(e => `${e.textContent?.trim()} ${e.getAttribute('href') ?? ''}`.trim()))
  }

  test('шторка профиля: те же пункты, что в меню на ПК, без «Настроить экран»; /screen - 404', async ({ page, guard }) => {
    guard.allow(404, /\/screen$/, 'GET')
    guard.allow(404, /\/api\/me\/layout$/, 'GET')
    await openHome(page)
    const desktop = await menuItems(page.locator('.wrap>header .pop.menu'))
    expect(desktop, 'меню профиля на ПК').toEqual(['Мой профиль /settings', 'Выйти'])
    await page.getByRole('button', { name: /^Меню @/ }).tap()
    const sheet = page.locator('.bsh.on')
    await expect(sheet).toBeVisible()
    expect(await menuItems(sheet), 'пункты шторки = пункты меню ПК').toEqual(desktop)
    expect((await page.goto('/screen'))?.status()).toBe(404)
    await expect(page.locator('.errp').getByText('404', { exact: true })).toBeVisible()
    expect((await page.request.get('/api/me/layout')).status()).toBe(404)
  })

  /** Где элемент в фокусе против шапки и таб-бара, после прокрутки к нему. */
  function focusState(page: Page) {
    return page.evaluate(() => new Promise<{ inMain: boolean, top: number, bottom: number, head: number, bar: number, y: number, field: boolean, name: string }>((r) => {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const a = document.activeElement as HTMLElement
        const box = a.getBoundingClientRect()
        r({
          inMain: !!a.closest('main.tab.on'),
          top: box.top,
          bottom: box.bottom,
          head: document.querySelector('.wrap>header')!.getBoundingClientRect().bottom,
          bar: document.querySelector('.mbar nav')!.getBoundingClientRect().top,
          y: scrollY,
          // многострочное поле браузер прокручивает к каретке, а не целиком
          field: a.tagName === 'TEXTAREA',
          name: `${a.tagName.toLowerCase()} «${(a.textContent || a.getAttribute('aria-label') || '').trim().slice(0, 24)}»`,
        })
      }))
    }))
  }

  test('Tab по разделам: элемент в фокусе целиком между шапкой и таб-баром', async ({ page }) => {
    // окно пониже, чтобы прокручивался каждый раздел
    await page.setViewportSize({ width: 402, height: 560 })
    await openHome(page)
    const hidden: string[] = [], scrolled: Record<string, boolean> = {}
    const walk = async (path: string) => {
      const main = page.locator('main.tab.on')
      await expect(main).toBeVisible()
      await main.evaluate(el => Promise.all(el.getAnimations({ subtree: true })
        .filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished)))
      await page.evaluate(() => {
        (document.activeElement as HTMLElement | null)?.blur()
        scrollTo(0, 0)
      })
      // вниз до конца раздела, потом обратно вверх: прокрутка к фокусу в обе стороны
      for (const key of ['Tab', 'Shift+Tab']) {
        let entered = false
        for (let i = 0; i < 200; i++) {
          await page.keyboard.press(key)
          const s = await focusState(page)
          if (s.y > 0) scrolled[path] = true
          if (!s.inMain) {
            if (entered) break
            continue
          }
          entered = true
          if (s.top < s.head - 0.5) hidden.push(`${path} ${key}: ${s.name} верх ${Math.round(s.top)} < низа шапки ${Math.round(s.head)}`)
          if ((s.field ? s.top + 40 : s.bottom) > s.bar + 0.5) hidden.push(`${path} ${key}: ${s.name} (высота ${Math.round(s.bottom - s.top)}) низ ${Math.round(s.bottom)} > верха таб-бара ${Math.round(s.bar)}`)
        }
      }
    }
    // Клиенты с 20+ строками, как на стенде
    const ids: number[] = []
    try {
      for (let i = 0; i < 22; i++) {
        const r = await page.request.post('/api/clients', { data: { name: uniq('tabfocus'), deviceLimit: null } })
        expect(r.status()).toBe(200)
        ids.push((await r.json() as { id: number }).id)
      }
      for (const { path, label } of TAB_PAGES) {
        await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
        await expect(page).toHaveURL(u => u.pathname === path)
        await walk(path)
      }
      expect(scrolled['/clients'], 'Клиенты прокручивались').toBe(true)
      expect(hidden, 'фокус под шапкой или таб-баром').toEqual([])
    }
    finally {
      for (const id of ids) await page.request.delete(`/api/clients/${id}`)
    }
  })
})

test.describe('mobile-inputs', { tag: '@mobile' }, () => {
  // Safari на iPhone увеличивает страницу при фокусе в поле со шрифтом меньше 16px.
  // Правило одно на всю панель: проверяются все поля каждого раздела, в том числе
  // подразделы DNS и окно «Новый клиент».

  const DNS_SUBS = ['Сводка', 'Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы', 'Клиенты', 'Настройки DNS']

  /** Поля страницы и их шрифт; скрытые тоже — у них тот же computed font-size. */
  function fields(page: Page) {
    return page.evaluate(() => [...document.querySelectorAll<HTMLElement>('input, select, textarea')].map(el => ({
      size: Number.parseFloat(getComputedStyle(el).fontSize),
      what: `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} «${el.getAttribute('aria-label') ?? el.getAttribute('placeholder') ?? el.getAttribute('name') ?? ''}»`,
    })))
  }

  async function allAtLeast16(page: Page, where: string) {
    const list = await fields(page)
    expect(list.length, `${where}: поля есть`).toBeGreaterThan(0)
    expect(list.filter(f => f.size < 16).map(f => `${f.what} ${f.size}px`), `${where}: поля мельче 16px`).toEqual([])
  }

  async function noZoomOnFocus(page: Page, where: string) {
    const field = page.locator('input:visible, textarea:visible, select:visible').first()
    await field.tap()
    expect(await page.evaluate(() => window.visualViewport?.scale ?? 1), `${where}: масштаб после фокуса`).toBe(1)
  }

  for (const width of [360, 402]) {
    test.describe(`поля на телефоне ${width}px`, () => {
      test.use({ viewport: { width, height: 874 }, isMobile: true, hasTouch: true })

      test('шрифт полей во всех разделах не меньше 16px, страница не увеличивается', async ({ page }) => {
        await openHome(page, '/clients')
        await allAtLeast16(page, '/clients')
        await noZoomOnFocus(page, '/clients')
        await page.getByRole('button', { name: 'Новый клиент' }).tap()
        await expect(page.getByRole('dialog', { name: 'Новый клиент' })).toBeVisible()
        await allAtLeast16(page, '/clients, Новый клиент')

        for (const path of ['/routes', '/settings']) {
          await openHome(page, path)
          await allAtLeast16(page, path)
          await noZoomOnFocus(page, path)
        }

        await openHome(page, '/dns')
        const seg = page.locator('.dns .dseg')
        for (const name of DNS_SUBS) {
          const tab = seg.getByRole('tab', { name, exact: true })
          await tab.tap()
          await expect(tab).toHaveAttribute('aria-selected', 'true')
        }
        await allAtLeast16(page, '/dns, все подразделы')
      })
    })
  }
})
