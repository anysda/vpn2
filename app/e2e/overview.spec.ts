import { expect, NODES, toast, test } from './support/fixtures'
import { openHome } from './support/clients'
import type { Page } from '@playwright/test'
import { noHorizontalOverflow } from './support/overflow'
import { orderBreaks } from './support/order'
import { wordBreaks } from './support/words'

const node = (page: Page, tag: string) => page.getByTestId('ov-nodes').locator(`[data-tag="${tag}"]`)

test.describe('overview', () => {
  test.describe('Обзор', () => {
    test('рассылки на Обзоре нет: она под списком клиентов', async ({ page }) => {
      await openHome(page)
      await expect(page.getByTestId('ov-nodes')).toBeVisible()
      await expect(page.getByTestId('ov-broadcast')).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Рассылка от бота', exact: true })).toHaveCount(0)
    })

    test('экзит выключается и включается из окна узла', async ({ page }) => {
      const exits = NODES.filter(t => t !== 'ru')
      test.skip(exits.length < 2, 'нужно два экзита')
      const tag = exits[0]!
      await openHome(page)
      const card = node(page, tag)
      await card.click()
      const dialog = page.getByRole('dialog')
      const toggle = dialog.getByRole('switch')
      await expect(toggle).toHaveAttribute('aria-checked', 'true')
      const state = (await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, active?: boolean, activeDevices?: number }>)
        .find(n => n.tag === tag)!
      await toggle.click()
      // через экзит идёт трафик — сначала вопрос
      if (state.active || state.activeDevices) {
        await expect(dialog.getByText(`Через ${tag.toUpperCase()} сейчас идёт трафик.`, { exact: false })).toBeVisible()
        await dialog.getByRole('button', { name: 'Выключить', exact: true }).click()
        await expect(dialog).toHaveCount(0)
        await card.click()
      }
      await expect(card.locator('.tag')).toHaveText('ВЫКЛ')
      expect((await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, disabled: boolean }>)
        .find(n => n.tag === tag)?.disabled).toBe(true)

      await dialog.getByRole('switch').click()
      await expect(card.locator('.tag')).not.toHaveText('ВЫКЛ')
      await expect(dialog.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    })

    test('пилюля периода переключает график на неделю и обратно', async ({ page }) => {
      await openHome(page)
      const wave = page.getByTestId('ov-wave')
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

    test('подсказка графика трафика показывает трафик за час', async ({ page }) => {
      await openHome(page)
      const wave = page.getByTestId('ov-wave')
      await expect(wave.locator('.big')).not.toHaveText(/^—/)
      const box = (await wave.locator('svg').first().boundingBox())!
      await page.mouse.move(box.x + box.width * 0.5, box.y + box.height / 2)
      await expect(wave.getByTestId('chart-tip')).toContainText(/ГБ за час · \d\d:00–\d\d:00/)
    })

    test('активность: «Трафик» показывает ГБ за сутки, «Онлайн» — клиентов в сети', async ({ page }) => {
      await openHome(page)
      const act = page.getByTestId('ov-activity')
      const read = act.getByTestId('ov-read')
      await expect(act.getByTestId('ov-day').first()).toBeVisible()
      await expect(read).toContainText('в сети')
      await act.getByRole('tab', { name: 'Трафик' }).click()
      await expect(act.getByRole('tab', { name: 'Трафик' })).toHaveAttribute('aria-selected', 'true')
      await expect(read).toContainText(/ГБ$/)
      // наведение переносит выбор на день
      const cell = act.getByTestId('ov-day').last()
      await cell.hover()
      await expect(cell).toHaveAttribute('aria-current', 'true')
      await act.getByRole('tab', { name: 'Онлайн' }).click()
      await expect(read).toContainText('в сети')
    })
  })
})

test.describe('mobile-overview', { tag: '@mobile' }, () => {
  // Обзор на телефоне: все блоки в одну колонку, окно узла вместо карточки на месте.

  type Node = { tag: string, disabled: boolean, active?: boolean, activeDevices?: number | null }

  /** Подменить список нод: то, что отдаёт сервер, после правки. */
  async function nodesAs(page: Page, edit: (list: Node[]) => Node[]) {
    await page.route(/\/api\/ops\/nodes(\?.*)?$/, async (route) => {
      const response = await route.fetch()
      await route.fulfill({ response, json: edit(await response.json() as Node[]) })
    })
  }

  // опрос нод может прийти в подмену, когда страница уже закрывается
  test.afterEach(({ page }) => page.unrouteAll({ behavior: 'ignoreErrors' }))

  const exits = NODES.filter(t => t !== 'ru')
  const BLOCKS = ['ov-events', 'ov-nodes', 'ov-exits', 'ov-dns', 'ov-wave', 'ov-activity', 'ov-top', 'ov-clients']

  for (const width of [360, 402]) {
    for (const scheme of ['dark', 'light'] as const) {
      test(`${width}px, тема ${scheme}: все блоки на месте, без горизонтальной прокрутки`, async ({ page }) => {
        await page.setViewportSize({ width, height: 874 })
        await page.emulateMedia({ colorScheme: scheme })
        await openHome(page)
        for (const tag of NODES) await expect(node(page, tag)).toBeVisible()
        for (const id of BLOCKS) await expect(page.getByTestId(id), id).toBeVisible()
        await noHorizontalOverflow(page, `Обзор ${width}`)
      })
    }
  }

  test('блоки Обзора в разметке идут сверху вниз, как на экране, без order', async ({ page }) => {
    await openHome(page)
    for (const tag of NODES) await expect(node(page, tag)).toBeVisible()
    const main = page.locator('main.tab.on')
    expect(await orderBreaks(main, 'blocks'), 'блоки по разметке против экрана').toEqual([])
    const ordered = await main.evaluate(el => [...el.querySelectorAll('*')]
      .filter(e => getComputedStyle(e).order !== '0').map(e => e.className))
    expect(ordered, 'перестановка через order').toEqual([])
  })

  test('тап по ноде — окно с сеткой 3×3, закрывается кнопкой', async ({ page }) => {
    await openHome(page)
    await node(page, 'ru').tap()
    const entry = page.getByRole('dialog', { name: 'Entry' })
    await expect(entry).toBeVisible()
    await expect(entry.locator('.mgrid > div')).toHaveCount(9)
    await noHorizontalOverflow(page, 'окно ноды')
    await entry.getByRole('button', { name: 'Закрыть', exact: true }).tap()
    await expect(entry).toHaveCount(0)

    await node(page, exits[0]!).tap()
    const ex = page.getByRole('dialog')
    await expect(ex.getByRole('switch')).toBeVisible()
    await ex.getByRole('button', { name: 'Закрыть', exact: true }).tap()
    await expect(ex).toHaveCount(0)
  })

  test('экзит с трафиком: вопрос «Выключить X?», «Отмена» закрывает без выключения', async ({ page }) => {
    const tag = exits[0]!
    await nodesAs(page, list => list.map(n => n.tag === tag ? { ...n, active: true, disabled: false } : n))
    await openHome(page)
    await node(page, tag).tap()
    await page.getByRole('dialog').getByRole('switch').tap()
    const ask = page.getByRole('dialog', { name: /^Выключить .+\?$/ })
    await expect(ask.getByText(`Через ${tag.toUpperCase()} сейчас идёт трафик.`, { exact: false })).toBeVisible()
    await expect(page.locator('.mgrid')).toHaveCount(0)
    await ask.getByRole('button', { name: 'Отмена', exact: true }).tap()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(node(page, tag).locator('.tag')).not.toHaveText('ВЫКЛ')
  })

  test('последний включённый экзит не выключается — тост-ошибка', async ({ page }) => {
    const [keep] = exits
    await nodesAs(page, list => list.map(n => n.tag === 'ru' ? n : { ...n, disabled: n.tag !== keep }))
    await openHome(page)
    await node(page, keep!).tap()
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('switch').tap()
    await expect(toast(page, 'Нельзя выключить последний включённый экзит')).toBeVisible()
    await expect(dialog.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  })

  test('активность: 14 недель, тап по клетке выбирает день; топ — не больше трёх клиентов', async ({ page }) => {
    await openHome(page)
    const act = page.getByTestId('ov-activity')
    const cells = act.getByTestId('ov-day')
    await expect(cells.first()).toBeVisible()
    const xs = new Set(await cells.evaluateAll(els => els.map(e => Math.round(e.getBoundingClientRect().x))))
    expect(xs.size).toBe(14)
    const target = cells.nth(3)
    const day = await target.getAttribute('data-day')
    await target.tap()
    await expect(act.locator('[data-testid="ov-day"][aria-current="true"]')).toHaveAttribute('data-day', day!)
    await expect(act.getByTestId('ov-read')).toBeVisible()
    expect(await page.getByTestId('ov-top').getByRole('link').count()).toBeLessThanOrEqual(3)
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
    const lines = page.getByTestId('ov-events').locator('li>div>*')
    await expect(lines).toHaveCount(6)
    for (const el of await lines.all()) {
      const found = await el.evaluate(wordBreaks)
      const text = await el.textContent()
      // рваться может только кусок без пробелов с точкой, двоеточием или длиннее любого слова
      expect(found.filter(x => !x.startsWith('разрыв посреди слова') || /[а-яё]\|[а-яё]/i.test(x)), `«${text}»`).toEqual([])
    }
    await noHorizontalOverflow(page, 'лента событий 360')
  })
})
