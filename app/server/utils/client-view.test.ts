import { beforeEach, describe, expect, it, vi } from 'vitest'
import { auth, calls, dbResults, fakeEvent, installStubs, rejection, stubError } from '../test/stubs'
import clients from '../resources/clients'
import { deviceConfigName } from './naming'

const access = vi.hoisted(() => {
  (globalThis as Record<string, unknown>).defineEventHandler = (handler: unknown) => handler
  return { byClient: new Map<number, unknown>(), latest: null as unknown }
})

const adminList = clients['GET /clients']
const adminCard = clients['GET /clients/:id']
const botList = clients['GET /bot/admin/clients']
const botCard = clients['GET /bot/admin/clients/:id']

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./bot-api', async () => (await import('../test/stubs')).botApiModule)
vi.mock('./access-operations', () => ({
  currentAccessByClient: async () => access.byClient,
  latestAccess: async () => access.latest,
}))

const created = new Date('2026-01-02T03:04:05.000Z')
const anna = {
  id: 7, name: 'Anna', password: 'secret', filterTraffic: true, expiresAt: new Date('2031-05-06T07:08:09.000Z'), deviceLimit: 3,
  frozenManual: false, deleting: false, createdAt: created, tgChatId: 42, tgUsername: 'anna', preferredExit: 'fra', accessVersion: 3,
}
const boris = {
  id: 8, name: 'Boris', password: 'other', filterTraffic: false, expiresAt: null, deviceLimit: 1,
  frozenManual: true, deleting: false, createdAt: created, tgChatId: null, tgUsername: null, preferredExit: null, accessVersion: 1,
}
const state = { state: 'pending', confirmedVersion: null, checkedAt: null }
const operation = { id: 'op-1', clientId: 7, kind: 'unfreeze', status: 'pending', version: 3, protocols: { wg: state, openvpn: state, ikev2: state } }
// Счётчик n лежит в каждой строке: число девайсов бота не зависит от того,
// считает ли ручка count() или длину списка.
const devices = [
  { id: 12, clientId: 7, name: 'laptop', createdAt: created, rxTotal: 100, txTotal: 200, wgPrivateKey: 'k', wgPublicKey: 'p', wgIp: '10.66.66.2', ovpnCert: null, ovpnKey: null, n: 2 },
  { id: 13, clientId: 7, name: 'phone', createdAt: created, rxTotal: 5, txTotal: 6, wgPrivateKey: null, wgPublicKey: null, wgIp: null, ovpnCert: 'c', ovpnKey: 'k', n: 2 },
]

const open = async (card: (event: never) => Promise<unknown>, id: string) => card(fakeEvent({ id }))

beforeEach(() => {
  installStubs()
  access.byClient = new Map([[7, operation]])
  access.latest = operation
})

const json = (value: unknown) => JSON.parse(JSON.stringify(value))
const fields = (c: typeof anna | typeof boris) => ({
  id: c.id, name: c.name, filterTraffic: c.filterTraffic, expiresAt: c.expiresAt, deviceLimit: c.deviceLimit,
  frozenManual: c.frozenManual, deleting: false, createdAt: created, tgUsername: c.tgUsername, preferredExit: c.preferredExit,
})

describe('списки клиентов: одни поля у админа и у бота, без пароля и чата', () => {
  const rows = () => dbResults.push([anna, boris], [{ clientId: 7, cnt: 2, rx: 105, tx: 206 }])
  const expected = json([
    { ...fields(anna), accessStatus: 'Включение выполняется', status: 'active', tgLinked: true, deviceCount: 2, rxTotal: 105, txTotal: 206 },
    { ...fields(boris), accessStatus: 'Состояние не подтверждено', status: 'frozen', tgLinked: false, deviceCount: 0, rxTotal: 0, txTotal: 0 },
  ])

  it.each([['админ', adminList], ['бот', botList]] as const)('%s', async (_who, list) => {
    rows()
    const got = json(await list(fakeEvent({}) as never))
    expect(got).toEqual(expected)
    expect(got[0]).not.toHaveProperty('password')
    expect(got[0]).not.toHaveProperty('tgChatId')
  })
})

describe('карточка клиента у админа и у бота', () => {
  it('админ: клиент, операция и девайсы с суммой трафика, без пароля', async () => {
    dbResults.push([anna], devices)
    expect(json(await adminCard(fakeEvent({ id: '7' })))).toEqual(json({
      ...fields(anna), operation, status: 'active', tgLinked: true, rxTotal: 105, txTotal: 206,
      devices: [
        { id: 12, name: 'laptop', configName: deviceConfigName('Anna', 'laptop', 12), hasWg: true, hasOvpn: false, createdAt: created, rxTotal: 100, txTotal: 200 },
        { id: 13, name: 'phone', configName: deviceConfigName('Anna', 'phone', 13), hasWg: false, hasOvpn: true, createdAt: created, rxTotal: 5, txTotal: 6 },
      ],
    }))
  })

  it('бот: число девайсов и пароль для приглашения', async () => {
    dbResults.push([anna], devices)
    expect(json(await botCard(fakeEvent({ id: '7' })))).toEqual(json({
      ...fields(anna), operation, accessStatus: 'Включение выполняется', status: 'active', deviceCount: 2, tgLinked: true, password: 'secret',
    }))
  })

  it('бот: без операции и без девайсов', async () => {
    access.latest = null
    dbResults.push([boris], [])
    expect(await botCard(fakeEvent({ id: '8' }))).toMatchObject({ operation: null, accessStatus: 'Состояние не подтверждено', deviceCount: 0 })
  })

  it.each([['админ', adminCard, auth.admin], ['бот', botCard, auth.bot]] as const)('%s: отказ авторизации раньше базы, не число - 400, нет клиента - 404', async (_who, card, gate) => {
    gate.mockImplementationOnce(() => { throw stubError({ statusCode: 401 }) })
    expect((await rejection(open(card, '7'))).statusCode).toBe(401)
    expect(calls).toEqual(['auth'])
    expect(await rejection(open(card, 'x'))).toMatchObject({ statusCode: 400, statusMessage: 'invalid_id' })
    expect(calls.filter(c => c.startsWith('db:'))).toEqual([])
    expect(await rejection(open(card, '9'))).toMatchObject({ statusCode: 404, statusMessage: 'not_found' })
  })
})
