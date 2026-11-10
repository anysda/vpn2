import { ADMIN_USER, expect, hydrated, loginApi, uniq, toast, test } from './support/fixtures'
import type { Page } from '@playwright/test'
import { openHome, clientCard, clientName, addDevice, configDialog, createClientUi, deviceCard, openClient } from './support/clients'
import { rects } from './support/geometry'
import { noHorizontalOverflow } from './support/overflow'
import { TAB_PAGES } from '../app/utils/tab-pages'
import { orderBreaks } from './support/order'
import { wordBreaks } from './support/words'
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

    test('боковые поля страницы как в макете: 40 px на 1440, 28 px на 1280', async ({ page }) => {
      await loginApi(page)
      for (const [width, side] of [[1440, 40], [1280, 28]] as const) {
        await page.setViewportSize({ width, height: 900 })
        for (const t of TABS) {
          await page.goto(t.path)
          await hydrated(page)
          const pad = await page.locator('.wrap').evaluate((el) => {
            const s = getComputedStyle(el)
            return [s.paddingLeft, s.paddingRight]
          })
          expect(pad, `${t.path} на ${width}`).toEqual([`${side}px`, `${side}px`])
        }
      }
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

test.describe('mobile-head', { tag: '@mobile' }, () => {
  // Заголовок раздела на телефоне переехал в шапку: .head, в котором остался
  // только он, не занимает места. Правило общее, не копия в каждом разделе.
  test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

  /** Верх первого видимого блока раздела и низ фиксированной шапки — в одном кадре. */
  function layout(page: Page) {
    return page.locator('main.tab.on').evaluate(async (main) => {
      // Раздел и его блоки въезжают анимацией: мерить после неё (бесконечные пульсы не ждать).
      await Promise.all(main.getAnimations({ subtree: true })
        .filter(a => a.effect?.getTiming().iterations !== Infinity)
        .map(a => a.finished))
      // Обёртка с display:contents своего блока не рисует: смотреть её детей.
      const blocks = (el: Element): Element[] => [...el.children]
        .flatMap(c => getComputedStyle(c).display === 'contents' ? blocks(c) : [c])
      const el = blocks(main).find(c => c.getBoundingClientRect().height > 0)
      return {
        top: el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : -1,
        head: document.querySelector('.wrap>header')!.getBoundingClientRect().bottom,
      }
    })
  }

  /** Верх первого видимого блока раздела. */
  const firstBlockTop = async (page: Page) => (await layout(page)).top

  test('первый блок каждого раздела на одной высоте под шапкой, пустой .head не держит отступ', async ({ page }) => {
    await openHome(page)
    const bar = page.locator('.mbar nav')
    const tops: Record<string, number> = {}, pads: Record<string, number> = {}
    for (const { path, label } of TAB_PAGES) {
      await bar.getByRole('link', { name: label }).tap()
      await expect(page).toHaveURL(u => u.pathname === path)
      await expect(page.locator('main.tab.on')).toBeVisible()
      tops[path] = await firstBlockTop(page)
      pads[path] = await page.locator('.wrap').evaluate(el => Math.round(Number.parseFloat(getComputedStyle(el).paddingTop)))

      // Шапка раздела только с заголовком, без своего правила раздела.
      const extra = await page.locator('main.tab.on').evaluate((main) => {
        const head = document.createElement('div')
        head.className = 'head'
        head.innerHTML = '<div><h1>Заголовок</h1><p>Подзаголовок</p></div>'
        main.prepend(head)
        return { height: head.getBoundingClientRect().height, display: getComputedStyle(head).display }
      })
      expect(extra, `${path}: .head только с заголовком`).toEqual({ height: 0, display: 'none' })
      expect(await firstBlockTop(page), `${path}: первый блок после пустой .head`).toBe(tops[path])
      await page.locator('main.tab.on > .head:first-child').evaluate(el => el.remove())
    }
    // Отступ контента под фиксированной шапкой задаёт .wrap, лишнего сверху нет.
    expect(tops, 'первый блок раздела').toEqual(Object.fromEntries(TAB_PAGES.map(t => [t.path, pads[t.path]])))
  })

  /** Низ фиксированной шапки. */
  const headBottom = (page: Page) => page.locator('.wrap>header').evaluate(el => el.getBoundingClientRect().bottom)

  test('первый блок на 4px ниже шапки на каждом разделе и при выросшей шапке тоже', async ({ page }) => {
    await openHome(page)
    const got: Record<string, number> = {}, want: Record<string, number> = {}
    const check = async (key: string) => {
      const { head, top } = await layout(page)
      want[key] = Math.round(head + 4)
      got[key] = top
    }
    for (const { path, label } of TAB_PAGES) {
      await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
      await expect(page).toHaveURL(u => u.pathname === path)
      await expect(page.locator('main.tab.on')).toBeVisible()
      await check(path)
    }
    // шапка любой высоты: отступ идёт за ней
    await page.locator('.wrap>header').evaluate((el) => { el.style.paddingBottom = '40px' })
    await expect.poll(() => headBottom(page)).toBeGreaterThan(90)
    // новая высота шапки доходит до отступа к следующему кадру
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
    await check('шапка выше')
    expect(got, 'верх первого блока = низ шапки + 4').toEqual(want)
  })
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

test.describe('mobile-tab-pill', { tag: '@mobile' }, () => {
  // Таб-бар телефона: пилюля активной вкладки облегает слово с равными отступами,
  // видимые промежутки между словами равны, бар целиком в ширине экрана.
  // Мерим геометрию элементов (Range по буквам слова, прямоугольники пилюли и вкладок),
  // не ширину шрифта: её headless Chromium округляет.

  /** Слово каждой вкладки (без пробелов вокруг), пилюля, вкладки и сам бар. */
  function geometry(nav: Element) {
    const word = (a: Element) => {
      const t = Array.from(a.childNodes).find(n => n.nodeType === Node.TEXT_NODE && n.textContent!.trim()) as Text
      const s = t.data.search(/\S/), e = t.data.trimEnd().length
      const r = document.createRange()
      r.setStart(t, s)
      r.setEnd(t, e)
      const q = r.getBoundingClientRect()
      return { left: q.left, right: q.right }
    }
    const links = Array.from(nav.querySelectorAll<HTMLElement>(':scope>a'))
    const g = nav.querySelector('.glide')!.getBoundingClientRect()
    const n = nav.getBoundingClientRect()
    return {
      words: links.map(word),
      links: links.map(a => ({ left: a.getBoundingClientRect().left, right: a.getBoundingClientRect().right, height: a.getBoundingClientRect().height })),
      on: links.findIndex(a => a.classList.contains('on')),
      pill: { left: g.left, right: g.right, width: g.width },
      nav: { left: n.left, right: n.right, scroll: nav.scrollWidth - nav.clientWidth },
      vw: document.documentElement.clientWidth,
    }
  }

  const round = (x: number) => Math.round(x * 10) / 10

  async function settled(page: Page) {
    const nav = page.locator('.mbar nav')
    // пилюля доехала: стоит ровно на активной вкладке
    await expect.poll(() => nav.evaluate((el) => {
      const g = el.querySelector('.glide')!.getBoundingClientRect(), a = el.querySelector(':scope>a.on')!.getBoundingClientRect()
      return Math.abs(g.left - a.left) < 0.5 && Math.abs(g.width - a.width) < 0.5
    })).toBe(true)
    return nav.evaluate(geometry)
  }

  for (const width of [360, 402]) {
    test.describe(`ширина ${width}`, () => {
      test.use({ viewport: { width, height: 800 } })

      test('пилюля по слову с равными отступами, промежутки между словами равны, бар в экране', async ({ page }) => {
        await openHome(page)
        const nav = page.locator('.mbar nav')
        await expect(nav).toBeVisible()

        for (const { path, label } of TAB_PAGES) {
          await nav.getByRole('link', { name: label }).tap()
          await expect(page).toHaveURL(u => u.pathname === path)
          const m = await settled(page)
          const w = m.words[m.on]!
          const left = w.left - m.pill.left, right = m.pill.right - w.right
          expect(Math.abs(left - right), `${label}: отступ слева ${round(left)} справа ${round(right)}`).toBeLessThanOrEqual(1)
        }

        const m = await nav.evaluate(geometry)
        const gaps = m.words.slice(1).map((w, i) => w.left - m.words[i]!.right)
        expect(Math.max(...gaps) - Math.min(...gaps), `промежутки ${gaps.map(round).join(', ')}`).toBeLessThanOrEqual(1)
        // отступ слова в пилюле у всех вкладок один (±1px): пилюля «по слову», а не по ячейке
        const pads = m.words.map((w, i) => w.left - m.links[i]!.left)
        expect(Math.max(...pads) - Math.min(...pads), `отступы в вкладках ${pads.map(round).join(', ')}`).toBeLessThanOrEqual(1)

        expect(m.nav.scroll, 'бар без прокрутки').toBeLessThanOrEqual(0)
        expect(m.nav.left, 'бар не за левым краем').toBeGreaterThanOrEqual(0)
        expect(m.nav.right, 'бар не за правым краем').toBeLessThanOrEqual(m.vw)
        for (const [i, l] of m.links.entries()) {
          expect(l.height, `${TAB_PAGES[i]!.label}: высота зоны нажатия`).toBeGreaterThanOrEqual(44)
          expect(m.words[i]!.left, `${TAB_PAGES[i]!.label}: слово не обрезано слева`).toBeGreaterThanOrEqual(l.left - 0.5)
          expect(m.words[i]!.right, `${TAB_PAGES[i]!.label}: слово не обрезано справа`).toBeLessThanOrEqual(l.right + 0.5)
        }
        expect(await nav.evaluate(wordBreaks)).toEqual([])
      })

      test('пилюля едет к новой вкладке и меняет ширину', async ({ page }) => {
        await openHome(page)
        const nav = page.locator('.mbar nav')
        const from = await settled(page)
        // Переход пилюли растянут до 10 с: на медленном CI кадр не обязан попасть в 0.32 с пути.
        // Свойства перехода (transform, width) и прыжок без перехода (.glide.now) - из стилей панели.
        await page.addStyleTag({ content: '.mbar .tabs .glide{transition-duration:10s!important}' })
        await nav.getByRole('link', { name: 'Настройки' }).tap()
        await expect.poll(() => nav.evaluate(el => el.querySelector('.glide')!.getAnimations()
          .map(a => (a as CSSTransition).transitionProperty).sort().join()), 'переход пилюли по положению и ширине').toBe('transform,width')
        // середина пути: пилюля между «Обзором» и «Настройками», ширина промежуточная
        const mid = await nav.evaluate((el) => {
          const g = el.querySelector('.glide')!
          for (const a of g.getAnimations()) {
            a.pause()
            a.currentTime = 5000
          }
          const r = g.getBoundingClientRect(), t = el.querySelector(':scope>a.on')!.getBoundingClientRect()
          for (const a of g.getAnimations()) a.finish()
          return { left: r.left, width: r.width, toLeft: t.left, toWidth: t.width }
        })
        const between = (x: number, a: number, b: number) => x > Math.min(a, b) + 1 && x < Math.max(a, b) - 1
        expect(between(mid.left, from.pill.left, mid.toLeft), `в пути: ${round(from.pill.left)} → ${round(mid.left)} → ${round(mid.toLeft)}`).toBe(true)
        expect(between(mid.width, from.pill.width, mid.toWidth), `ширина: ${round(from.pill.width)} → ${round(mid.width)} → ${round(mid.toWidth)}`).toBe(true)
        const to = await settled(page)
        expect(to.on).toBe(TAB_PAGES.findIndex(t => t.label === 'Настройки'))
        expect(to.pill.left, 'пилюля уехала вправо').toBeGreaterThan(from.pill.left)
        expect(Math.abs(to.pill.width - from.pill.width), 'ширина пилюли по слову').toBeGreaterThan(5)
      })
    })
  }
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
