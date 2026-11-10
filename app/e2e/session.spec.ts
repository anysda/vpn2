import type { Page, PlaywrightWorkerArgs, Route, APIRequestContext } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, toast, uniq, test } from './support/fixtures'
import { totp } from './support/totp'

test.describe('password-change-session', () => {
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
})

test.describe('session-gone', () => {
  // Cookie сессии исчезла на живой странице (очистили, вышли в другой вкладке):
  // опросы панели не уходят без сессии и не сыплют 401 в консоль.
  // Сторож из fixtures ловит любой 401 за тест.

  test('cookie стёрты на странице настроек: опросы админки не уходят без сессии', async ({ page }) => {
    await loginApi(page)
    await page.goto('/settings')
    await hydrated(page)
    // Возврат на вкладку сразу обновляет статус бота: тот же запрос, что опрос раз в 10 с.
    const refreshed = page.waitForResponse(r => new URL(r.url()).pathname === '/api/admin/telegram')
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    expect((await refreshed).status(), 'положительный контроль: с сессией статус бота приходит').toBe(200)

    const sent: string[] = []
    page.on('request', (r) => {
      const path = new URL(r.url()).pathname
      if (path.startsWith('/api/') && !path.startsWith('/api/auth/')) sent.push(`${r.method()} ${path}`)
    })
    await page.context().clearCookies()
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    // Опрос доставок ботов идёт раз в 2 с: окно его захватывает.
    await page.waitForTimeout(2_500)
    expect(sent, 'запросы без сессии').toEqual([])
  })
})

test.describe('session-rotation-hang', () => {
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
})

test.describe('tfa-disable-session', () => {
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
   * Включить 2FA через API, открыть настройки и выключить её модалкой. Опрос
   * статуса бота со старой cookie идёт до клика «Выключить» (`before`) или
   * пока disable в пути, после выключения на сервере (`after`).
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
    if (pollLands === 'before') await poll()
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
})
