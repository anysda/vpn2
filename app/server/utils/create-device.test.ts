import { beforeEach, describe, expect, it, vi } from 'vitest'
import { calls, dbRows, fakeEvent, installStubs } from '../test/stubs'
import { provisionDeviceKeys } from './create-device'
import { ensureDeviceWg, syncWireguardConfig } from './wireguard'
import { ensureDeviceOvpn } from './openvpn'
import { ensureDeviceIkev2, syncIkev2 } from './ikev2'

vi.hoisted(() => vi.stubGlobal('defineEventHandler', (h: unknown) => h))
vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./bot-api', async () => ({ ...(await import('../test/stubs')).botApiModule, botClientView: vi.fn(async () => 'view') }))
vi.mock('./validate', () => ({ readBodyAs: vi.fn(async (e: { body: unknown }) => e.body) }))
vi.mock('./client-prefs', () => ({ syncClientPrefsFile: vi.fn(async () => { calls.push('prefs') }) }))
vi.mock('./bot-events', () => ({ notifyClient: vi.fn(), notifyWarning: vi.fn() }))
vi.mock('./wireguard', () => ({ ensureDeviceWg: vi.fn(), syncWireguardConfig: vi.fn() }))
vi.mock('./openvpn', () => ({ ensureDeviceOvpn: vi.fn(), caReady: vi.fn(async () => true) }))
vi.mock('./ikev2', () => ({ ensureDeviceIkev2: vi.fn(), syncIkev2: vi.fn() }))
vi.mock('./create-device', async importOriginal => ({
  ...await importOriginal<typeof import('./create-device')>(),
  deviceRequestKey: () => undefined,
  rejectDeletedDeviceRepeat: async () => {},
  createDeviceWithAddresses: vi.fn(async () => ({ device: { id: 5, name: 'phone', createdAt: 1 }, replay: false })),
}))

const steps = {
  wg: vi.mocked(ensureDeviceWg),
  ovpn: vi.mocked(ensureDeviceOvpn),
  ikev2: vi.mocked(ensureDeviceIkev2),
  wgSync: vi.mocked(syncWireguardConfig),
  ikev2Sync: vi.mocked(syncIkev2),
}
const log = { wg: 'L-wg', ovpn: 'L-ovpn', ikev2: 'L-ikev2', wgSync: 'L-wgSync', ikev2Sync: 'L-ikev2Sync' }
const logged: string[] = []

beforeEach(() => {
  installStubs({ wgEnabled: true, ovpnEnabled: true })
  logged.length = 0
  vi.stubGlobal('useLogger', () => ({ error: (_: unknown, msg: string) => { logged.push(msg) } }))
  for (const [name, fn] of Object.entries(steps)) {
    fn.mockReset().mockImplementation((async (id?: number) => { calls.push(id === undefined ? name : `${name}:${id}`) }) as never)
  }
})

/** Шаг падает, но отметку о вызове оставляет. */
function failStep(name: keyof typeof steps) {
  steps[name].mockImplementation((async () => {
    calls.push(`${name}!`)
    throw new Error(name)
  }) as never)
}

describe('provisionDeviceKeys', () => {
  it('порядок: ключи wg, ovpn, ikev2, затем сверка wg и ikev2', async () => {
    await provisionDeviceKeys(5, true, log)
    expect(calls).toEqual(['wg:5', 'ovpn:5', 'ikev2:5', 'wgSync', 'ikev2Sync'])
    expect(logged).toEqual([])
  })

  it('ovpnReady false - ключ OpenVPN не выдаётся, остальное как обычно', async () => {
    await provisionDeviceKeys(5, false, log)
    expect(calls).toEqual(['wg:5', 'ikev2:5', 'wgSync', 'ikev2Sync'])
  })

  it('wgEnabled выключен - ключ WireGuard не выдаётся, сверка wg идёт', async () => {
    installStubs({ wgEnabled: false })
    await provisionDeviceKeys(5, true, log)
    expect(calls).toEqual(['ovpn:5', 'ikev2:5', 'wgSync', 'ikev2Sync'])
  })

  it.each(Object.keys(steps) as (keyof typeof steps)[])('сбой шага %s - в журнал своей строкой, остальные шаги идут', async (name) => {
    failStep(name)
    await provisionDeviceKeys(5, true, log)
    expect(calls).toHaveLength(5)
    expect(calls).toContain(`${name}!`)
    expect(logged).toEqual([log[name]])
  })
})

const allFail = () => (Object.keys(steps) as (keyof typeof steps)[]).forEach(failStep)

describe('маршруты создания отдают свои строки журнала', () => {
  it('бот', async () => {
    const { default: route } = await import('../api/bot/devices/index.post')
    dbRows.clients = [{ id: 7, tgChatId: 42 }]
    allFail()
    expect(await route({ body: { chatId: 42, name: 'phone' } } as never)).toBe('view')
    expect(logged).toEqual([
      'bot: ensureDeviceWg failed', 'bot: ensureDeviceOvpn failed', 'bot: ensureDeviceIkev2 failed',
      'bot: wg sync failed', 'bot: ikev2 sync failed',
    ])
  })

  it('панель - и сверка настроек клиента после выдачи', async () => {
    const { default: route } = await import('../api/clients/[id]/devices/index.post')
    dbRows.clients = [{ id: 7, name: 'Анна', tgChatId: null }]
    allFail()
    const event = Object.assign(fakeEvent({ id: '7' }) as object, { body: { name: 'phone' } })
    expect(await route(event as never)).toEqual({ id: 5, clientId: 7, name: 'phone', createdAt: 1 })
    expect(logged).toEqual([
      'ensureDeviceWg on add failed', 'ensureDeviceOvpn on add failed', 'ensureDeviceIkev2 on add failed',
      'wg sync after device add failed', 'ikev2 sync after device add failed',
    ])
    expect(calls.at(-1)).toBe('prefs')
  })
})
