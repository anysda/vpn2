import { test as base, expect, type APIRequestContext, type Download, type Page } from '@playwright/test'

export const ADMIN_USER = process.env.E2E_ADMIN_USER || 'admin'
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || ''
const baseURL = new URL(process.env.E2E_BASE_URL || 'http://127.0.0.1:31710')
const baseHost = baseURL.hostname
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

/** Карточка и трафик клиента: GET, который гонка с удалением честно отвечает 404. */
const CLIENT_GET = /\/api\/clients\/(\d+)(\/traffic)?(\?.*)?$/

/**
 * Сторож страницы: ошибки консоли, исключения и HTTP-ответы >= 400. Ответы,
 * которых тест ждёт сам, он объявляет через allow(); остальное валит тест.
 */
export class Guard {
  readonly problems: string[] = []
  private allowed: Allowed[] = []
  private signedOut = false
  /** 404 карточки клиента по id; ошибка, только если клиент к концу теста есть. */
  private gone = new Map<number, string[]>()

  private maybeGone(status: number, url: string, method: string | undefined, problem: string) {
    const m = CLIENT_GET.exec(url)
    if (status !== 404 || !m || (method && method !== 'GET')) return false
    const id = Number(m[1])
    this.gone.set(id, [...this.gone.get(id) ?? [], problem])
    return true
  }

  /**
   * Клиента удалили (второй админ, тест через API), пока панель грузила его
   * карточку: 404 — законный ответ, панель молча выбирает следующего. Это
   * проверяется по факту: клиента с таким id нет (id не переиспользуются).
   * Существующий клиент с 404 или недоступный API — ошибка, как раньше.
   */
  async settle(api: APIRequestContext) {
    for (const [id, problems] of this.gone) {
      const status = (await api.get(`/api/clients/${id}`)).status()
      if (status !== 404) this.problems.push(...problems.map(p => `${p} (клиент ${id}: GET ${status})`))
    }
    this.gone.clear()
  }

  allow(status: number, url: RegExp, method?: string) {
    this.allowed.push({ status, url, method })
  }

  private isAllowed(status: number, url: string, method?: string) {
    // Поллеры могут закончить отложенный GET уже после удаления сессии.
    // Исключение действует только после успешного выхода и до нового входа.
    if (this.signedOut && status === 401 && /\/api\/(admin\/telegram|ops\/nodes)(\?.*)?$/.test(url)
      && (!method || method === 'GET')) return true
    return this.allowed.some(a => a.status === status && a.url.test(url) && (!a.method || !method || a.method === method))
  }

  attach(page: Page) {
    page.on('pageerror', e => this.problems.push(`pageerror: ${e.message}`))
    page.on('console', (m) => {
      if (m.type() !== 'error') return
      // «Failed to load resource: ... status of 401» дублирует ответ, разбор — по ответу.
      const s = /status of (\d{3})/.exec(m.text())
      if (s && this.isAllowed(Number(s[1]), m.location().url)) return
      const problem = `console.error: ${m.text()} @ ${m.location().url}`
      if (s && this.maybeGone(Number(s[1]), m.location().url, undefined, problem)) return
      this.problems.push(problem)
    })
    page.on('response', (r) => {
      const status = r.status()
      const method = r.request().method()
      if (status >= 200 && status < 300 && method === 'POST') {
        if (/\/api\/auth\/sign-out$/.test(r.url())) this.signedOut = true
        if (/\/api\/auth\/(sign-in\/|two-factor\/verify-)/.test(r.url())) this.signedOut = false
      }
      if (status < 400) return
      if (this.isAllowed(status, r.url(), method)) return
      const problem = `http ${status} ${method} ${r.url()}`
      if (this.maybeGone(status, r.url(), method, problem)) return
      this.problems.push(problem)
    })
  }
}

export const test = base.extend<{ guard: Guard }>({
  guard: [async ({ page }, use) => {
    const g = new Guard()
    g.attach(page)
    await use(g)
    // Опрос страницы может застать конец теста внутри обработчика route:
    // ответ route.fetch() освобождается вместе с контекстом, и json() в нём
    // падал бы уже после теста. Снимаем перехват, не дожидаясь таких вызовов.
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await g.settle(page.request)
    expect(g.problems, 'ошибки консоли, исключения и HTTP >= 400 за тест').toEqual([])
  }, { auto: true }],
})

export { expect }

/**
 * POST в ручки входа (/api/auth/*). Запрос с cookie они принимают только со
 * страницы панели — с её Origin, как шлёт браузер; запросы API-контекста
 * Playwright идут без Origin, его ставим сами.
 */
export function authPost(api: APIRequestContext, path: string, data: object = {}) {
  return api.post(`/api/auth${path}`, { data, headers: { origin: baseURL.origin } })
}

/** Вход через API: для тестов, где сам вход не предмет проверки. */
export async function loginApi(page: Page) {
  const r = await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
  expect(r.status(), await r.text()).toBe(200)
  const body = await r.json()
  expect(body.twoFactorRedirect, 'у admin осталась включённая 2FA от прошлого прогона').toBeFalsy()
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
  // path() недоступен у удалённого браузера; поток работает и в Docker, и локально.
  const stream = await d.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(Buffer.from(chunk))
  return Buffer.concat(chunks).toString('utf8')
}

/** Дождаться, пока Nuxt гидратирует страницу: до этого клики уходят в пустоту. */
export async function hydrated(page: Page) {
  // Наличие useNuxtApp ещё не означает гидратацию. Vue публикует приложение
  // на корневом элементе после mount; Nuxt снимает isHydrating после suspense.
  // Фоновые запросы не должны добавлять 500 мс тишины к каждому переходу.
  await page.waitForFunction(() => {
    const root = document.querySelector('#__nuxt') as (Element & {
      __vue_app__?: { $nuxt?: { isHydrating: boolean } }
    }) | null
    return root?.__vue_app__?.$nuxt?.isHydrating === false
  })
}

/**
 * Тост Nuxt UI: reka ToastRoot — li внутри ol области уведомлений. Открытая
 * модалка прячет соседей через aria-hidden, поэтому область ищется и скрытой.
 */
export function toast(page: Page, title: string | RegExp) {
  return page.getByRole('region', { includeHidden: true }).locator('ol > li').filter({ hasText: title }).first()
}
