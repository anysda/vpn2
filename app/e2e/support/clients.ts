import type { Locator, Page } from '@playwright/test'
import { expect, hydrated, loginApi, toast } from './fixtures'

export async function openHome(page: Page, path = '/') {
  await loginApi(page)
  await page.goto(path)
  await hydrated(page)
}

/** Создать клиента формой «Новый клиент». */
export async function createClientUi(page: Page, name: string, opts: { expires?: string } = {}) {
  await page.getByRole('button', { name: 'Новый клиент' }).click()
  await page.getByPlaceholder('Иван Иванов').fill(name)
  if (opts.expires) await page.getByPlaceholder('ДД.ММ.ГГГГ').pressSequentially(opts.expires.replace(/\D/g, ''))
  await page.getByRole('button', { name: 'Создать' }).click()
  await expect(toast(page, `Клиент «${name}» создан`)).toBeVisible()
}

export function clientCard(page: Page, name: string) {
  return page.getByTestId('client-row').filter({ has: page.getByText(name, { exact: true }) })
}

/** Выбрать клиента в списке; вернуть локатор панели клиента справа. */
export async function openClient(page: Page, name: string) {
  await page.getByPlaceholder('Поиск...').fill(name)
  await clientCard(page, name).click()
  const panel = page.getByTestId('client-detail')
  await expect(clientName(panel)).toHaveText(name)
  return panel
}

/** Имя клиента в шапке панели: редактируется на месте, Enter сохраняет. */
export function clientName(panel: Locator) {
  return panel.getByTitle('Имя клиента')
}

/** Кнопка «+ девайс» в панели клиента. */
export function addDeviceButton(page: Page) {
  return page.getByRole('button', { name: '+ девайс', exact: true })
}

export async function addDevice(page: Page, name: string) {
  await addDeviceButton(page).click()
  const dlg = page.getByRole('dialog', { name: 'Новый девайс' })
  await dlg.getByPlaceholder('напр. iPhone').fill(name)
  // Выдача VPN-ключей и сертификатов может занять дольше UI-ожидания.
  // Сначала ждём ответ создания, затем проверяем тост и закрытие формы.
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST'
      && /^\/api\/clients\/\d+\/devices$/.test(new URL(r.url()).pathname)),
    dlg.getByRole('button', { name: 'Добавить' }).click(),
  ])
  expect(response.status()).toBe(200)
  await expect(toast(page, `Девайс «${name}» добавлен`)).toBeVisible()
  await expect(dlg).toBeHidden()
}

/** Строка девайса в панели клиента и её кнопки. */
export function deviceCard(page: Page, name: string) {
  const card = page.getByTestId('device-row').filter({ has: page.getByText(name, { exact: true }) })
  return {
    card,
    ikev2: card.getByRole('button', { name: 'IKEv2', exact: true }),
    wg: card.getByRole('button', { name: 'WG', exact: true }),
    ovpn: card.getByRole('button', { name: 'OVPN', exact: true }),
    reissue: card.getByRole('button', { name: 'Перевыпустить ключи девайса' }),
    remove: card.getByRole('button', { name: 'Удалить девайс' }),
  }
}

/** Окно конфига девайса: «WireGuard · phone», «OpenVPN · phone», «IKEv2 · phone». */
export function configDialog(page: Page, device: string, proto: 'WireGuard' | 'OpenVPN' | 'IKEv2') {
  return page.getByRole('dialog', { name: `${proto} · ${device}`, exact: true })
}

/** Значение строки окна IKEv2 по подписи: «Сервер», «Логин», «Пароль». */
export function ikeField(dlg: Locator, label: string) {
  return dlg.locator('.kvr').filter({ has: dlg.page().getByText(label, { exact: true }) }).locator('code')
}

/** Дата через год в ДД.ММ.ГГГГ. */
export function nextYear(): string {
  const d = new Date()
  d.setFullYear(d.getFullYear() + 1)
  return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`
}
