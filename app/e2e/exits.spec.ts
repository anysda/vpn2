import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test } from './support/fixtures'
import { container, inside } from './support/openvpn-ca'
import { noHorizontalOverflow } from './support/overflow'

// Экзиты в настройках против подставного менеджера (support/fake-manager.py): сценарий задаёт ввод,
// файлы hold (держит ход) и deploy-busy («идёт ./deploy.sh») - его рычаги внутри контейнера панели.
const lever = (name: 'hold' | 'deploy-busy', on: boolean) => inside(`import fs from 'node:fs'
  const path = '/run/vpn2-manager/' + JSON.parse(process.argv[1])
  if (JSON.parse(process.argv[2])) fs.writeFileSync(path, ''); else fs.rmSync(path, { force: true })`, name, on)
const card = (page: Page) => page.locator('section.sexits')
const row = (page: Page, tag: string) => card(page).locator('.xrow').filter({ hasText: tag.toUpperCase() })
// Состояние экрана для карточки: 1440x900, при E2E_983_DIR.
const shot = (page: Page, state: string) => process.env.E2E_983_DIR
  ? page.screenshot({ path: `${process.env.E2E_983_DIR}/983-${state}.png` }) : Promise.resolve()

async function openSettings(page: Page) {
  await page.goto('/settings')
  await hydrated(page)
  await expect(card(page).getByRole('heading', { name: 'Экзиты' })).toBeVisible()
}

async function exitTags(page: Page) {
  const nodes: Array<{ tag: string }> = await (await page.request.get('/api/ops/nodes')).json()
  return nodes.map(n => n.tag)
}

/** Экран добавления до годной проверки. */
async function addForm(page: Page, host: string, tag: string) {
  await card(page).getByRole('button', { name: 'Добавить экзит' }).click()
  await card(page).getByLabel('Адрес').fill(host)
  await card(page).getByLabel('Тег').fill(tag)
  await card(page).getByRole('button', { name: 'Проверить' }).click()
}

test.describe.serial('экзиты в настройках', () => {
  test.use({ viewport: { width: 1440, height: 900 } })
  test.skip(!container, 'подставной менеджер есть только в одноразовом e2e-контейнере')
  test.beforeEach(async ({ page }) => {
    lever('hold', false)
    lever('deploy-busy', false)
    await loginApi(page)
  })

  test('список, ключ оркестратора, проверка с замечаниями и повторная', async ({ page }) => {
    await openSettings(page)
    await expect(card(page).locator('.xrow')).toHaveCount(3)
    await shot(page, 'idle')
    lever('hold', true)
    await addForm(page, 'root@192.0.2.66', 'de')
    await expect(card(page).getByTestId('orchestrator-key')).toContainText('ssh-ed25519 AAAA')
    await expect(card(page).getByRole('status').filter({ hasText: 'проверяю…' })).toBeVisible()
    await shot(page, 'checking')
    lever('hold', false)
    // Все замечания менеджера - его же текстами; установка закрыта.
    const alert = card(page).getByRole('alert').filter({ hasText: 'Сервер пока не годится' })
    await expect(alert).toContainText('192.0.2.66 не отвечает по ssh')
    await expect(alert).toContainText('нужна Ubuntu 26.04')
    await expect(card(page).getByRole('button', { name: 'Установить' })).toBeDisabled()
    await shot(page, 'problems')
    // Исправленный адрес - проверить заново; занятый тег отказывает менеджер, не панель.
    await card(page).getByLabel('Адрес').fill('192.0.2.9')
    await expect(card(page).getByText('данные изменились: проверьте снова')).toBeVisible()
    await card(page).getByLabel('Тег').fill('nl')
    await card(page).getByRole('button', { name: 'Проверить' }).click()
    await expect(card(page).getByText('тег nl уже занят')).toBeVisible()
    await card(page).getByLabel('Тег').fill('de')
    await card(page).getByRole('button', { name: 'Проверить' }).click()
    await expect(card(page).getByText('сервер годен')).toBeVisible()
    await expect(card(page).getByRole('button', { name: 'Установить' })).toBeEnabled()
  })

  test('добавление: ход по стадиям, второй и перезагруженный экран, обрыв связи, экзит везде', async ({ page, browser, guard }) => {
    await openSettings(page)
    await addForm(page, '192.0.2.9', 'de')
    await expect(card(page).getByText('сервер годен')).toBeVisible()
    lever('hold', true)
    await card(page).getByRole('button', { name: 'Установить' }).click()
    const job = card(page).locator('.xjob')
    await expect(job).toContainText('Добавление экзита DE')
    await expect(job.locator('li')).toHaveCount(9)
    await expect(job.getByText('идёт…')).toHaveCount(1)
    await shot(page, 'running')
    // Второй администратор и перезагрузка видят то же задание, добавлять и удалять нельзя - с причиной на виду.
    const other = await browser.newPage({ storageState: await page.context().storageState(), viewport: { width: 1440, height: 900 } })
    for (const view of [other, page]) {
      await (view === page ? page.reload() : openSettings(other))
      await hydrated(view)
      await expect(card(view).locator('.xjob')).toContainText('Добавление экзита DE')
      await expect(card(view).getByText('Идёт задание менеджера установки')).toBeVisible()
      await expect(card(view).getByRole('button', { name: 'Добавить экзит' })).toBeDisabled()
      await expect(row(view, 'nl').getByRole('button', { name: 'Удалить' })).toBeDisabled()
    }
    await shot(page, 'busy-job')
    await other.close()
    // Менеджер пропал на время (рестарт роутера): заметка вместо стены ошибок, опрос дальше.
    guard.allow(503, /\/api\/exits\/jobs\/\d+$/, 'GET')
    await page.route('**/api/exits/jobs/*', route => route.fulfill({ status: 503, json: { message: 'Нет связи с менеджером установки' } }))
    await expect(card(page).getByText('Нет связи с менеджером установки, повторяю…')).toBeVisible()
    await expect(card(page).getByRole('alert')).toHaveCount(0)
    await page.unroute('**/api/exits/jobs/*')
    lever('hold', false)
    await expect(job).toContainText('Экзит DE добавлен и работает.', { timeout: 60_000 })
    await expect(card(page).getByText('Нет связи с менеджером установки, повторяю…')).toHaveCount(0)
    await shot(page, 'done')
    // Состав на ходу, без перезапуска панели: настройки, обзор и мониторинг, выбор экзита клиента.
    await expect(row(page, 'de')).toBeVisible()
    await expect.poll(() => exitTags(page)).toContain('de')
    const outbounds: Array<{ name: string }> = await (await page.request.get('/api/routes/outbounds')).json()
    expect(outbounds.map(o => o.name)).toContain('hy2-de-direct')
    await page.goto('/')
    await expect(page.getByTestId('ov-nodes')).toContainText('DE')
  })

  test('удаление: закреплённый клиент уходит на авто; сервер очищен или забыт - своими словами', async ({ page }) => {
    const created = await page.request.post('/api/clients', { data: { name: `exit-pin-${Date.now()}` } })
    const client = await created.json()
    expect((await page.request.patch(`/api/clients/${client.id}`, { data: { preferredExit: 'de' } })).status()).toBe(200)
    await openSettings(page)
    for (const [tag, text, state] of [['de', 'Экзит DE удалён, сервер очищен', 'removed-cleaned'], ['us', 'Экзит US удалён и забыт: сервер не ответил', 'removed-forgotten']]) {
      await row(page, tag!).getByRole('button', { name: 'Удалить' }).click()
      const dialog = page.getByRole('dialog')
      await expect(dialog).toContainText('клиенты, закреплённые за ним, перейдут на авто')
      await dialog.getByRole('button', { name: 'Удалить' }).click()
      await expect(card(page).locator('.xjob')).toContainText(text!, { timeout: 60_000 })
      await shot(page, state!)
      await expect(row(page, tag!)).toHaveCount(0)
      await expect.poll(() => exitTags(page)).not.toContain(tag)
      await card(page).getByRole('button', { name: 'Скрыть' }).click()
    }
    // Ссылки на удалённый экзит в базе нет: закрепление снято, клиент работает через остальные.
    await expect.poll(async () => (await (await page.request.get(`/api/clients/${client.id}`)).json()).preferredExit, { timeout: 15_000 }).toBeNull()
    await page.request.delete(`/api/clients/${client.id}`)
    // Прежний состав стенда для следующих спеков.
    await addForm(page, '192.0.2.4', 'us')
    await card(page).getByRole('button', { name: 'Установить' }).click()
    await expect(card(page).locator('.xjob')).toContainText('Экзит US добавлен и работает.', { timeout: 60_000 })
    await card(page).getByRole('button', { name: 'Готово' }).click()
    await expect(row(page, 'us')).toBeVisible()
  })

  test('упавшее и оборванное задание: действия ровно из ответа менеджера', async ({ page }) => {
    await openSettings(page)
    await addForm(page, '192.0.2.10', 'fail1')
    await card(page).getByRole('button', { name: 'Установить' }).click()
    const job = card(page).locator('.xjob')
    await expect(job.getByRole('alert')).toContainText('ru: sing-box check: fail1', { timeout: 60_000 })
    await expect(job.getByRole('button')).toHaveText(['Повторить', 'Забыть'])
    await expect(job.locator('details')).not.toHaveAttribute('open')
    await shot(page, 'failed')
    await page.reload()
    await expect(card(page).getByText('Прошлое задание не закрыто: повторите его или забудьте')).toBeVisible()
    await card(page).locator('.xjob').getByRole('button', { name: 'Забыть' }).click()
    await expect(card(page).locator('.xjob')).toContainText('Задание закрыто.')
    await card(page).getByRole('button', { name: 'Скрыть' }).click()
    await expect(row(page, 'fail1')).toHaveCount(0)

    await addForm(page, '192.0.2.11', 'cut1')
    await card(page).getByRole('button', { name: 'Установить' }).click()
    await expect(job).toContainText('Задание оборвалось', { timeout: 60_000 })
    await expect(job.getByRole('button')).toHaveText(['Повторить', 'Забыть'])
    await shot(page, 'interrupted')
    await job.getByRole('button', { name: 'Повторить' }).click()
    await expect(job).toContainText('Экзит CUT1 добавлен и работает.', { timeout: 60_000 })
    await card(page).getByRole('button', { name: 'Готово' }).click()
    // Убрать добавленный: заданием через API, до конца.
    const removing = await (await page.request.delete('/api/exits/cut1')).json()
    await expect.poll(async () => (await (await page.request.get(`/api/exits/jobs/${removing.id}`)).json()).state, { timeout: 60_000 }).toBe('done')
  })

  test('идёт ./deploy.sh в терминале: добавлять и удалять нельзя, причина на виду', async ({ page }) => {
    lever('deploy-busy', true)
    await openSettings(page)
    await expect(card(page).getByText('Идёт ./deploy.sh в терминале')).toBeVisible()
    await expect(card(page).getByRole('button', { name: 'Добавить экзит' })).toBeDisabled()
    await expect(row(page, 'nl').getByRole('button', { name: 'Удалить' })).toBeDisabled()
    await shot(page, 'busy-deploy')
  })

  test('последний экзит: кнопка закрыта, причина видна без наведения', async ({ page }) => {
    await page.route('**/api/exits', async (route) => {
      const status = await (await route.fetch()).json()
      await route.fulfill({ json: { ...status, nodes: status.nodes.filter((n: { tag: string }) => n.tag === 'ru' || n.tag === 'nl') } })
    })
    await openSettings(page)
    await expect(card(page).locator('.xrow')).toHaveCount(1)
    await expect(row(page, 'nl').getByRole('button', { name: 'Удалить' })).toBeDisabled()
    await expect(card(page).getByRole('note')).toHaveText('Последний экзит удалить нельзя: без экзита VPN не работает')
    await shot(page, 'last-exit')
  })
})

test('экраны экзитов на телефоне не вылезают за ширину @mobile', async ({ page }) => {
  test.skip(!container, 'подставной менеджер есть только в одноразовом e2e-контейнере')
  await loginApi(page)
  await openSettings(page)
  await addForm(page, 'root@192.0.2.66', 'de')
  await expect(card(page).getByText('Сервер пока не годится')).toBeVisible()
  await noHorizontalOverflow(page, 'настройки: экзиты и добавление')
})
