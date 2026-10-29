import type { Page, PlaywrightWorkerArgs } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, test, toast, uniq } from './support/fixtures'
import { totp } from './support/totp'

/**
 * VPN2-575, тот же класс, что выключение 2FA: смена пароля и включение 2FA в
 * настройках пересоздают сессию — сервер ставит новую cookie и удаляет старую.
 * Опрос, ушедший со старой cookie, пока такой запрос в пути, не должен получать
 * 401. Окно держим сами: ответ браузер получает только после опроса.
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

/** Уборка 2FA своим API-контекстом, если тест её включил. */
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
 * Запрос `path` уходит на сервер своим контекстом со cookie браузера; пока
 * браузер ждёт ответ, страница опрашивает статус бота. Возвращает статус этого
 * опроса, 0 — панель его не отправила, пока запрос в пути.
 */
async function pollWhileRotating(page: Page, playwright: PlaywrightWorkerArgs['playwright'], path: RegExp) {
  let pollStatus = -1
  await page.route(path, async (route) => {
    const cookie = (await page.context().cookies(route.request().url())).map(c => `${c.name}=${c.value}`).join('; ')
    const api = await playwright.request.newContext()
    try {
      const response = await api.fetch(route.request(), { headers: { ...route.request().headers(), cookie } })
      const answered = page.waitForResponse(r => /\/api\/admin\/telegram$/.test(r.url()) && r.request().method() === 'GET', { timeout: 5_000 })
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      pollStatus = await answered.then(r => r.status(), () => 0)
      await route.fulfill({ response })
    }
    finally {
      await api.dispose()
    }
  })
  return () => pollStatus
}

/** Вкладка после пересоздания сессии работает: опрос проходит, на вход не выкинуло. */
async function tabStillWorks(page: Page) {
  const next = page.waitForResponse(r => /\/api\/admin\/telegram$/.test(r.url()) && r.request().method() === 'GET')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  expect((await next).status(), 'опрос после пересоздания сессии').toBe(200)
  await expect(page).toHaveURL(/\/settings$/)
}

async function openSettings(page: Page) {
  // Опросы по таймеру выключены: единственный опрос в окне — наш.
  await page.addInitScript(() => { window.setInterval = (() => 0) as unknown as typeof window.setInterval })
  await loginApi(page)
  await page.goto('/settings')
  await hydrated(page)
}

test.describe('пересоздание сессии и опрос открытой вкладки', () => {
  test('смена пароля: опрос, ушедший со старой сессией, не получает 401', async ({ page, playwright }) => {
    const temp = uniq('Pw')
    await openSettings(page)
    const pollStatus = await pollWhileRotating(page, playwright, /\/api\/auth\/change-password$/)
    const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Сохранить пароль' }) })
    let changed = false
    try {
      await form.getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
      await form.getByLabel('Новый пароль').fill(temp)
      await form.getByLabel('Повторите').fill(temp)
      await form.getByRole('button', { name: 'Сохранить пароль' }).click()
      await expect(toast(page, 'Пароль обновлён')).toBeVisible()
      changed = true
      expect([0, 200], 'опрос статуса бота, отправленный до ответа change-password').toContain(pollStatus())
      await tabStillWorks(page)
    }
    finally {
      if (changed) {
        await page.unrouteAll({ behavior: 'ignoreErrors' })
        let back = await authPost(page.request, '/change-password', { currentPassword: temp, newPassword: ADMIN_PASSWORD })
        if (back.status() !== 200) {
          await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: temp })
          back = await authPost(page.request, '/change-password', { currentPassword: temp, newPassword: ADMIN_PASSWORD })
        }
        expect(back.status(), await back.text()).toBe(200)
      }
    }
  })

  tfaTest('включение 2FA: опрос, ушедший со старой сессией, не получает 401', async ({ page, playwright, tfa }) => {
    await openSettings(page)
    const setup = page.locator('form').filter({ has: page.getByRole('button', { name: 'Включить 2FA' }) })
    await setup.getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
    await setup.getByRole('button', { name: 'Включить 2FA' }).click()
    tfa.secret = await page.getByLabel('Секрет').inputValue()
    expect(tfa.secret).not.toBe('')
    const pollStatus = await pollWhileRotating(page, playwright, /\/api\/auth\/two-factor\/verify-totp$/)
    await page.getByLabel('Введи 6-значный код из приложения').fill(await freshCode(tfa))
    await page.getByRole('button', { name: 'Подтвердить' }).click()
    await expect(toast(page, '2FA включена')).toBeVisible()
    expect([0, 200], 'опрос статуса бота, отправленный до ответа verify-totp').toContain(pollStatus())
    await tabStillWorks(page)
  })
})
