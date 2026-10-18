import type { Locator, Page } from '@playwright/test'
import { expect, test, toast, uniq } from './support/fixtures'
import { addDeviceButton, clientCard, clientName, configDialog, deviceCard, ikeField, nextYear, openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'

/** Поле срока сразу после открытия «Нового клиента»: автофокус окна не уводит набор в «Имя». */
async function typeExpiryRightAway(page: Page, tap: boolean) {
  await openHome(page, '/clients')
  const date = nextYear()
  const name = `Срок ${Date.now()}`
  const create = page.getByRole('button', { name: 'Новый клиент' })
  if (tap) await create.tap()
  else await create.click()
  const dlg = page.getByRole('dialog', { name: 'Новый клиент' })
  const expiry = dlg.getByPlaceholder('ДД.ММ.ГГГГ')
  await expiry.focus()
  await expiry.pressSequentially(date.replace(/\D/g, ''), { delay: 40 })
  await expect(expiry).toHaveValue(date)
  await expect(dlg.getByPlaceholder('Иван Иванов')).toHaveValue('')

  await dlg.getByPlaceholder('Иван Иванов').fill(name)
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
    dlg.getByRole('button', { name: 'Создать' }).click(),
  ])
  expect(response.status()).toBe(200)
  const [d, m, y] = date.split('.')
  expect(response.request().postDataJSON().expiresAt).toContain(`${y}-${m}-${d}`)
}

test.describe('десктоп', () => {
  // Файл в проекте mobile: окно — как у проекта desktop.
  test.use({ viewport: { width: 1400, height: 1000 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 })

  test('срок, набранный сразу после открытия окна, не теряет цифр', async ({ page }) => {
    await typeExpiryRightAway(page, false)
  })
})

/** Длинное имя: 64 знака, предел сервера; префикс e2e- снесёт teardown. */
function longName() {
  return `${uniq('m')} Константин Длиннофамильный-Ласточкин-Задунайский`.slice(0, 64)
}

/** Строка списка в одну линию: имя и мета обрезаются, правый столбец не съезжает. */
async function rowFits(row: Locator) {
  const m = await row.evaluate((el) => {
    const r = el.getBoundingClientRect()
    const who = el.querySelector('.who')!.getBoundingClientRect()
    const right = el.querySelector('.crt')!.getBoundingClientRect()
    const name = el.querySelector('.who b')!.getBoundingClientRect()
    return { h: r.height, right: r.right, vw: document.documentElement.clientWidth, gap: right.left - who.right, nameH: name.height }
  })
  expect(m.right, 'строка вылезла за экран').toBeLessThanOrEqual(m.vw)
  expect(m.gap, 'имя наехало на срок').toBeGreaterThanOrEqual(0)
  expect(m.nameH, 'имя переносится на вторую строку').toBeLessThan(24)
  expect(m.h, 'строка выросла').toBeLessThan(70)
}

/** Кнопки шторки целиком на экране, ничего не уходит вбок. */
async function sheetFits(page: Page, dlg: Locator, where: string) {
  await expect(dlg).toBeVisible()
  await noHorizontalOverflow(page, where)
  const vw = page.viewportSize()!.width
  for (const b of await dlg.locator('.mf .pill').all()) {
    const box = (await b.boundingBox())!
    expect(box.x, `${where}: кнопка за левым краем`).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width, `${where}: кнопка за правым краем`).toBeLessThanOrEqual(vw)
  }
}

async function closeSheet(dlg: Locator) {
  await dlg.getByRole('button', { name: 'Закрыть' }).tap()
  await expect(dlg).toBeHidden()
}

async function tapDownload(page: Page, dlg: Locator, name: string) {
  const [d] = await Promise.all([page.waitForEvent('download'), dlg.getByRole('button', { name, exact: true }).tap()])
  return d.suggestedFilename()
}

for (const width of [360, 402]) {
  test.describe(`телефон ${width}`, () => {
    test.use({ viewport: { width, height: 874 } })

    test('срок, набранный сразу после открытия шторки, не теряет цифр', async ({ page }) => {
      await typeExpiryRightAway(page, true)
    })

    for (const scheme of ['dark', 'light'] as const) {
      test(`тема ${scheme}: клиент от создания до удаления, девайс и все три конфига`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: scheme })
        await openHome(page, '/clients')
        await noHorizontalOverflow(page, 'список')

        const name = longName()
        await page.getByRole('button', { name: 'Новый клиент' }).tap()
        const create = page.getByRole('dialog', { name: 'Новый клиент' })
        await create.getByPlaceholder('Иван Иванов').fill(name)
        await sheetFits(page, create, 'шторка нового клиента')
        await create.getByRole('button', { name: 'Создать' }).tap()
        await expect(toast(page, `Клиент «${name}» создан`)).toBeVisible()
        await expect(create).toBeHidden()

        // Поиск и фильтры.
        const row = clientCard(page, name)
        await page.getByPlaceholder('Поиск...').fill(name)
        await expect(row).toHaveCount(1)
        await rowFits(row)
        const seg = page.locator('.clp .seg')
        await seg.getByRole('button', { name: /^Заморожены/ }).tap()
        await expect(row).toHaveCount(0)
        await seg.getByRole('button', { name: /^Все/ }).tap()
        await expect(row).toHaveCount(1)

        // Карточка клиента — отдельный экран вместо списка.
        await row.tap()
        const panel = page.getByTestId('client-detail')
        await expect(clientName(panel)).toHaveText(name)
        await expect(row).toBeHidden()
        await noHorizontalOverflow(page, 'карточка клиента')

        const expires = nextYear()
        const expiry = panel.getByLabel('Истекает')
        await expiry.fill(expires)
        await expiry.press('Tab')
        await expect(toast(page, 'Срок действия сохранён')).toBeVisible()
        await panel.getByRole('button', { name: 'Безлимитно' }).tap()
        await expect(panel.getByText('лимит снят (∞)')).toBeVisible()
        await panel.getByLabel('Предпочитаемый экзит').selectOption('fi')
        await expect(toast(page, 'Предпочитаемый экзит: FI')).toBeVisible()

        // Девайс и конфиги.
        const device = 'phone'
        await addDeviceButton(page).tap()
        const add = page.getByRole('dialog', { name: 'Новый девайс' })
        await add.getByPlaceholder('напр. iPhone').fill(device)
        await sheetFits(page, add, 'шторка нового девайса')
        await add.getByRole('button', { name: 'Добавить' }).tap()
        await expect(toast(page, `Девайс «${device}» добавлен`)).toBeVisible({ timeout: 30_000 })
        await expect(add).toBeHidden()
        const dc = deviceCard(page, device)
        await noHorizontalOverflow(page, 'карточка с девайсом')

        await dc.wg.tap()
        const wg = configDialog(page, device, 'WireGuard')
        const qr = wg.locator('img[alt="WireGuard QR"]')
        await expect(qr).toBeInViewport()
        await expect.poll(() => qr.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0)
        await sheetFits(page, wg, 'шторка WireGuard')
        await wg.getByRole('button', { name: 'Копировать', exact: true }).tap()
        await expect(toast(page, '.conf скопирован')).toBeVisible()
        expect(await tapDownload(page, wg, 'Скачать')).toMatch(/\.conf$/)
        await expect(wg.getByRole('button', { name: 'В TG', exact: true })).toBeInViewport()
        await closeSheet(wg)

        await dc.ovpn.tap()
        const ovpn = configDialog(page, device, 'OpenVPN')
        await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
        await sheetFits(page, ovpn, 'шторка OpenVPN')
        expect(await tapDownload(page, ovpn, 'Скачать')).toMatch(/\.ovpn$/)
        await closeSheet(ovpn)

        await dc.ikev2.tap()
        const ike = configDialog(page, device, 'IKEv2')
        await expect(ikeField(ike, 'Сервер')).not.toBeEmpty({ timeout: 30_000 })
        await sheetFits(page, ike, 'шторка IKEv2')
        await ike.locator('.mf').getByRole('button', { name: 'Копировать', exact: true }).tap()
        await expect(toast(page, 'IKEv2 креды скопированы')).toBeVisible()
        await closeSheet(ike)

        await dc.remove.tap()
        const delDev = page.getByRole('dialog', { name: 'Удалить девайс?' })
        await sheetFits(page, delDev, 'подтверждение удаления девайса')
        await delDev.getByRole('button', { name: 'Удалить' }).tap()
        await expect(toast(page, 'Отключение выполняется')).toBeVisible()
        await expect(dc.card).toHaveCount(0, { timeout: 45_000 })

        // Заморозка видна в списке после «Назад».
        await panel.getByRole('button', { name: 'Заморозить' }).tap()
        await expect(toast(page, 'Отключение выполняется')).toBeVisible()
        await expect(panel.getByRole('status')).toContainText('Доступ отключён', { timeout: 45_000 })
        await page.getByRole('button', { name: 'Назад' }).tap()
        await expect(panel).toBeHidden()
        await expect(row).toContainText('Доступ отключён')
        await rowFits(row)

        await row.tap()
        await panel.getByRole('button', { name: 'Удалить клиента' }).tap()
        const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
        await sheetFits(page, del, 'подтверждение удаления клиента')
        await del.getByRole('button', { name: 'Удалить' }).tap()
        await expect(toast(page, 'Отключение выполняется')).toBeVisible()
        await expect(panel).toBeHidden({ timeout: 45_000 })
        await expect(page.getByRole('button', { name: 'Назад' })).toHaveCount(0)
        await expect(row).toHaveCount(0)
        const list: { name: string }[] = await (await page.request.get('/api/clients')).json()
        expect(list.map(c => c.name)).not.toContain(name)
      })
    }

    test('длинный ник Telegram не ломает строку списка', async ({ page }) => {
      await openHome(page, '/clients')
      const name = longName()
      expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(200)
      // Ник ставит только бот при привязке; для вёрстки подменяем его в ответе списка.
      await page.route(u => u.pathname === '/api/clients', async (route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        const res = await route.fetch()
        const body = (await res.json()) as { name: string, tgUsername: string | null, tgLinked: boolean }[]
        for (const c of body) if (c.name === name) Object.assign(c, { tgUsername: 'a_very_long_telegram_username_32', tgLinked: true })
        await route.fulfill({ response: res, json: body })
      })
      await page.reload()
      await page.getByPlaceholder('Поиск...').fill(name)
      const row = clientCard(page, name)
      await expect(row).toContainText('@a_very_long_telegram_username_32')
      await rowFits(row)
      await noHorizontalOverflow(page, 'список с длинным ником')
    })
  })
}
