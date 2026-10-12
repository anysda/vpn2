import { expect, test } from '@playwright/test'
import { Guard } from './support/fixtures'

test('сторож: отложенный telegram 401 после успешного выхода ожидаем', async ({ page }) => {
  const guard = new Guard()
  guard.attach(page)
  await page.route('**/*', route => route.fulfill({
    status: route.request().url().endsWith('/api/admin/telegram') ? 401 : 200,
    contentType: 'application/json',
    body: '{}',
  }))
  await page.goto('/')
  await page.evaluate(async () => {
    await fetch('/api/auth/sign-out', { method: 'POST' })
    await fetch('/api/admin/telegram')
  })
  expect(guard.problems).toEqual([])
})

test('сторож: 401 до выхода, после неудачного выхода и после нового входа остаются ошибками', async ({ page }) => {
  const guard = new Guard()
  guard.attach(page)
  let logoutStatus = 500
  await page.route('**/*', route => route.fulfill({
    status: route.request().url().endsWith('/api/auth/sign-out') ? logoutStatus
      : route.request().url().endsWith('/api/admin/telegram') ? 401 : 200,
    contentType: 'application/json',
    body: '{}',
  }))
  await page.goto('/')
  await page.evaluate(async () => {
    await fetch('/api/admin/telegram')
    await fetch('/api/auth/sign-out', { method: 'POST' })
    await fetch('/api/admin/telegram')
  })
  logoutStatus = 200
  await page.evaluate(async () => {
    await fetch('/api/auth/sign-out', { method: 'POST' })
    await fetch('/api/auth/sign-in/username', { method: 'POST' })
    await fetch('/api/admin/telegram')
  })
  expect(guard.problems.filter(p => p.startsWith('http 401 GET'))).toHaveLength(3)
})

test('сторож: после выхода посторонние ошибки и запись в telegram не скрываются', async ({ page }) => {
  const guard = new Guard()
  guard.attach(page)
  await page.route('**/*', (route) => {
    const path = new URL(route.request().url()).pathname
    return route.fulfill({ status: path === '/api/auth/sign-out' || path === '/' ? 200 : 401, body: '{}' })
  })
  await page.goto('/')
  await page.evaluate(async () => {
    await fetch('/api/auth/sign-out', { method: 'POST' })
    await fetch('/api/admin/telegram', { method: 'PUT' })
    await fetch('/api/clients')
  })
  expect(guard.problems.filter(p => p.startsWith('http 401'))).toHaveLength(2)
})

for (const { status, path } of [
  { status: 500, path: '/api/admin/telegram' },
  { status: 401, path: '/api/admin/telegram/settings' },
]) {
  test(`сторож: после выхода GET ${path} со статусом ${status} остаётся ошибкой`, async ({ page }) => {
    const guard = new Guard()
    guard.attach(page)
    await page.route('**/*', route => route.fulfill({
      status: new URL(route.request().url()).pathname === path ? status : 200,
      contentType: 'application/json',
      body: '{}',
    }))
    await page.goto('/')
    await page.evaluate(async (path) => {
      await fetch('/api/auth/sign-out', { method: 'POST' })
      await fetch(path)
    }, path)
    // Проверяем конкретный ответ: посторонняя ошибка не должна озеленять тест.
    await expect.poll(() => guard.problems.filter(p => p.startsWith('http '))).toEqual([
      `http ${status} GET ${new URL(path, page.url()).href}`,
    ])
  })
}
