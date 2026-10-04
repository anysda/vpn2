import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, toast } from './fixtures'

export async function openHome(page: Page) {
  await loginApi(page)
  await page.goto('/')
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
  return page.locator('[role="button"]').filter({ has: page.getByText(name, { exact: true }) })
}

/** Открыть модалку клиента по карточке; вернуть локатор диалога. */
export async function openClient(page: Page, name: string) {
  await page.getByPlaceholder('Поиск...').fill(name)
  await clientCard(page, name).click()
  const dlg = page.getByRole('dialog').filter({ has: page.getByLabel('Имя клиента') })
  await expect(dlg.getByLabel('Имя клиента')).toHaveValue(name)
  return dlg
}

export async function addDevice(page: Page, name: string) {
  await page.getByRole('button', { name: 'девайс', exact: true }).click()
  const dlg = page.getByRole('dialog', { name: 'Новый девайс' })
  await dlg.getByPlaceholder('напр. iPhone').fill(name)
  await dlg.getByRole('button', { name: 'Добавить' }).click()
  await expect(toast(page, `Девайс «${name}» добавлен`)).toBeVisible()
  await expect(dlg).toBeHidden()
}

/** Карточка девайса внутри модалки клиента и её кнопки по порядку. */
export function deviceCard(page: Page, name: string) {
  const card = page.locator('div.rounded-md.border.px-3').filter({ has: page.getByText(name, { exact: true }) })
  const btns = card.locator('div.justify-end').getByRole('button')
  return {
    card,
    ikev2: btns.nth(0),
    wg: btns.nth(1),
    ovpn: btns.nth(2),
    reissue: btns.nth(3),
    remove: btns.nth(4),
  }
}

/** Дата через год в ДД.ММ.ГГГГ. */
export function nextYear(): string {
  const d = new Date()
  d.setFullYear(d.getFullYear() + 1)
  return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`
}
