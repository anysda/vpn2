import { expect, NODES, toast, test } from './support/fixtures'
import { openHome } from './support/clients'
import type { Page } from '@playwright/test'
import { rects } from './support/geometry'
import { noHorizontalOverflow } from './support/overflow'
import { orderBreaks } from './support/order'
import { wordBreaks } from './support/words'

test.describe('overview', () => {
  test.describe('Обзор', () => {
    test('рассылки на Обзоре нет: она под списком клиентов', async ({ page }) => {
      await openHome(page)
      await expect(page.locator('.ov-nodes')).toBeVisible()
      await expect(page.locator('.ov-broadcast')).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toHaveCount(0)
    })

    test('экзит выключается и включается из окна узла', async ({ page }) => {
      const exits = NODES.filter(t => t !== 'ru')
      test.skip(exits.length < 2, 'нужно два экзита')
      const tag = exits[0]!
      await openHome(page)
      const card = page.locator(`.ov-nodes .node[data-tag="${tag}"]`)
      await card.click()
      const dialog = page.getByRole('dialog')
      const toggle = dialog.getByRole('switch')
      await expect(toggle).toHaveAttribute('aria-checked', 'true')
      const node = (await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, active?: boolean, activeDevices?: number }>)
        .find(n => n.tag === tag)!
      await toggle.click()
      // через экзит идёт трафик — сначала вопрос
      if (node.active || node.activeDevices) {
        await expect(dialog.getByText(`Через ${tag.toUpperCase()} сейчас идёт трафик.`, { exact: false })).toBeVisible()
        await dialog.getByRole('button', { name: 'Выключить', exact: true }).click()
        await expect(dialog).toHaveCount(0)
        await card.click()
      }
      await expect(card.locator('.ft .tag')).toHaveText('ВЫКЛ')
      expect((await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, disabled: boolean }>)
        .find(n => n.tag === tag)?.disabled).toBe(true)

      await dialog.getByRole('switch').click()
      await expect(card.locator('.ft .tag')).not.toHaveText('ВЫКЛ')
      await expect(dialog.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    })
    test('пилюля периода переключает график на неделю и обратно', async ({ page }) => {
      await openHome(page)
      const wave = page.locator('.ov-wave')
      const pill = page.locator('.head .pill')
      await expect(wave.locator('h3')).toHaveText('Трафик за сутки')
      const day = page.waitForResponse(r => r.url().includes('/api/ops/traffic?period=week'))
      await pill.click()
      await day
      await expect(pill).toHaveText('Неделя')
      await expect(wave.locator('h3')).toHaveText('Трафик за неделю')
      await pill.click()
      await expect(pill).toHaveText('Сутки')
      await expect(wave.locator('h3')).toHaveText('Трафик за сутки')
    })

    test('линейка волны показывает трафик за час', async ({ page }) => {
      await openHome(page)
      const wave = page.locator('.ov-wave')
      await expect(wave.locator('.big')).not.toHaveText('—ГБ')
      const box = (await wave.locator('.plot').boundingBox())!
      await page.mouse.move(box.x + box.width * 0.5, box.y + box.height / 2)
      await expect(wave).toHaveClass(/\bhv\b/)
      await expect(wave.locator('.qtip')).toContainText(/ГБ за час\d\d:00–\d\d:00/)
    })

    test('активность: «Трафик» показывает ГБ за сутки, «Онлайн» — клиентов в сети', async ({ page }) => {
      await openHome(page)
      const act = page.locator('.ov-activity')
      await expect(act.locator('.agrid i').first()).toBeVisible()
      await expect(act.locator('.aread')).toContainText('в сети')
      await act.getByRole('button', { name: 'Трафик' }).click()
      await expect(act.getByRole('button', { name: 'Трафик' })).toHaveAttribute('aria-pressed', 'true')
      await expect(act.locator('.aread')).toContainText(/ГБ$/)
      // наведение переносит выбор на день
      const cell = act.locator('.agrid i').last()
      await cell.hover()
      await expect(cell).toHaveClass(/\bsel\b/)
      await act.getByRole('button', { name: 'Онлайн' }).click()
      await expect(act.locator('.aread')).toContainText('в сети')
    })
  })
})

test.describe('mobile-overview', { tag: '@mobile' }, () => {
  // Обзор на телефоне: блоки в порядке макета, ноды парами, шторка ноды вместо модалки.

  type Node = { tag: string, disabled: boolean, active?: boolean, activeDevices?: number | null }

  /** Подменить список нод: то, что отдаёт сервер, после правки. */
  async function nodesAs(page: Page, edit: (list: Node[]) => Node[]) {
    await page.route(/\/api\/ops\/nodes(\?.*)?$/, async (route) => {
      const response = await route.fetch()
      await route.fulfill({ response, json: edit(await response.json() as Node[]) })
    })
  }

  /** Дождаться конца входных анимаций и переходов (бесконечные пульсы не в счёт). */
  async function settled(page: Page) {
    await page.waitForFunction(() => document.getAnimations()
      .every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity))
  }

  // опрос нод может прийти в подмену, когда страница уже закрывается
  test.afterEach(({ page }) => page.unrouteAll({ behavior: 'ignoreErrors' }))

  const exits = NODES.filter(t => t !== 'ru')
  const node = (page: Page, tag: string) => page.locator(`.ov-nodes .node[data-tag="${tag}"]`)
  const sheet = (page: Page) => page.locator('.bsh.on')

  for (const width of [360, 402]) {
    for (const scheme of ['dark', 'light'] as const) {
      test(`${width}px, тема ${scheme}: блоки в порядке макета, без горизонтальной прокрутки`, async ({ page }) => {
        await page.setViewportSize({ width, height: 874 })
        await page.emulateMedia({ colorScheme: scheme })
        await openHome(page)
        for (const tag of NODES) await expect(node(page, tag)).toBeVisible()
        await settled(page)
        await noHorizontalOverflow(page, `Обзор ${width}`)

        // сверху вниз: события, ноды, выходы и DNS в одном ряду, трафик, активность, топ, клиенты
        const [ev, nodes, ex, dns, wave, act, top, cl] = await rects(page,
          '.ov .ev', '.ov-nodes', '.ov-exits', '.ov-dns', '.ov-wave', '.ov-activity', '.ov-top', '.ov-clients')
        expect(ev.y).toBeLessThan(nodes.y)
        expect(nodes.y).toBeLessThan(ex.y)
        expect(dns.y).toBe(ex.y)
        expect(dns.x).toBeGreaterThan(ex.x + ex.width)
        expect([wave.y, act.y, top.y, cl.y]).toEqual([...[wave.y, act.y, top.y, cl.y]].sort((a, b) => a - b))
        expect(ex.y).toBeLessThan(wave.y)

        // ноды парами, карточка 184
        const [a, b] = await rects(page, '.ov-nodes > .node:nth-child(1)', '.ov-nodes > .node:nth-child(2)')
        expect(b.y).toBe(a.y)
        expect(Math.round(a.height)).toBe(184)
        // клиенты онлайн и офлайн рядом
        const [m1, m2] = await rects(page, '.ov-clients > .mini:nth-child(1)', '.ov-clients > .mini:nth-child(2)')
        expect(m2.y).toBe(m1.y)
      })
    }
  }

  test('блоки Обзора в разметке идут сверху вниз, как на экране, без order', async ({ page }) => {
    await openHome(page)
    for (const tag of NODES) await expect(node(page, tag)).toBeVisible()
    await settled(page)
    const main = page.locator('main.tab.on')
    expect(await orderBreaks(main, 'blocks'), 'блоки по разметке против экрана').toEqual([])
    const ordered = await main.evaluate(el => [...el.querySelectorAll('*')]
      .filter(e => getComputedStyle(e).order !== '0').map(e => e.className))
    expect(ordered, 'перестановка через order').toEqual([])
  })

  test('нечётное число нод: entry на всю ширину, экзиты парами', async ({ page }) => {
    test.skip(exits.length < 3, 'нужно три экзита')
    await nodesAs(page, list => list.filter(n => n.tag !== exits[2]))
    await openHome(page)
    await expect(node(page, exits[2]!)).toHaveCount(0)
    await expect(node(page, 'ru')).toBeVisible()
    await settled(page)
    const [grid, ru, a, b] = await rects(page, '.ov-nodes', '.ov-nodes .node[data-tag="ru"]',
      `.ov-nodes .node[data-tag="${exits[0]}"]`, `.ov-nodes .node[data-tag="${exits[1]}"]`)
    expect(Math.round(ru.width)).toBe(Math.round(grid.width))
    expect(b.y).toBe(a.y)
    expect(a.y).toBeGreaterThan(ru.y)
  })

  test('тап по ноде — шторка с сеткой 3×3 и спарклайном, закрывается фоном и кнопкой', async ({ page }) => {
    await openHome(page)
    await node(page, 'ru').tap()
    const sh = page.getByRole('dialog', { name: 'Entry' })
    await expect(sh).toBeVisible()
    await expect(sheet(page)).toHaveCount(1)
    const cells = sh.locator('.mgrid > div')
    await expect(cells).toHaveCount(9)
    await settled(page)
    const cell = (n: number) => `[role="dialog"][aria-label="Entry"] .mgrid > div:nth-child(${n})`
    const [c1, c3, c4] = await rects(page, cell(1), cell(3), cell(4))
    expect(c3.y).toBe(c1.y)
    expect(c4.y).toBeGreaterThan(c1.y)
    expect(Math.round((await sh.locator('.msp').boundingBox())!.height)).toBe(70)
    await noHorizontalOverflow(page, 'шторка ноды')
    await page.locator('.bso.on').tap({ position: { x: 40, y: 40 } })
    await expect(sh).toBeHidden()

    await node(page, exits[0]!).tap()
    const ex = page.getByRole('dialog')
    await expect(ex.getByRole('switch')).toBeVisible()
    await ex.getByRole('button', { name: 'Закрыть', exact: true }).tap()
    await expect(ex).toHaveCount(0)
  })

  test('экзит с трафиком: «Выключить X?» в той же шторке, «Отмена» её закрывает', async ({ page }) => {
    const tag = exits[0]!
    await nodesAs(page, list => list.map(n => n.tag === tag ? { ...n, active: true, disabled: false } : n))
    await openHome(page)
    await node(page, tag).tap()
    const sh = sheet(page)
    await sh.getByRole('switch').tap()
    await expect(sh.getByRole('heading', { level: 2 })).toHaveText(/^Выключить .+\?$/)
    await expect(sh.getByText(`Через ${tag.toUpperCase()} сейчас идёт трафик.`, { exact: false })).toBeVisible()
    await expect(sh.locator('.mgrid')).toHaveCount(0)
    await sh.getByRole('button', { name: 'Отмена', exact: true }).tap()
    await expect(sheet(page)).toHaveCount(0)
  })

  test('последний включённый экзит не выключается — тост-ошибка', async ({ page }) => {
    const [keep] = exits
    await nodesAs(page, list => list.map(n => n.tag === 'ru' ? n : { ...n, disabled: n.tag !== keep }))
    await openHome(page)
    await node(page, keep!).tap()
    await sheet(page).getByRole('switch').tap()
    await expect(toast(page, 'Нельзя выключить последний включённый экзит')).toBeVisible()
    await expect(sheet(page).getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  })

  test('активность: 14 недель, тап по клетке выбирает день; топ — три клиента', async ({ page }) => {
    await openHome(page)
    const cells = page.locator('.ov-activity .agrid > i')
    await expect(cells.first()).toBeVisible()
    const xs = new Set(await cells.evaluateAll(els => els.map(e => Math.round(e.getBoundingClientRect().x))))
    expect(xs.size).toBe(14)
    const read = page.locator('.ov-activity .aread')
    const target = cells.nth(3)
    const day = await target.getAttribute('data-day')
    await target.tap()
    await expect(page.locator('.ov-activity .agrid > i.sel')).toHaveAttribute('data-day', day!)
    await expect(read).toBeVisible()
    expect(await page.locator('.ov-top .tr').count()).toBeLessThanOrEqual(3)
  })

  // Лента «События» на узком телефоне: слова заголовка и подробностей целиком, длинный домен,
  // адрес и логин без пробелов рвутся внутри себя, но не вылезают за ленту.
  test('лента событий 360: слова целиком, длинный домен и логин внутри ленты', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 874 })
    const at = Date.now()
    const long = 'e2e-feed-unbreakablelonglabelwithoutanydots.very-long-subdomain.example-domain.org'
    const events = [
      { id: 1, at, type: 'route_added', level: 'info', title: `Маршрут добавлен · ${long}`, detail: 'через Нидерланды · admin' },
      { id: 2, at, type: 'login_failed', level: 'alarm', title: 'Неудачный вход в панель', detail: 'логин «averyveryverylongloginwithoutanyspaces» · 2001:db8:85a3::8a2e:370:7334' },
      { id: 3, at, type: 'client_term', level: 'info', title: 'Ключи перевыпущены · Соединенные Штаты', detail: 'Безопасная навигация, Родительский контроль' },
    ]
    await page.route(/\/api\/events(\?.*)?$/, route => route.fulfill({ json: events }))
    await openHome(page)
    // первую ленту отдал сервер, подмена - со следующего опроса
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    const lines = page.locator('.ov .ev li>div>*')
    await expect(lines).toHaveCount(6)
    for (const el of await lines.all()) {
      const found = await el.evaluate(wordBreaks)
      const text = await el.textContent()
      // рваться может только кусок без пробелов с точкой, двоеточием или длиннее любого слова
      expect(found.filter(x => !x.startsWith('разрыв посреди слова') || /[а-яё]\|[а-яё]/i.test(x)), `«${text}»`).toEqual([])
    }
    expect(await page.locator('.ov .ev .tk').evaluateAll(l => l.map(e => e.textContent)), 'рвутся только не слова')
      .toEqual([long, '«averyveryverylongloginwithoutanyspaces»', '2001:db8:85a3::8a2e:370:7334'])
  })
})
