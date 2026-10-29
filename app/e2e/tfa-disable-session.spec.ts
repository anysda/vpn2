import type { APIRequestContext, Page, Route } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, test, toast } from './support/fixtures'
import { totp } from './support/totp'

/**
 * VPN2-575: выключение 2FA пересоздаёт сессию — библиотека ставит новую cookie
 * и сразу удаляет старую сессию. Опрос страницы, ушедший со старой cookie до
 * ответа disable, доходит до сервера уже после удаления и получает 401, хотя
 * человек остаётся на той же вкладке. Окно держим сами: ответ disable браузер
 * получает после опроса статуса бота, опрос идёт до или после выключения.
 */

interface Totp { secret: string, usedStep: number }

/** Код шага, которого панель ещё не видела (VPN2-62 гасит принятый шаг). */
async function freshCode(state: Totp) {
  const now = Math.floor(Date.now() / 30_000)
  const step = Math.max(now, state.usedStep + 1)
  if (step > now + 1) await new Promise(resolve => setTimeout(resolve, (step - 1) * 30_000 - Date.now() + 500))
  state.usedStep = step
  return totp(state.secret, step * 30_000)
}

/** Уборка 2FA своим API-контекстом, если тест не дошёл до выключения. */
const tfaTest = test.extend<{ tfa: Totp }>({
  tfa: async ({ playwright, baseURL }, use) => {
    const state: Totp = { secret: '', usedStep: 0 }
    await use(state)
    if (!state.secret) return
    const api = await playwright.request.newContext({ baseURL, extraHTTPHeaders: { origin: new URL(baseURL!).origin } })
    try {
      const login = await authPost(api, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
      expect(login.status(), await login.text()).toBe(200)
      if (!(await login.json()).twoFactorRedirect) return
      const verify = await authPost(api, '/two-factor/verify-totp', { code: await freshCode(state) })
      expect(verify.status(), await verify.text()).toBe(200)
      const disable = await authPost(api, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await freshCode(state) })
      expect(disable.status(), await disable.text()).toBe(200)
    }
    finally {
      await api.dispose()
    }
  },
})

/**
 * Включить 2FA через API, открыть настройки и выключить её модалкой. Пока
 * disable в пути, страница опрашивает статус бота со старой cookie; опрос
 * доходит до сервера до (`before`) или после (`after`) выключения.
 * Возвращает статус этого опроса, 0 — опроса не было.
 */
async function disableWhilePolling(page: Page, newApi: () => Promise<APIRequestContext>, tfa: Totp, pollLands: 'before' | 'after') {
  // Опросы по таймеру выключены: единственный опрос в окне — наш.
  await page.addInitScript(() => { window.setInterval = (() => 0) as unknown as typeof window.setInterval })
  await loginApi(page)
  const setup = await authPost(page.request, '/two-factor/enable', { password: ADMIN_PASSWORD })
  expect(setup.status(), await setup.text()).toBe(200)
  tfa.secret = new URL((await setup.json()).totpURI).searchParams.get('secret')!
  const enable = await authPost(page.request, '/two-factor/verify-totp', { code: await freshCode(tfa) })
  expect(enable.status(), await enable.text()).toBe(200)

  let pollStatus = 0
  // Человек вернулся на вкладку, пока выключение в пути: панель обновляет
  // статус бота с той cookie, что сейчас у браузера.
  const poll = async () => {
    const answered = page.waitForResponse(r => /\/api\/admin\/telegram$/.test(r.url()) && r.request().method() === 'GET', { timeout: 5_000 })
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    // Панель вправе не слать опрос, пока выключение в пути: тогда статус 0.
    pollStatus = await answered.then(r => r.status(), () => 0)
  }
  await page.route(/\/api\/auth\/two-factor\/disable$/, async (route: Route) => {
    // Запрос уходит на сервер своим контекстом со cookie браузера, а ответ с
    // новой cookie браузер получит только в fulfill: так держится окно, когда
    // сервер уже удалил старую сессию, а вкладка ещё шлёт её.
    const cookie = (await page.context().cookies(route.request().url())).map(c => `${c.name}=${c.value}`).join('; ')
    const api = await newApi()
    try {
      if (pollLands === 'before') await poll()
      const response = await api.fetch(route.request(), { headers: { ...route.request().headers(), cookie } })
      if (pollLands === 'after') await poll()
      await route.fulfill({ response })
    }
    finally {
      await api.dispose()
    }
  })

  await page.goto('/settings')
  await hydrated(page)
  await expect(page.getByText('Включена', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Выключить 2FA' }).click()
  const dlg = page.getByRole('dialog', { name: 'Выключить 2FA?' })
  await dlg.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
  await dlg.getByLabel('Код 2FA (6 цифр)').fill(await freshCode(tfa))
  await dlg.getByRole('button', { name: 'Выключить' }).click()
  await expect(toast(page, '2FA выключена')).toBeVisible()
  await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
  return pollStatus
}

/** Вкладка после выключения работает: следующий опрос проходит, на вход не выкинуло. */
async function tabStillWorks(page: Page) {
  const next = page.waitForResponse(r => /\/api\/admin\/telegram$/.test(r.url()) && r.request().method() === 'GET')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  expect((await next).status(), 'опрос после выключения 2FA').toBe(200)
  await expect(page).toHaveURL(/\/settings$/)
}

test.describe('2FA: выключение и опрос открытой вкладки', () => {
  tfaTest('опрос, ушедший со старой сессией до ответа disable, не получает 401', async ({ page, playwright, tfa }) => {
    const status = await disableWhilePolling(page, () => playwright.request.newContext(), tfa, 'after')
    expect([0, 200], 'опрос статуса бота, отправленный до ответа two-factor/disable').toContain(status)
    await tabStillWorks(page)
  })

  tfaTest('положительный контроль: опрос до выключения проходит, вкладка работает', async ({ page, playwright, tfa }) => {
    const status = await disableWhilePolling(page, () => playwright.request.newContext(), tfa, 'before')
    expect(status, 'опрос статуса бота до two-factor/disable').toBe(200)
    await tabStillWorks(page)
  })
})
