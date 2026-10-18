import type { Page } from '@playwright/test'
import { expect, NODES, test, toast } from './support/fixtures'
import { openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'
import { orderBreaks } from './support/order'
import { wordBreaks } from './support/words'

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

async function box(page: Page, sel: string) {
  const b = await page.locator(sel).first().boundingBox()
  expect(b, sel).not.toBeNull()
  return b!
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
      const ev = await box(page, '.ov .ev'), nodes = await box(page, '.ov-nodes')
      const ex = await box(page, '.ov-exits'), dns = await box(page, '.ov-dns')
      const wave = await box(page, '.ov-wave'), act = await box(page, '.ov-activity')
      const top = await box(page, '.ov-top'), cl = await box(page, '.ov-clients')
      expect(ev.y).toBeLessThan(nodes.y)
      expect(nodes.y).toBeLessThan(ex.y)
      expect(dns.y).toBe(ex.y)
      expect(dns.x).toBeGreaterThan(ex.x + ex.width)
      expect([wave.y, act.y, top.y, cl.y]).toEqual([...[wave.y, act.y, top.y, cl.y]].sort((a, b) => a - b))
      expect(ex.y).toBeLessThan(wave.y)

      // ноды парами, карточка 184
      const cards = page.locator('.ov-nodes .node')
      const [a, b] = [(await cards.nth(0).boundingBox())!, (await cards.nth(1).boundingBox())!]
      expect(b.y).toBe(a.y)
      expect(Math.round(a.height)).toBe(184)
      // клиенты онлайн и офлайн рядом
      const minis = page.locator('.ov-clients .mini')
      const [m1, m2] = [await minis.nth(0).boundingBox(), await minis.nth(1).boundingBox()]
      expect(m2!.y).toBe(m1!.y)
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
  const grid = await box(page, '.ov-nodes'), ru = await box(page, '.ov-nodes .node[data-tag="ru"]')
  expect(Math.round(ru.width)).toBe(Math.round(grid.width))
  const a = await box(page, `.ov-nodes .node[data-tag="${exits[0]}"]`)
  const b = await box(page, `.ov-nodes .node[data-tag="${exits[1]}"]`)
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
  const [c1, c3, c4] = [await cells.nth(0).boundingBox(), await cells.nth(2).boundingBox(), await cells.nth(3).boundingBox()]
  expect(c3!.y).toBe(c1!.y)
  expect(c4!.y).toBeGreaterThan(c1!.y)
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
