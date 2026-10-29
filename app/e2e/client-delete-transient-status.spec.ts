import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

/**
 * Пока идёт удаление, список отдаёт удаляемого клиента промежуточным статусом
 * (frozen), и он выпадает из фильтра «Истекают». После удаления всё равно выбран
 * сосед из того, что видно слева, а удалённый на это время не остаётся открытым.
 */

interface Made { id: number, name: string }

const DAY = 24 * 60 * 60 * 1000
/** Срок в окне «Истекают». */
const soon = () => new Date(Date.now() + 3 * DAY).toISOString()

/** Ход удаления, как его видит страница: список и подтверждение операции. */
interface Deletion {
  /** `none` - DELETE ещё не ушёл, `frozen` - удаляемый в списке замороженным, `gone` - его нет. */
  phase: 'none' | 'frozen' | 'gone'
  /** Сколько ответов списка с замороженным удаляемым дошло до страницы. */
  frozenListed: number
  /** Строка удаляемого в списке до DELETE. */
  row?: { id: number, name: string, status: string }
  operation?: Promise<AccessOperation>
}

/**
 * Три своих клиента a, b, c со сроком в окне; список на странице сужен до них.
 * Удаление `target` подменено: после DELETE список отдаёт его замороженным, пока
 * тест не переведёт `phase` в `gone`, а подтверждение операции до того придержано.
 */
async function threeExpiring(page: Page, target: 'a' | 'b' | 'c') {
  const prefix = uniq('ts')
  const made: Made[] = []
  for (const suffix of ['a', 'b', 'c']) {
    const name = `${prefix}-${suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null, expiresAt: soon() } })
    expect(r.status(), await r.text()).toBe(200)
    made.push({ id: (await r.json()).id, name })
  }
  const victim = made[['a', 'b', 'c'].indexOf(target)]!
  const del: Deletion = { phase: 'none', frozenListed: 0 }
  const saved = await page.request.get(`/api/clients/${victim.id}`)
  expect(saved.status()).toBe(200)
  const detail = { ...await saved.json(), status: 'frozen' }
  await page.route(new RegExp(`/api/clients/${victim.id}$`), async (route) => {
    // В окне frozen клиент ещё есть в базе: карточка его отдаёт, а не 404.
    if (route.request().method() === 'GET' && del.phase === 'frozen') {
      const response = await route.fetch()
      if (response.status() !== 404) return route.fulfill({ response, json: { ...await response.json(), status: 'frozen' } })
      return route.fulfill({ status: 200, json: detail })
    }
    if (route.request().method() !== 'DELETE') return route.fallback()
    const response = await route.fetch()
    del.operation = response.json().then((body: { operation: AccessOperation }) => body.operation)
    del.phase = 'frozen'
    await route.fulfill({ response })
  })
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const phase = del.phase
    const response = await route.fetch()
    const list: { id: number, name: string, status: string }[] = await response.json()
    const own = list.filter(c => c.name.startsWith(prefix) && !(phase === 'gone' && c.id === victim.id))
    // До DELETE запомнить строку удаляемого; в окне frozen она в ответе, даже если
    // сервер уже успел его убрать (настоящее окно ~200 мс тест растягивает).
    const row = own.find(c => c.id === victim.id)
    if (phase === 'none' && row) del.row = row
    if (phase === 'frozen') {
      const rest = own.filter(c => c.id !== victim.id)
      own.splice(0, own.length, ...rest, { ...(row ?? del.row!), status: 'frozen' })
    }
    // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
    await route.fulfill({ response, json: own }).then(() => {
      if (phase === 'frozen' && own.some(c => c.id === victim.id)) del.frozenListed++
    }, (e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
    const response = await route.fetch()
    const state: AccessOperation = await response.json()
    // Пока список отдаёт удаляемого замороженным, удаление для страницы не закончено.
    const early = state.status === 'confirmed' && del.phase !== 'gone'
    await route.fulfill({ response, json: early ? { ...state, status: 'pending' } : state })
  })
  return { prefix, made, victim, del }
}

const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

type View = 'exp' | 'all' | 'search'

async function open(page: Page, view: View, prefix: string, made: Made[]) {
  await page.goto('/clients')
  if (view === 'exp') {
    await segment(page, 'Истекают').click()
    await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
  }
  if (view === 'search') await page.getByPlaceholder('Поиск...').fill(prefix)
  for (const c of made) await expect(clientCard(page, c.name)).toBeVisible()
}

async function select(page: Page, c: Made) {
  await clientCard(page, c.name).click()
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
}

/** Удалить выбранного и дождаться, пока страница получит список с ним замороженным. */
async function deleteToFrozen(page: Page, del: Deletion) {
  await page.getByTestId('client-detail').getByRole('button', { name: 'Удалить клиента' }).click()
  await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
  // Положительный контроль подмены: промежуточный статус дошёл до страницы.
  await expect.poll(() => del.frozenListed, { timeout: 15_000 }).toBeGreaterThan(0)
}

/** Удаление закончено: удалённого нет ни в списке, ни в операции. */
async function finish(page: Page, del: Deletion, victim: Made) {
  del.phase = 'gone'
  await waitAccess(page.request, await del.operation!)
  await expect(clientCard(page, victim.name)).toHaveCount(0)
}

async function expectShown(page: Page, c: Made) {
  await page.waitForLoadState('networkidle')
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  await expect(clientCard(page, c.name)).toBeVisible()
  await expect(clientCard(page, c.name)).toHaveAttribute('aria-pressed', 'true')
}

async function cleanup(page: Page, made: Made[]) {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
}

test('«Истекают», удаление среднего, в списке он сперва замороженный: выбран сосед снизу', async ({ page }) => {
  await loginApi(page)
  const { prefix, made, victim, del } = await threeExpiring(page, 'b')
  const [, b, c] = made as [Made, Made, Made]
  try {
    await open(page, 'exp', prefix, made)
    await select(page, b)
    await deleteToFrozen(page, del)
    await finish(page, del, victim)
    await expectShown(page, c)
  }
  finally { await cleanup(page, made) }
})

test('«Истекают», удаление среднего: пока он в списке замороженный, карточка справа его не показывает', async ({ page }) => {
  await loginApi(page)
  const { prefix, made, del } = await threeExpiring(page, 'b')
  const [, b] = made as [Made, Made, Made]
  try {
    await open(page, 'exp', prefix, made)
    await select(page, b)
    await deleteToFrozen(page, del)
    await expect(clientCard(page, b.name)).toHaveCount(0)
    // Не «когда-нибудь перестанет показывать», а ни разу за всё окно frozen:
    // проверка с повтором прошла бы, едва выбор уйдёт на кого угодно.
    await page.waitForTimeout(300)
    for (let i = 0; i < 10; i++) {
      expect(await clientName(page.getByTestId('client-detail')).allTextContents(), `${i * 200 + 300} мс после списка с замороженным`).not.toContain(b.name)
      await page.waitForTimeout(200)
    }
    expect(del.phase).toBe('frozen')
  }
  finally { await cleanup(page, made) }
})

test('«Истекают», удаление нижнего, в списке он сперва замороженный: выбран сосед сверху', async ({ page }) => {
  await loginApi(page)
  const { prefix, made, victim, del } = await threeExpiring(page, 'c')
  const [, b, c] = made as [Made, Made, Made]
  try {
    await open(page, 'exp', prefix, made)
    await select(page, c)
    await deleteToFrozen(page, del)
    await finish(page, del, victim)
    await expectShown(page, b)
  }
  finally { await cleanup(page, made) }
})

for (const view of ['all', 'search'] as const) {
  const what = view === 'all' ? '«Все»' : 'поиск'
  test(`${what}, удаление среднего, в списке он сперва замороженный: выбран сосед снизу`, async ({ page }) => {
    await loginApi(page)
    const { prefix, made, victim, del } = await threeExpiring(page, 'b')
    const [, b, c] = made as [Made, Made, Made]
    try {
      await open(page, view, prefix, made)
      await select(page, b)
      await deleteToFrozen(page, del)
      await finish(page, del, victim)
      await expectShown(page, c)
    }
    finally { await cleanup(page, made) }
  })
}
