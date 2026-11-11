import { ADMIN_USER, expect, hydrated, loginApi, uniq, toast, test } from './support/fixtures'
import type { Page } from '@playwright/test'
import { openHome, clientCard, clientName, addDevice, configDialog, createClientUi, deviceCard, openClient } from './support/clients'
import { rects } from './support/geometry'
import { noHorizontalOverflow } from './support/overflow'
import { TAB_PAGES } from '../app/utils/tab-pages'
import { orderBreaks } from './support/order'
import { checkBotQueue } from './support/bot-queue'
import { waitAccess } from './support/access'

test.describe('frame', () => {
  const TABS = [
    { path: '/', tab: 'Обзор', title: 'Обзор' },
    { path: '/clients', tab: 'Клиенты', title: 'Клиенты' },
    { path: '/routes', tab: 'Маршруты', title: 'Маршруты' },
    { path: '/dns', tab: 'DNS', title: 'DNS' },
    { path: '/settings', tab: 'Настройки', title: 'Настройки' },
  ]

  test.describe('разделы шапки', () => {
    test('каждый раздел открывается прямой ссылкой и отмечен в шапке', async ({ page }) => {
      await loginApi(page)
      for (const t of TABS) {
        await page.goto(t.path)
        await hydrated(page)
        await expect(page).toHaveTitle(new RegExp(`^${t.title} — `))
        const nav = page.getByRole('navigation')
        await expect(nav.getByRole('link', { name: t.tab, exact: true })).toHaveAttribute('aria-current', 'page')
        await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
      }
    })

    test('переход по вкладке и «назад» без перезагрузки', async ({ page }) => {
      await loginApi(page)
      await page.goto('/routes')
      await hydrated(page)
      await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
      await page.evaluate(() => Reflect.set(window, '__sameDocument', true))
      await page.getByRole('navigation').getByRole('link', { name: 'Клиенты', exact: true }).click()
      await expect(page).toHaveURL(/\/clients$/)
      await expect(page.getByRole('heading', { level: 1, name: 'Клиенты и ключи' })).toBeVisible()
      await page.goBack()
      await expect(page).toHaveURL(/\/routes$/)
      await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
      expect(await page.evaluate(() => Reflect.get(window, '__sameDocument'))).toBe(true)
    })

    test('переход поверх незаконченного перехода не роняет ошибку на страницу', async ({ page }) => {
      await loginApi(page)
      await page.goto('/routes')
      await hydrated(page)
      // Вторая вкладка нажата, пока первый переход ещё снимает старую страницу:
      // браузер отменяет первый переход, его отказ не должен всплыть на страницу.
      await page.evaluate(() => {
        const link = (name: string) => [...document.querySelectorAll<HTMLAnchorElement>('nav a')].find(a => a.textContent?.trim() === name)!
        new MutationObserver((_, observer) => {
          if (document.documentElement.dataset.vt !== 'page') return
          observer.disconnect()
          link('DNS').click()
        }).observe(document.documentElement, { attributeFilter: ['data-vt'] })
        link('Клиенты').click()
      })
      const nav = page.getByRole('navigation')
      await expect(page).toHaveURL(/\/dns$/)
      await expect(page).toHaveTitle(/^DNS — /)
      await expect(nav.locator('[aria-current="page"]')).toHaveText('DNS')
      await expect(page.locator('html[data-vt]')).toHaveCount(0)
    })
  })

  test('экран ошибки: код и текст макета без сообщения фреймворка', async ({ page, guard }) => {
    guard.allow(404, /\/no-such-page$/, 'GET')
    await loginApi(page)
    const response = await page.goto('/no-such-page')
    expect(response?.status()).toBe(404)
    await hydrated(page)
    const card = page.locator('.errp')
    await expect(card.getByRole('heading', { name: 'Ошибка' })).toBeVisible()
    await expect(card.getByText('404', { exact: true })).toBeVisible()
    await expect(card.getByText('Что-то пошло не так.', { exact: true })).toBeVisible()
    await expect(page.getByText(/Page not found/)).toHaveCount(0)
    await expect(card.getByRole('button', { name: 'Обновить' })).toBeVisible()
    await card.getByRole('button', { name: 'На главную' }).click()
    await expect(page).toHaveURL(/\/$/)
  })

  test.describe('тема', () => {
    test('выбор темы переживает перезагрузку и стоит до первой отрисовки', async ({ page }) => {
      await page.emulateMedia({ colorScheme: 'dark' })
      // Тема на <html> в момент разбора документа — до скриптов приложения.
      await page.addInitScript(() => {
        document.addEventListener('DOMContentLoaded', () => {
          Reflect.set(window, '__themeAtParse', document.documentElement.dataset.theme ?? null)
        })
      })
      await loginApi(page)
      await page.goto('/')
      await hydrated(page)
      const html = page.locator('html')
      await expect(html).toHaveAttribute('data-theme', 'dark')

      const choose = async (label: string) => {
        await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
        await page.getByRole('group', { name: 'Тема' }).getByRole('button', { name: label, exact: true }).click()
        await page.keyboard.press('Escape')
      }
      await choose('Светлая')
      await expect(html).toHaveAttribute('data-theme', 'light')
      await page.reload()
      await hydrated(page)
      expect(await page.evaluate(() => Reflect.get(window, '__themeAtParse'))).toBe('light')
      await expect(html).toHaveAttribute('data-theme', 'light')

      // «Как в системе» следует за сменой темы ОС без перезагрузки.
      await choose('Как в системе')
      await expect(html).toHaveAttribute('data-theme', 'dark')
      await page.emulateMedia({ colorScheme: 'light' })
      await expect(html).toHaveAttribute('data-theme', 'light')
    })
  })
})

test.describe('mobile', { tag: '@mobile' }, () => {
  /** Верх шапки и низ таб-бара в окне: после прокрутки они должны остаться там же. */
  async function chrome(page: Page) {
    const [head, bar] = await rects(page, '.wrap>header', '.mbar nav')
    return { head: head.y, bar: Math.round(bar.y + bar.height) }
  }

  /** Шторка открыта и закрывается тапом по оверлею над ней. */
  async function sheetClosesOnOverlay(page: Page, name: string) {
    const sheet = page.getByRole('dialog', { name })
    await expect(sheet).toBeVisible()
    await page.locator('.bso.on').tap({ position: { x: 40, y: 40 } })
    await expect(sheet).toBeHidden()
  }

  for (const scheme of ['dark', 'light'] as const) {
    test(`телефон, тема ${scheme}: разделы из таб-бара без горизонтальной прокрутки, шапка и таб-бар на месте`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme })
      await openHome(page)
      const bar = page.locator('.mbar nav')
      await expect(bar).toBeVisible()

      for (const { path, label } of TAB_PAGES) {
        await bar.getByRole('link', { name: label }).tap()
        await expect(page).toHaveURL(u => u.pathname === path)
        await expect(page.locator('.mhead h1')).not.toBeEmpty()
        await noHorizontalOverflow(page, path)

        const top = await chrome(page)
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
        await expect.poll(() => chrome(page), `${path}: шапка и таб-бар после прокрутки`).toEqual(top)
        await page.evaluate(() => window.scrollTo(0, 0))
      }

      await page.getByRole('button', { name: /^Меню @/ }).tap()
      await sheetClosesOnOverlay(page, 'Меню профиля')
      await page.getByRole('button', { name: /^Уведомления/ }).tap()
      await sheetClosesOnOverlay(page, 'Доставка уведомлений')
    })
  }
})

test.describe('mobile-header-order', { tag: '@mobile' }, () => {
  // Телефон: порядок шапки в разметке совпадает с экраном — Tab и скринридер
  // идут слева направо, «Назад» карточки клиента первым.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  /** Видимые элементы шапки в порядке разметки — слева направо и на экране. */
  async function headerInDomOrder(page: Page, where: string) {
    const xs = await page.locator('.wrap>header').evaluate(h => [...h.children]
      .map(el => ({ el: el.className || el.tagName, box: el.getBoundingClientRect() }))
      .filter(c => c.box.width > 0 && c.box.height > 0)
      .map(c => ({ el: c.el, x: Math.round(c.box.left) })))
    expect(xs.length, where).toBeGreaterThan(0)
    expect(xs, `${where}: разметка шапки не в порядке экрана`).toEqual([...xs].sort((a, b) => a.x - b.x))
  }

  test('карточка клиента: «Назад» первый в шапке и первый по Tab', async ({ page }) => {
    await loginApi(page)
    const name = uniq('hdr')
    expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(200)
    await openHome(page, '/clients')
    await headerInDomOrder(page, 'список')
    await page.getByPlaceholder('Поиск...').fill(name)
    await clientCard(page, name).tap()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(name)

    const back = page.getByRole('button', { name: 'Назад' })
    await expect(back).toBeVisible()
    await headerInDomOrder(page, 'карточка клиента')
    const first = await page.locator('.wrap>header>*').first().getAttribute('aria-label')
    expect(first).toBe('Назад')

    // Tab с самой шапки: первый фокус — первый элемент разметки.
    await page.locator('.wrap>header').evaluate((h: HTMLElement) => { h.tabIndex = -1; h.focus() })
    await page.keyboard.press('Tab')
    await expect(back).toBeFocused()
  })

  for (const url of ['/', '/routes', '/dns', '/settings']) {
    test(`${url}: шапка в порядке экрана`, async ({ page }) => {
      await openHome(page, url)
      await headerInDomOrder(page, url)
    })
  }
})

test.describe('mobile-order', { tag: '@mobile' }, () => {
  // Телефон: Tab и скринридер идут по разметке, поэтому порядок в разметке — тот же,
  // что на экране. Перестановка на экране (order, grid-row) без разметки — дефект.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  test('разделы таб-бара: фокусируемое по разметке идёт как на экране', async ({ page }) => {
    await openHome(page)
    const main = page.locator('main.tab.on')
    const breaks: Record<string, string[]> = {}
    for (const { path, label } of TAB_PAGES) {
      await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
      await expect(page).toHaveURL(u => u.pathname === path)
      await expect(main).toBeVisible()
      breaks[path] = await orderBreaks(main, 'focusable')
    }
    // вкладки DNS — свои экраны в одном разделе
    await page.locator('.mbar nav').getByRole('link', { name: 'DNS' }).tap()
    await expect(page).toHaveURL(u => u.pathname === '/dns')
    for (const tab of ['Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы']) {
      const t = main.locator('.dseg').getByRole('tab', { name: tab, exact: true })
      await t.tap()
      await expect(t).toHaveAttribute('aria-selected', 'true')
      breaks[`/dns ${tab}`] = await orderBreaks(main, 'focusable')
    }
    expect(breaks).toEqual(Object.fromEntries(Object.keys(breaks).map(k => [k, []])))
  })
})

test.describe('mobile-light', { tag: '@mobile' }, () => {
  // Верхнее свечение div.light на телефоне, как в мобильном макете: пятно 620×420
  // над верхним краем, шапка его не перекрывает, вбок страница не едет.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  function glow(page: Page) {
    return page.evaluate(() => {
      const light = document.querySelector('div.light')!
      const b = getComputedStyle(light, '::before')
      const header = document.querySelector('.wrap>header')
      return {
        display: getComputedStyle(light).display,
        spot: { top: b.top, width: b.width, height: b.height, image: b.backgroundImage.startsWith('radial-gradient') },
        streak: getComputedStyle(light, '::after').display,
        header: header ? getComputedStyle(header).backgroundImage.startsWith('radial-gradient') : null,
      }
    })
  }

  const spot = { top: '-220px', width: '620px', height: '420px', image: true }

  for (const scheme of ['dark', 'light'] as const) {
    test(`свечение на телефоне, тема ${scheme}: по макету, шапка не перекрывает, без прокрутки вбок`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme })
      for (const path of ['/', '/clients', '/routes', '/dns', '/settings']) {
        await openHome(page, path)
        expect(await glow(page), path).toEqual({ display: 'block', spot, streak: 'none', header: true })
        await noHorizontalOverflow(page, `${path}, ${scheme}`)
      }
      await page.context().clearCookies()
      await page.goto('/login')
      await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
      expect(await glow(page), '/login').toEqual({ display: 'block', spot, streak: 'none', header: null })
      await noHorizontalOverflow(page, `/login, ${scheme}`)
    })
  }
})

test.describe('narrow', { tag: '@narrow' }, () => {
  test.describe('окно 1024px', () => {
    test('очередь: короткий тост один раз за сессию вкладки без горизонтальной прокрутки', async ({ page }) => {
      await checkBotQueue(page, () => noHorizontalOverflow(page, 'очередь уведомлений'))
    })
    test('разделы, вход и модалки без горизонтальной прокрутки; модалки закрываются', async ({ page }) => {
      await page.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: Array.from({ length: 5 }, (_, i) => ({
        id: `phone-queue-${i}`, label: `Уведомление ${i + 1}`, status: 'queued', remaining: 5,
      })) }))
      const queueToast = toast(page, 'Бот недоступен, 5 уведомлений в очереди')
      await page.goto('/login')
      await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
      await noHorizontalOverflow(page, '/login')

      await openHome(page)
      await expect(queueToast).toBeVisible()
      // Дождаться карточек мониторинга — они и есть самые широкие.
      await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
      await expect(page.getByText('CPU').first()).toBeVisible()
      await noHorizontalOverflow(page, '/')

      await page.goto('/routes')
      await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
      await noHorizontalOverflow(page, '/routes')
      await page.goto('/dns')
      await expect(page.getByText('DNS').first()).toBeVisible()
      await noHorizontalOverflow(page, '/dns')

      await page.goto('/settings')
      await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
      await noHorizontalOverflow(page, '/settings')

      await page.goto('/clients')
      await hydrated(page)
      const client = uniq('phone')
      await createClientUi(page, client)
      const dlg = await openClient(page, client)
      await expect(queueToast, 'фоновая очередь не накрывает кнопки модалки').toBeHidden()
      await addDevice(page, 'e2e-phone')
      await noHorizontalOverflow(page, 'панель клиента')

      const dc = deviceCard(page, 'e2e-phone')
      for (const [btn, proto] of [[dc.wg, 'WireGuard'], [dc.ovpn, 'OpenVPN'], [dc.ikev2, 'IKEv2']] as const) {
        await btn.click()
        const m = configDialog(page, 'e2e-phone', proto)
        await expect(m).toBeVisible()
        await noHorizontalOverflow(page, `модалка ${proto}`)
        await m.getByRole('button', { name: /Close|Закрыть/ }).click()
        await expect(m).toBeHidden()
      }

      await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
      const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
      await noHorizontalOverflow(page, 'подтверждение удаления')
      // Принятие ещё не означает отзыв: ждём подтверждения всех VPN-служб.
      const [removed] = await Promise.all([
        page.waitForResponse(r => r.request().method() === 'DELETE'
          && /^\/api\/clients\/\d+$/.test(new URL(r.url()).pathname)),
        del.getByRole('button', { name: 'Удалить' }).click(),
      ])
      expect(removed.status()).toBe(202)
      const { operation } = await removed.json()
      expect(operation.status).toBe('pending')
      await waitAccess(page.request, operation)
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await expect(queueToast, 'переходы и закрытие модалок не повторяют тост очереди').toHaveCount(0)
    })
  })
})
