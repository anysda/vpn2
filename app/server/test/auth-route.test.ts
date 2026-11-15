import { expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({ response: null as unknown as Response }))
vi.mock('../utils/auth', () => ({ SSO_PROVIDER: 'authentik', useAuth: () => ({ handler: async () => auth.response }) }))
vi.mock('../utils/bot', () => ({ journalFailedLogin: async () => {} }))
vi.stubGlobal('defineEventHandler', (handler: unknown) => handler)
vi.stubGlobal('getRequestHeader', (event: { headers: Record<string, string> }, name: string) => event.headers[name])
vi.stubGlobal('toWebRequest', () => new Request('http://panel.test/api/auth/sign-in/username', { method: 'POST', body: '{}' }))

const handler = (await import('../api/auth/[...all]')).default as unknown as (event: unknown) => Promise<Response>
const signIn = async (headers: Record<string, string>) => {
  auth.response = new Response(JSON.stringify({ message: 'Too many requests' }), { status: 429, headers })
  const event = { path: '/api/auth/sign-in/username', method: 'POST', headers: { origin: 'http://panel.test', host: 'panel.test' }, node: { req: { socket: { remoteAddress: '198.51.100.7' } } } }
  return (await handler(event)).json()
}

it('429 ограничителя входа: срок из x-retry-after вверх до минут, без заголовка - прежний текст', async () => {
  expect(await signIn({ 'x-retry-after': '286' })).toEqual({ statusCode: 429, statusMessage: 'too_many_requests', message: 'Слишком много попыток, попробуйте через 5 мин' })
  expect((await signIn({ 'x-retry-after': '7' })).message).toBe('Слишком много попыток, попробуйте через 1 мин')
  expect((await signIn({})).message).toBe('Слишком много попыток, попробуйте позже')
})
