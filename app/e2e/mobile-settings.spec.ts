import type { Page } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, loginUi, test, toast } from './support/fixtures'
import { noHorizontalOverflow } from './support/overflow'
import { totp } from './support/totp'

// Настройки на телефоне: одна колонка, подпись над полем, «Выключить 2FA?» — шторкой.

async function openSettings(page: Page) {
  await page.goto('/settings')
  await hydrated(page)
  await expect(page.locator('.stg fieldset.fg2')).toBeEnabled()
}

async function logoutUi(page: Page) {
  await page.getByRole('button', { name: /^Меню @/ }).tap()
  await page.getByRole('menuitem', { name: 'Выйти' }).tap()
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
}

test.describe('настройки на телефоне', () => {
  for (const width of [360, 402]) {
    for (const scheme of ['dark', 'light'] as const) {
      test(`${width} ${scheme}: одна колонка, поля целиком, вбок не едет`, async ({ page }) => {
        await page.setViewportSize({ width, height: 874 })
        await page.emulateMedia({ colorScheme: scheme })
        await loginApi(page)
        await openSettings(page)

        const cards = page.locator('.stg .sgrid > .card')
        await expect(cards).toHaveCount(3)
        const boxes = await cards.evaluateAll(els => els.map((el) => {
          const r = el.getBoundingClientRect()
          return { left: Math.round(r.left), width: Math.round(r.width) }
        }))
        const inner = await page.locator('.stg .sgrid').evaluate(el => el.clientWidth)
        for (const b of boxes) {
          expect(b.left, 'карточки стоят одна под другой').toBe(boxes[0]!.left)
          expect(b.width, 'карточка во всю ширину').toBe(inner)
        }

        // Каждое поле ввода: подпись над ним, ширина поля почти во всю карточку.
        const fields = await page.locator('.stg .sc label.fld').evaluateAll(els => els.map((el) => {
          const span = el.querySelector(':scope > span')!.getBoundingClientRect()
          const input = el.querySelector('input')!.getBoundingClientRect()
          const card = el.closest('.sc')!.getBoundingClientRect()
          return { name: el.querySelector(':scope > span')!.textContent, labelBottom: span.bottom, inputTop: input.top, input: input.width, card: card.width }
        }))
        expect(fields.length).toBeGreaterThanOrEqual(10)
        for (const f of fields) {
          expect(f.labelBottom, `«${f.name}»: подпись над полем`).toBeLessThanOrEqual(f.inputTop)
          expect(f.input, `«${f.name}»: поле не обрезано`).toBeGreaterThan(f.card * 0.6)
        }
        await noHorizontalOverflow(page, `/settings ${width} ${scheme}`)
        await page.getByRole('switch', { name: 'Вход через Authentik' }).tap()
        await noHorizontalOverflow(page, `/settings ${width} ${scheme}, Authentik вкл.`)
      })
    }
  }

  test('2FA: включить, войти с кодом, выключить в шторке, войти без кода', async ({ page }) => {
    test.setTimeout(150_000)
    await loginApi(page)
    await openSettings(page)
    await page.locator('#tfa').getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
    await page.getByRole('button', { name: 'Включить 2FA' }).tap()
    const secretInput = page.locator('#tfa input[readonly]')
    await expect(secretInput).toHaveValue(/^[A-Z2-7]{16,}$/)
    const secret = await secretInput.inputValue()
    // Принятый шаг гасится: каждому коду — шаг, которого панель ещё не видела.
    let usedStep = 0
    let enabled = false
    const fresh = async () => {
      const now = Math.floor(Date.now() / 30_000)
      const step = Math.max(now, usedStep + 1)
      if (step > now + 1) await page.waitForTimeout((step - 1) * 30_000 - Date.now() + 500)
      usedStep = step
      return totp(secret, step * 30_000)
    }
    try {
      await page.getByLabel('Введи 6-значный код из приложения').fill(await fresh())
      await page.getByRole('button', { name: 'Подтвердить' }).tap()
      enabled = true
      await expect(toast(page, '2FA включена')).toBeVisible()

      await logoutUi(page)
      await page.getByLabel('Логин').fill(ADMIN_USER)
      await page.getByLabel('Пароль').fill(ADMIN_PASSWORD)
      await page.getByRole('button', { name: 'Войти' }).tap()
      const code = page.getByPlaceholder('123 456')
      await expect(code).toBeVisible()
      await code.fill(await fresh())
      await page.getByRole('button', { name: 'Подтвердить' }).tap()
      await expect(page).toHaveURL(/\/$/)

      await openSettings(page)
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Выключить 2FA' }).tap()
      const sheet = page.getByRole('dialog', { name: 'Выключить 2FA?' })
      await expect(sheet).toBeVisible()
      await expect(sheet, 'окно — нижней шторкой').toHaveClass(/\bbsh\b/)
      await noHorizontalOverflow(page, 'шторка «Выключить 2FA?»')
      // «Отмена» ничего не меняет.
      await sheet.getByRole('button', { name: 'Отмена' }).tap()
      await expect(sheet).toBeHidden()
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()

      await page.getByRole('button', { name: 'Выключить 2FA' }).tap()
      await sheet.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
      await sheet.getByLabel('Код 2FA (6 цифр)').fill(await fresh())
      await sheet.getByRole('button', { name: 'Выключить' }).tap()
      await expect(toast(page, '2FA выключена')).toBeVisible()
      enabled = false
      await expect(sheet).toBeHidden()
      await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
    }
    finally {
      if (enabled) {
        await page.context().clearCookies()
        await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD }).catch(() => {})
        await authPost(page.request, '/two-factor/verify-totp', { code: await fresh() }).catch(() => {})
        await authPost(page.request, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await fresh() }).catch(() => {})
      }
    }
    // Вход снова без кода.
    await page.context().clearCookies()
    await loginUi(page)
    await expect(page).toHaveURL(/\/$/)
  })

  test('смена пароля, затем вход с новым', async ({ page }) => {
    await loginApi(page)
    await openSettings(page)
    const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Сохранить пароль' }) })
    const temp = `${ADMIN_PASSWORD}-m388`
    let changed = false
    try {
      await form.getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
      await form.getByLabel('Новый пароль').fill(temp)
      await form.getByLabel('Повторите').fill(temp)
      await form.getByRole('button', { name: 'Сохранить пароль' }).tap()
      await expect(toast(page, 'Пароль обновлён')).toBeVisible()
      changed = true
      await page.context().clearCookies()
      await loginUi(page, temp)
      await expect(page).toHaveURL(/\/$/)
    }
    finally {
      if (changed) {
        // Открытая главная опрашивает API: после снятия cookie это 401 в журнал.
        await page.goto('about:blank')
        await page.context().clearCookies()
        await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: temp })
        const back = await authPost(page.request, '/change-password', { currentPassword: temp, newPassword: ADMIN_PASSWORD })
        expect(back.status()).toBe(200)
      }
    }
    await page.context().clearCookies()
    await loginApi(page)
  })

  test('бот и Authentik сохраняются с телефона', async ({ page }) => {
    await loginApi(page)
    const tg = await (await page.request.get('/api/admin/telegram')).json()
    const sso = await (await page.request.get('/api/admin/sso')).json()
    try {
      await openSettings(page)
      const bot = page.locator('.sbot')
      const nick = bot.getByLabel('Никнейм админа бота', { exact: true })
      const temp = `m388_${Date.now().toString(36)}`
      await nick.fill(temp)
      await bot.getByRole('button', { name: 'Сохранить' }).tap()
      await expect(toast(page, 'Настройки бота сохранены')).toBeVisible()
      expect((await (await page.request.get('/api/admin/telegram')).json()).admin_username).toBe(temp)

      const card = page.locator('form').filter({ hasText: 'Кого пускать' })
      await card.getByLabel('Подпись кнопки').fill('Вход m388')
      await card.getByRole('button', { name: 'Сохранить' }).tap()
      await expect(toast(page, 'Настройки входа сохранены')).toBeVisible()
      expect((await (await page.request.get('/api/admin/sso')).json()).label).toBe('Вход m388')
    }
    finally {
      await page.request.put('/api/admin/telegram', { data: { admin_username: tg.admin_username } })
      await page.request.put('/api/admin/sso', { data: { ...sso, clientSecret: '' } })
    }
    expect((await (await page.request.get('/api/admin/sso')).json()).label).toBe(sso.label)
  })
})
