import { readFile } from 'node:fs/promises'
import { test as base, expect, type Download, type Page } from '@playwright/test'
import { totp } from './totp'

export const ADMIN_USER = process.env.E2E_ADMIN_USER || 'admin'
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || ''
const baseHost = new URL(process.env.E2E_BASE_URL || 'http://127.0.0.1:51899').hostname
/** Хост, который панель пишет в Endpoint/remote конфигов. */
export const PUBLIC_HOST = process.env.E2E_PUBLIC_HOST || baseHost
/** Теги нод мониторинга, которые обязаны быть на экране. */
export const NODES = (process.env.E2E_NODES || '').split(/\s+/).filter(Boolean)

if (!ADMIN_PASSWORD) throw new Error('E2E_ADMIN_PASSWORD не задан')

/** Уникальное имя тестового объекта; всё с префиксом e2e- снесёт teardown. */
export function uniq(kind: string): string {
  return `e2e-${kind}-${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`
}

interface Allowed { status: number, url: RegExp, method?: string }

/**
 * Сторож страницы: ошибки консоли, исключения и HTTP-ответы >= 400. Ответы,
 * которых тест ждёт сам, он объявляет через allow(); остальное валит тест.
 */
export class Guard {
  readonly problems: string[] = []
  private allowed: Allowed[] = []

  allow(status: number, url: RegExp, method?: string) {
    this.allowed.push({ status, url, method })
  }

  private isAllowed(status: number, url: string, method?: string) {
    return this.allowed.some(a => a.status === status && a.url.test(url) && (!a.method || !method || a.method === method))
  }

  attach(page: Page) {
    page.on('pageerror', e => this.problems.push(`pageerror: ${e.message}`))
    page.on('console', (m) => {
      if (m.type() !== 'error') return
      // «Failed to load resource: ... status of 401» дублирует ответ, разбор — по ответу.
      const s = /status of (\d{3})/.exec(m.text())
      if (s && this.isAllowed(Number(s[1]), m.location().url)) return
      this.problems.push(`console.error: ${m.text()} @ ${m.location().url}`)
    })
    page.on('response', (r) => {
      const status = r.status()
      if (status < 400) return
      const method = r.request().method()
      if (this.isAllowed(status, r.url(), method)) return
      this.problems.push(`http ${status} ${method} ${r.url()}`)
    })
  }
}

export const test = base.extend<{ guard: Guard }>({
  guard: [async ({ page }, use) => {
    const g = new Guard()
    g.attach(page)
    await use(g)
    expect(g.problems, 'ошибки консоли, исключения и HTTP >= 400 за тест').toEqual([])
  }, { auto: true }],
})

export { expect }

/** Вход через API: для тестов, где сам вход не предмет проверки. */
export async function loginApi(page: Page, totpSecret?: string) {
  const data: Record<string, string> = { username: ADMIN_USER, password: ADMIN_PASSWORD }
  if (totpSecret) data.totpCode = totp(totpSecret)
  const r = await page.request.post('/api/auth/login', { data })
  expect(r.status(), await r.text()).toBe(200)
  const body = await r.json()
  expect(body.needsTotp, 'у admin осталась включённая 2FA от прошлого прогона').toBeFalsy()
}

/** Вход формой /login. */
export async function loginUi(page: Page, password = ADMIN_PASSWORD) {
  await page.goto('/login')
  // До гидратации v-model не связан: поля заполняются, а форма уходит пустой.
  await hydrated(page)
  await page.getByLabel('Логин').fill(ADMIN_USER)
  await page.getByLabel('Пароль').fill(password)
  await page.getByRole('button', { name: 'Войти' }).click()
}

export async function downloadText(d: Download): Promise<string> {
  const p = await d.path()
  return readFile(p, 'utf8')
}

/** Дождаться, пока Nuxt гидратирует страницу: до этого клики уходят в пустоту. */
export async function hydrated(page: Page) {
  await page.waitForFunction(() => (window as unknown as { useNuxtApp?: unknown }).useNuxtApp !== undefined
    || document.querySelector('#__nuxt')?.hasAttribute('data-v-app'))
  await page.waitForLoadState('networkidle')
}

/**
 * Тост Nuxt UI: reka ToastRoot — li внутри ol области уведомлений. Открытая
 * модалка прячет соседей через aria-hidden, поэтому область ищется и скрытой.
 */
export function toast(page: Page, title: string | RegExp) {
  return page.getByRole('region', { includeHidden: true }).locator('ol > li').filter({ hasText: title }).first()
}
