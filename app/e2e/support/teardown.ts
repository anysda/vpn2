import { request } from '@playwright/test'

/**
 * Уборка после прогона, в том числе упавшего: клиенты с префиксом e2e- и
 * правила маршрутизации из тестовых диапазонов (RFC 5737) и доменов e2e-*.
 */
const TEST_ROUTE = /^(\*\.)?e2e-|^198\.51\.100\.|^203\.0\.113\./

export default async function teardown() {
  const baseURL = process.env.E2E_BASE_URL
  const password = process.env.E2E_ADMIN_PASSWORD
  if (!baseURL || !password) return
  const api = await request.newContext({ baseURL, extraHTTPHeaders: { origin: new URL(baseURL).origin } })
  try {
    const login = await api.post('/api/auth/sign-in/username', {
      data: { username: process.env.E2E_ADMIN_USER || 'admin', password },
      headers: { origin: new URL(baseURL).origin },
    })
    if (!login.ok() || (await login.json()).twoFactorRedirect) {
      console.error(`teardown: вход не удался (${login.status()}), уборка пропущена`)
      return
    }
    const clients: { id: number, name: string }[] = await (await api.get('/api/clients')).json()
    for (const c of clients.filter(c => c.name.startsWith('e2e-'))) {
      await api.delete(`/api/clients/${c.id}`)
    }
    const routes: { id: number, value: string }[] = await (await api.get('/api/routes')).json()
    for (const r of routes.filter(r => TEST_ROUTE.test(r.value))) {
      await api.delete(`/api/routes/${r.id}`)
    }
  }
  finally {
    await api.dispose()
  }
}
