import type { Locator, Page } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, test } from './support/fixtures'
import { noHorizontalOverflow } from './support/overflow'
import { totp } from './support/totp'

/** Вход через Authentik включён: на экране входа есть кнопка SSO. */
const sso = {
  enabled: true,
  authentikUrl: 'https://auth.example.invalid',
  appSlug: 'vpn2-e2e',
  clientId: 'vpn2-e2e',
  clientSecret: 'e2e-client-secret-0123456789',
  allowedSubs: '00000000-0000-4000-8000-000000000001',
  label: 'Authentik',
  autoRedirect: false,
  passwordLogin: true,
  redirectUrl: 'https://vpn2.example.invalid/auth/authentik',
}

async function px(l: Locator, prop: string) {
  return l.evaluate((el, p) => Number.parseFloat(getComputedStyle(el).getPropertyValue(p)), prop)
}

/** Карточка во всю ширину с полями 16px и по центру экрана по высоте. */
async function cardFits(page: Page, where: string) {
  const vp = page.viewportSize()!
  const card = page.locator('.lgn')
  // Карточка выплывает и встряхивается при ошибке: мерить после движения.
  await card.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
  const box = (await card.boundingBox())!
  expect(Math.round(box.x), `${where}: отступ карточки слева`).toBe(16)
  expect(Math.round(box.width), `${where}: ширина карточки`).toBe(vp.width - 32)
  const below = vp.height - (box.y + box.height)
  expect(Math.abs(box.y - below), `${where}: карточка по центру (сверху ${box.y}, снизу ${below})`).toBeLessThanOrEqual(2)
}

/** Поля и кнопки под палец; шрифт полей не меньше 16px — Safari не увеличит страницу при фокусе. */
async function touchSized(page: Page, where: string) {
  for (const l of await page.locator('.lgn .in').all()) {
    expect(await px(l, 'height'), `${where}: высота поля`).toBe(46)
    expect(await px(l, 'font-size'), `${where}: шрифт поля`).toBeGreaterThanOrEqual(16)
  }
  for (const l of await page.locator('.lgn .pill').all()) {
    expect(await px(l, 'height'), `${where}: высота кнопки`).toBe(46)
  }
}

for (const width of [360, 402]) {
  test.describe(`телефон ${width}px`, () => {
    test.use({ viewport: { width, height: 874 }, isMobile: true, hasTouch: true })

    test('вход: обе темы по макету без горизонтальной прокрутки, пароль и код из аутентификатора, неверный код виден', async ({ page, guard, request }) => {
      test.setTimeout(180_000)
      guard.allow(401, /\/api\/auth\/two-factor\/verify-totp$/, 'POST')
      // Настройки — отдельной сессией API, страница остаётся без входа.
      const signIn = await authPost(request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
      expect(signIn.status(), await signIn.text()).toBe(200)
      expect((await request.put('/api/admin/sso', { data: sso })).status()).toBe(200)
      let secret = ''
      let usedStep = 0
      let enabled = false
      // Панель гасит принятый шаг: каждому подтверждению — шаг, которого она ещё не видела.
      const fresh = async () => {
        const now = Math.floor(Date.now() / 30_000)
        const step = Math.max(now, usedStep + 1)
        if (step > now + 1) await page.waitForTimeout((step - 1) * 30_000 - Date.now() + 500)
        usedStep = step
        return totp(secret, step * 30_000)
      }
      try {
        for (const scheme of ['dark', 'light'] as const) {
          await page.emulateMedia({ colorScheme: scheme })
          await page.goto('/login')
          await hydrated(page)
          await expect(page.getByRole('link', { name: 'Войти через Authentik' })).toBeVisible()
          await expect(page.getByText('или по паролю')).toBeVisible()
          await cardFits(page, `${scheme}, шаг пароля`)
          await touchSized(page, `${scheme}, шаг пароля`)
          await noHorizontalOverflow(page, `/login, ${scheme}`)
        }

        const en = await authPost(request, '/two-factor/enable', { password: ADMIN_PASSWORD })
        expect(en.status(), await en.text()).toBe(200)
        secret = new URL((await en.json()).totpURI).searchParams.get('secret') ?? ''
        const on = await authPost(request, '/two-factor/verify-totp', { code: await fresh() })
        expect(on.status(), await on.text()).toBe(200)
        enabled = true

        await page.getByLabel('Логин').fill(ADMIN_USER)
        await page.getByLabel('Пароль').fill(ADMIN_PASSWORD)
        await page.getByRole('button', { name: 'Войти' }).tap()
        const code = page.getByLabel('Код из аутентификатора')
        await expect(code).toBeVisible()
        expect(await px(code, 'font-size'), 'шрифт кода').toBe(17)
        expect(await px(code, 'letter-spacing'), 'разрядка кода').toBeCloseTo(17 * 0.12, 1)
        await expect(page.getByRole('button', { name: 'Назад' })).toBeVisible()
        await touchSized(page, 'шаг кода')

        const wrong = String((Number(totp(secret)) + 500_000) % 1_000_000).padStart(6, '0')
        await code.fill(wrong)
        await page.getByRole('button', { name: 'Подтвердить' }).tap()
        const err = page.locator('form .al.red')
        await expect(err).toHaveText('Код не подошёл')
        await expect(err).toBeInViewport({ ratio: 1 })
        await cardFits(page, 'неверный код')
        await noHorizontalOverflow(page, '/login, неверный код')
        await code.tap()
        expect(await page.evaluate(() => window.visualViewport?.scale ?? 1), 'масштаб после фокуса в поле').toBe(1)

        await code.fill(await fresh())
        await page.getByRole('button', { name: 'Подтвердить' }).tap()
        await expect(page).toHaveURL(/\/$/)
        await expect(page.locator('.mbar nav')).toBeVisible()
      }
      finally {
        // Упавший тест не оставляет admin с 2FA и включённым SSO.
        if (enabled) {
          const off = await authPost(request, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await fresh() })
          expect(off.status(), await off.text()).toBe(200)
        }
        expect((await request.put('/api/admin/sso', { data: { ...sso, enabled: false } })).status()).toBe(200)
        expect(await (await request.get('/api/sso')).json()).toMatchObject({ enabled: false })
      }
    })
  })
}
