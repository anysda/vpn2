import type { Route } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, test } from './support/fixtures'
import { totp } from './support/totp'

/**
 * VPN2-575: пока запрос, пересоздающий сессию, в пути, панель держит остальные
 * `/api/*`. Если он завис без ответа (сервер или прокси подвис на выключении
 * 2FA), ворота не держат вкладку дольше своего предела: опросы, разделы и выход
 * уходят дальше, а не ждут до перезагрузки.
 */

/** Предел ворот в плагине session-rotation и запас на сам опрос. */
const GATE_MS = 15_000
const SLACK_MS = 10_000

interface Totp { secret: string, usedStep: number }

/** Код шага, которого панель ещё не видела (VPN2-62 гасит принятый шаг). */
async function freshCode(state: Totp) {
  const now = Math.floor(Date.now() / 30_000)
  const step = Math.max(now, state.usedStep + 1)
  if (step > now + 1) await new Promise(resolve => setTimeout(resolve, (step - 1) * 30_000 - Date.now() + 500))
  state.usedStep = step
  return totp(state.secret, step * 30_000)
}

/** Уборка 2FA своим API-контекстом: зависший disable до сервера не дошёл. */
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

test.describe('2FA: зависшее выключение не держит вкладку', () => {
  tfaTest('опрос уходит и отвечает, пока two-factor/disable висит без ответа', async ({ page, guard, tfa }) => {
    // Опросы по таймеру выключены: единственный опрос в окне — наш.
    await page.addInitScript(() => { window.setInterval = (() => 0) as unknown as typeof window.setInterval })
    await loginApi(page)
    const setup = await authPost(page.request, '/two-factor/enable', { password: ADMIN_PASSWORD })
    expect(setup.status(), await setup.text()).toBe(200)
    tfa.secret = new URL((await setup.json()).totpURI).searchParams.get('secret')!
    const enable = await authPost(page.request, '/two-factor/verify-totp', { code: await freshCode(tfa) })
    expect(enable.status(), await enable.text()).toBe(200)

    let release!: () => void
    const released = new Promise<void>((resolve) => { release = resolve })
    let held!: () => void
    const holding = new Promise<void>((resolve) => { held = resolve })
    // Сервер не отвечает на выключение: запрос висит, пока тест не отпустит,
    // потом прокси отдаёт 504, как подвисший upstream. 2FA остаётся включённой.
    guard.allow(504, /\/api\/auth\/two-factor\/disable$/, 'POST')
    await page.route(/\/api\/auth\/two-factor\/disable$/, async (route: Route) => {
      held()
      await released
      await route.fulfill({ status: 504, contentType: 'text/plain', body: 'gateway timeout' })
    })

    try {
      await page.goto('/settings')
      await hydrated(page)
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      const dlg = page.getByRole('dialog', { name: 'Выключить 2FA?' })
      await dlg.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
      await dlg.getByLabel('Код 2FA (6 цифр)').fill(await freshCode(tfa))
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await holding

      // Человек вернулся на вкладку, пока выключение висит: опрос статуса бота
      // уходит не позже предела ворот и проходит со старой, живой сессией.
      const answered = page.waitForResponse(
        r => /\/api\/admin\/telegram$/.test(r.url()) && r.request().method() === 'GET',
        { timeout: GATE_MS + SLACK_MS },
      )
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      expect((await answered).status(), 'опрос статуса бота, пока two-factor/disable висит').toBe(200)
      await expect(page).toHaveURL(/\/settings$/)

      const gaveUp = page.waitForResponse(r => /\/api\/auth\/two-factor\/disable$/.test(r.url()))
      release()
      expect((await gaveUp).status(), 'two-factor/disable после отпуска').toBe(504)
    }
    finally {
      release()
    }
  })
})
