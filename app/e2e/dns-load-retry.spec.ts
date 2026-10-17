import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'

// Подраздел DNS, чей первый GET упал, показывает ошибку и «Повторить загрузку»;
// по кнопке подраздел загружается без перезагрузки страницы.
const tab = (page: Page, name: string) => page.locator('.dns .dseg').getByRole('tab', { name })
const alert = (page: Page) => page.locator('.dns .dpan.on').getByRole('alert').filter({ hasText: 'AdGuard не ответил (e2e)' })

// $fetch сам повторяет упавший GET один раз, поэтому GET падает, пока тест не
// «починит» AdGuard перед нажатием «Повторить загрузку».
async function failGet(page: Page, url: RegExp) {
  await page.route(url, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({ status: 500, json: { message: 'AdGuard не ответил (e2e)' } })
  })
}

async function retry(page: Page, url: RegExp) {
  await page.unrouteAll({ behavior: 'wait' })
  const ok = page.waitForResponse(r => url.test(r.url()) && r.request().method() === 'GET' && r.status() === 200)
  await page.locator('.dns .dpan.on').getByRole('button', { name: 'Повторить загрузку' }).click()
  await ok
  await expect(alert(page)).toHaveCount(0)
}

test('свои правила: после ошибки загрузки «Повторить» открывает поле с текстом сервера', async ({ page, guard }) => {
  const url = /\/api\/dns\/rules$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  const text = (await (await page.request.get('/api/dns/rules')).json()).text as string
  await failGet(page, url)
  await tab(page, 'Свои правила').click()
  const area = page.getByRole('textbox', { name: 'Свои правила' })
  await expect(alert(page)).toBeVisible()
  await expect(area).not.toBeEditable()
  await expect(page.getByRole('button', { name: 'Применить' })).toBeDisabled()
  await retry(page, url)
  await expect(area).toHaveValue(text)
  await expect(area).toBeEditable()
  await expect(page.getByRole('button', { name: 'Применить' })).toBeEnabled()
})

test('настройки DNS: после ошибки загрузки «Повторить» показывает настройки', async ({ page, guard }) => {
  const url = /\/api\/dns\/settings$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  const ups = (await (await page.request.get('/api/dns/settings')).json()).upstreams as string
  await failGet(page, url)
  await tab(page, 'Настройки DNS').click()
  await expect(alert(page)).toBeVisible()
  await retry(page, url)
  await expect(page.getByRole('textbox', { name: 'Серверы выше' })).toHaveValue(ups)
  await expect(page.getByRole('switch', { name: 'Оптимистичный кэш' })).toBeEnabled()
})

test('фильтры: после ошибки загрузки «Повторить» показывает списки', async ({ page, guard }) => {
  const url = /\/api\/dns\/filters$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  await failGet(page, url)
  await tab(page, 'Фильтры').click()
  await expect(alert(page)).toBeVisible()
  await retry(page, url)
  await expect(page.getByRole('switch', { name: 'AdGuard DNS filter' })).toBeVisible()
})

test('перезаписи: после ошибки загрузки «Повторить» показывает список', async ({ page, guard }) => {
  const url = /\/api\/dns\/rewrites$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  await failGet(page, url)
  await tab(page, 'Перезаписи').click()
  await expect(alert(page)).toBeVisible()
  await retry(page, url)
})

test('сервисы: после ошибки загрузки «Повторить» показывает каталог', async ({ page, guard }) => {
  const url = /\/api\/dns\/services$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  const all = (await (await page.request.get('/api/dns/services')).json()).all as unknown[]
  await failGet(page, url)
  await tab(page, 'Сервисы').click()
  await expect(alert(page)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Разблокировать все' })).toBeDisabled()
  await retry(page, url)
  await expect(page.locator('.dns .svg2 [role="switch"]')).toHaveCount(all.length)
  await expect(page.getByRole('button', { name: 'Разблокировать все' })).toBeEnabled()
})

test('клиенты DNS: после ошибки загрузки «Повторить» показывает клиентов', async ({ page, guard }) => {
  const url = /\/api\/dns\/clients$/
  guard.allow(500, url, 'GET')
  await openHome(page, '/dns')
  await failGet(page, url)
  await tab(page, 'Клиенты').click()
  await expect(alert(page)).toBeVisible()
  await retry(page, url)
})
