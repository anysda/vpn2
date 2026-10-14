import { ADMIN_USER, expect, hydrated, loginApi, test } from './support/fixtures'

const TABS = [
  { path: '/', tab: 'Обзор', title: 'Обзор' },
  { path: '/clients', tab: 'Клиенты', title: 'Клиенты' },
  { path: '/routes', tab: 'Маршруты', title: 'Маршруты' },
  { path: '/dns', tab: 'DNS', title: 'DNS' },
  { path: '/settings', tab: 'Настройки', title: 'Настройки' },
]

test.describe('разделы шапки', () => {
  test('каждый раздел открывается прямой ссылкой и отмечен в шапке', async ({ page }) => {
    await loginApi(page)
    for (const t of TABS) {
      await page.goto(t.path)
      await hydrated(page)
      await expect(page).toHaveTitle(new RegExp(`^${t.title} — `))
      const nav = page.getByRole('navigation')
      await expect(nav.getByRole('link', { name: t.tab, exact: true })).toHaveAttribute('aria-current', 'page')
      await expect(nav.locator('[aria-current="page"]')).toHaveCount(1)
    }
  })

  test('переход по вкладке и «назад» без перезагрузки', async ({ page }) => {
    await loginApi(page)
    await page.goto('/routes')
    await hydrated(page)
    await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
    await page.evaluate(() => Reflect.set(window, '__sameDocument', true))
    await page.getByRole('navigation').getByRole('link', { name: 'Клиенты', exact: true }).click()
    await expect(page).toHaveURL(/\/clients$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Клиенты и ключи' })).toBeVisible()
    await page.goBack()
    await expect(page).toHaveURL(/\/routes$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
    expect(await page.evaluate(() => Reflect.get(window, '__sameDocument'))).toBe(true)
  })

  test('переход поверх незаконченного перехода не роняет ошибку на страницу', async ({ page }) => {
    await loginApi(page)
    await page.goto('/routes')
    await hydrated(page)
    // Вторая вкладка нажата, пока первый переход ещё снимает старую страницу:
    // браузер отменяет первый переход, его отказ не должен всплыть на страницу.
    await page.evaluate(() => {
      const link = (name: string) => [...document.querySelectorAll<HTMLAnchorElement>('nav a')].find(a => a.textContent?.trim() === name)!
      new MutationObserver((_, observer) => {
        if (document.documentElement.dataset.vt !== 'page') return
        observer.disconnect()
        link('DNS').click()
      }).observe(document.documentElement, { attributeFilter: ['data-vt'] })
      link('Клиенты').click()
    })
    const nav = page.getByRole('navigation')
    await expect(page).toHaveURL(/\/dns$/)
    await expect(page).toHaveTitle(/^DNS — /)
    await expect(nav.locator('[aria-current="page"]')).toHaveText('DNS')
    await expect(page.locator('html[data-vt]')).toHaveCount(0)
  })
})

test('экран ошибки: код и текст макета без сообщения фреймворка', async ({ page, guard }) => {
  guard.allow(404, /\/no-such-page$/, 'GET')
  await loginApi(page)
  const response = await page.goto('/no-such-page')
  expect(response?.status()).toBe(404)
  await hydrated(page)
  const card = page.locator('.errp')
  await expect(card.getByRole('heading', { name: 'Ошибка' })).toBeVisible()
  await expect(card.getByText('404', { exact: true })).toBeVisible()
  await expect(card.getByText('Что-то пошло не так.', { exact: true })).toBeVisible()
  await expect(page.getByText(/Page not found/)).toHaveCount(0)
  await expect(card.getByRole('button', { name: 'Обновить' })).toBeVisible()
  await card.getByRole('button', { name: 'На главную' }).click()
  await expect(page).toHaveURL(/\/$/)
})

test.describe('тема', () => {
  test('выбор темы переживает перезагрузку и стоит до первой отрисовки', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    // Тема на <html> в момент разбора документа — до скриптов приложения.
    await page.addInitScript(() => {
      document.addEventListener('DOMContentLoaded', () => {
        Reflect.set(window, '__themeAtParse', document.documentElement.dataset.theme ?? null)
      })
    })
    await loginApi(page)
    await page.goto('/')
    await hydrated(page)
    const html = page.locator('html')
    await expect(html).toHaveAttribute('data-theme', 'dark')

    const choose = async (label: string) => {
      await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
      await page.getByRole('group', { name: 'Тема' }).getByRole('button', { name: label, exact: true }).click()
      await page.keyboard.press('Escape')
    }
    await choose('Светлая')
    await expect(html).toHaveAttribute('data-theme', 'light')
    await page.reload()
    await hydrated(page)
    expect(await page.evaluate(() => Reflect.get(window, '__themeAtParse'))).toBe('light')
    await expect(html).toHaveAttribute('data-theme', 'light')

    // «Как в системе» следует за сменой темы ОС без перезагрузки.
    await choose('Как в системе')
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await page.emulateMedia({ colorScheme: 'light' })
    await expect(html).toHaveAttribute('data-theme', 'light')
  })
})
