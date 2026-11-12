import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { calls, dbOps, dbResults, dbRows, installStubs, rejection } from '../test/stubs'
import { accessRequestKey, enqueueAccess, getAccess, replayAccess, retryAccess, runAccessOperations } from './access-operations'
import { accessAdapter } from './access-adapter'
import { botConfigured } from './bot'

vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('./access-adapter', () => ({ accessAdapter: { enabled: vi.fn() } }))
vi.mock('./access-lock', () => ({ withAccessLock: (action: () => Promise<void>) => action() }))
vi.mock('./bot', () => ({ botConfigured: vi.fn() }))
vi.mock('./control', () => ({ ControlUnavailable: class extends Error { constructor() { super('Нет связи с сервисом') } } }))

const allOn = { wg: true, openvpn: true, ikev2: true }
const client = (over: Record<string, unknown> = {}) => ({
  id: 7, name: 'Anna', accessVersion: 3, deleting: false, frozenManual: false, expiresAt: null, tgChatId: 42, ...over,
})
const device = (id: number, over: Record<string, unknown> = {}) => ({
  id, clientId: 7, deleting: false, wgPublicKey: `test-key-wg-${id}`, ovpnCert: `test-cert-${id}`, ...over,
})
const inserted = (over: Record<string, unknown> = {}) => ({
  id: 'op-1', clientId: 7, deviceId: null, kind: 'freeze', version: 4, status: 'pending', protocols: {}, nextRetry: 0, createdAt: 0, ...over,
})
const sets = () => dbOps.filter(o => o.op === 'set').map(o => o.args[0] as Record<string, unknown>)
const values = () => dbOps.find(o => o.op === 'values')!.args[0] as Record<string, unknown>
const payloadOf = () => JSON.parse(values().payload as string)

/** Очередь dbResults для enqueueAccess: нет ключа, клиент, устройства, обновление клиента, прошлая операция. */
function queue(opts: { before?: unknown, devices?: unknown[], updated?: unknown, previous?: unknown, existing?: unknown } = {}) {
  dbResults.push(
    opts.existing ? [opts.existing] : [],
    opts.before === null ? [] : [opts.before ?? client()],
    opts.devices ?? [],
    opts.updated === null ? [] : [opts.updated ?? client({ accessVersion: 4, frozenManual: true })],
    opts.previous ? [opts.previous] : [],
  )
}

beforeEach(() => {
  installStubs()
  vi.mocked(accessAdapter.enabled).mockReset().mockResolvedValue(allOn)
  vi.mocked(botConfigured).mockReset().mockReturnValue(true)
  vi.stubGlobal('getHeader', (e: { headers: Record<string, string> }, name: string) => e.headers[name])
})

describe('accessRequestKey', () => {
  const event = (headers: Record<string, string>) => ({ headers }) as never

  it('допустимый ключ возвращается как есть', () => {
    expect(accessRequestKey(event({ 'idempotency-key': 'req:1.a-b_c' }))).toBe('req:1.a-b_c')
    expect(accessRequestKey(event({ 'idempotency-key': 'k'.repeat(128) }))).toHaveLength(128)
  })

  it('без заголовка выдаётся новый UUID', () => {
    expect(accessRequestKey(event({}))).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  })

  it.each(['bad key', 'k'.repeat(129), 'a/b', ''])('ключ "%s" - 400', (key) => {
    let err: { statusCode?: number, message?: string } = {}
    try { accessRequestKey(event({ 'idempotency-key': key })) }
    catch (e) { err = e as typeof err }
    expect([err.statusCode, err.message]).toEqual([400, 'Неверный ключ идемпотентности'])
  })
})

describe('getAccess', () => {
  it('нет операции - 404 not_found', async () => {
    dbResults.push([])
    const err = await rejection(getAccess('nope'))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
  })

  it('отдаёт только публичные поля операции, без payload и ключа', async () => {
    dbResults.push([{ ...inserted(), payload: '{}', idempotencyKey: 'k', requestHash: 'h', actor: 'a' }])
    expect(Object.keys(await getAccess('op-1')).sort()).toEqual(
      ['clientId', 'createdAt', 'deviceId', 'id', 'kind', 'nextRetry', 'protocols', 'status', 'version'])
  })
})

/** Хвост очереди: обновления устройств, вытеснение прошлых операций, вставка новой. */
function tail(deviceUpdates = 0) {
  for (let i = 0; i < deviceUpdates; i++) dbResults.push([])
  dbResults.push([], [inserted()])
}

describe('enqueueAccess: отказы', () => {
  it('чат бота не привязан к клиенту - 404 not_linked до любых чтений', async () => {
    dbResults.push([])
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'bot', chatId: 42 }))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_linked'])
    expect(calls).toEqual(['db:select'])
  })

  it('клиента нет - 404 not_found', async () => {
    queue({ before: null })
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'admin' }))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
  })

  it('клиент удаляется - 409, кроме самой операции delete', async () => {
    queue({ before: client({ deleting: true }) })
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'admin', kind: 'freeze' }))
    expect([err.statusCode, err.message]).toEqual([409, 'Клиент удаляется'])
    dbResults.length = 0
    queue({ before: client({ deleting: true }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', kind: 'delete' })
    expect(calls).toContain('db:insert')
  })

  it('устройство клиента не найдено - 404, клиент не обновляется', async () => {
    queue({ devices: [device(1)] })
    const err = await rejection(enqueueAccess({ clientId: 7, deviceId: 99, actor: 'admin', kind: 'delete' }))
    expect([err.statusCode, err.statusMessage]).toEqual([404, 'not_found'])
    expect(calls).not.toContain('db:update')
  })

  it('перевыпуск при удаляемом устройстве - 409', async () => {
    queue({ devices: [device(1, { deleting: true })] })
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'admin', kind: 'reissue' }))
    expect([err.statusCode, err.message]).toEqual([409, 'Устройство удаляется'])
  })

  it('условие записи не сработало - 409 про параллельный запрос, операция не создаётся', async () => {
    queue({ updated: null })
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'admin' }))
    expect([err.statusCode, err.message]).toEqual([409, 'Клиент изменён параллельным запросом; повторите изменение'])
    expect(calls).not.toContain('db:insert')
  })

  it('тот же ключ с другим запросом - 409; с тем же - прежняя операция без записей', async () => {
    queue()
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', key: 'k1', kind: 'freeze' })
    const hash = values().requestHash
    dbOps.length = 0
    dbResults.length = 0
    calls.length = 0
    dbResults.push([{ ...inserted(), requestHash: 'other-hash' }])
    const err = await rejection(enqueueAccess({ clientId: 7, actor: 'admin', key: 'k1', kind: 'freeze' }))
    expect([err.statusCode, err.message]).toEqual([409, 'Ключ идемпотентности уже использован для другого запроса'])
    dbResults.push([{ ...inserted({ id: 'op-9' }), requestHash: hash }], [client()])
    const again = await enqueueAccess({ clientId: 7, actor: 'admin', key: 'k1', kind: 'freeze' })
    expect(again.operation.id).toBe('op-9')
    expect(calls).not.toContain('db:insert')
    expect(calls.filter(c => c === 'db:update')).toHaveLength(0)
  })
})

describe('enqueueAccess: версия, вид и содержимое операции', () => {
  it('версия растёт на 1, патч получает accessVersion; с initialize версия прежняя', async () => {
    queue()
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', patch: { frozenManual: true } })
    expect(sets()[0]).toMatchObject({ frozenManual: true, accessVersion: 4 })
    expect(values()).toMatchObject({ version: 4, status: 'pending', actor: 'admin', clientId: 7, deviceId: null, completionRecorded: false })
    dbOps.length = 0
    queue()
    tail()
    await enqueueAccess({ clientId: 7, actor: 'reconcile', initialize: true })
    expect(sets()[0]).toMatchObject({ accessVersion: 3 })
    expect(values()).toMatchObject({ version: 3, completionRecorded: true })
    expect(payloadOf().chatId).toBeNull()
  })

  it.each([
    ['активный клиент - unfreeze', client({ accessVersion: 4 }), 'unfreeze', true],
    ['замороженный - freeze', client({ accessVersion: 4, frozenManual: true }), 'freeze', false],
    ['с истёкшим сроком - freeze', client({ accessVersion: 4, expiresAt: new Date(Date.now() - 1000) }), 'freeze', false],
  ])('вид без kind: %s', async (_name, updated, kind, enabled) => {
    queue({ updated })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    expect(values().kind).toBe(kind)
    expect(payloadOf()).toMatchObject({ enabled, name: 'Anna', chatId: 42 })
  })

  it('delete клиента целиком: deleting и frozenManual в патче, устройства помечены, реквизиты в retired', async () => {
    queue({ devices: [device(1), device(2)] })
    tail(2)
    await enqueueAccess({ clientId: 7, actor: 'admin', kind: 'delete' })
    expect(sets()[0]).toMatchObject({ deleting: true, frozenManual: true })
    expect(sets().filter(s => Object.keys(s).join() === 'deleting')).toHaveLength(2)
    expect(payloadOf().retired).toEqual([
      { id: 1, wgPublicKey: 'test-key-wg-1', ovpnCert: 'test-cert-1' },
      { id: 2, wgPublicKey: 'test-key-wg-2', ovpnCert: 'test-cert-2' },
    ])
    expect(payloadOf().rotateOvpn).toEqual([])
  })

  it('delete одного устройства не помечает клиента и чужие устройства', async () => {
    queue({ devices: [device(1), device(2)] })
    tail(1)
    await enqueueAccess({ clientId: 7, deviceId: 2, actor: 'admin', kind: 'delete' })
    expect(sets()[0]).not.toHaveProperty('deleting')
    expect(sets()[1]).toEqual({ deleting: true })
    expect(payloadOf().retired.map((r: { id: number }) => r.id)).toEqual([2])
    expect(values().deviceId).toBe(2)
  })

  it('reissue: новые ключи WG и пароль IKEv2, rotateOvpn с прежним сертификатом, retired со старым ключом', async () => {
    queue({ devices: [device(1)] })
    tail(1)
    await enqueueAccess({ clientId: 7, actor: 'admin', kind: 'reissue' })
    const set = sets()[1]!
    for (const key of ['wgPrivateKey', 'wgPublicKey', 'wgPresharedKey']) expect(set[key]).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    expect(set.wgPublicKey).not.toBe('test-key-wg-1')
    expect(set.ikev2Password).toMatch(/^[A-Za-z]{20}$/)
    expect(payloadOf().rotateOvpn).toEqual([{ id: 1, cert: 'test-cert-1' }])
    expect(payloadOf().retired).toEqual([{ id: 1, wgPublicKey: 'test-key-wg-1', ovpnCert: 'test-cert-1' }])
  })

  it('reissue при выключенных протоколах: ключи и пароль не трогаются, rotateOvpn пуст, статусы not_required', async () => {
    vi.mocked(accessAdapter.enabled).mockResolvedValue({ wg: false, openvpn: false, ikev2: true })
    queue({ devices: [device(1)] })
    tail(1)
    await enqueueAccess({ clientId: 7, actor: 'admin', kind: 'reissue' })
    expect(Object.keys(sets()[1]!).sort()).toEqual(['ikev2Password', 'updatedAt'])
    expect(payloadOf().rotateOvpn).toEqual([])
    const protocols = values().protocols as Record<string, { state: string, nextRetry: number | null }>
    expect([protocols.wg!.state, protocols.openvpn!.state, protocols.ikev2!.state]).toEqual(['not_required', 'not_required', 'pending'])
    expect([protocols.wg!.nextRetry, protocols.ikev2!.nextRetry]).toEqual([null, values().nextRetry])
  })

  it('вытеснение прошлых операций идёт после правки клиента и до вставки новой', async () => {
    queue()
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    const updates = calls.map((c, i) => [c, i] as const).filter(([c]) => c === 'db:update').map(([, i]) => i)
    expect(updates).toHaveLength(2)
    expect(updates[1]).toBeLessThan(calls.indexOf('db:insert'))
    expect(sets()[1]).toMatchObject({ status: 'superseded' })
  })

  it('срок изменился - в payload уведомление; снят - другое; не менялся - без уведомления', async () => {
    const until = new Date('2031-05-06T12:00:00.000Z')
    queue({ updated: client({ accessVersion: 4, expiresAt: until }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', patch: { expiresAt: until } })
    expect(payloadOf().notice).toBe('Срок действия вашего ключа: до 06.05.2031.')
    dbOps.length = 0
    queue({ before: client({ expiresAt: until }), updated: client({ accessVersion: 4, expiresAt: null }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', patch: { expiresAt: null } })
    expect(payloadOf().notice).toBe('Срок действия ключа снят — доступ бессрочный.')
    dbOps.length = 0
    queue({ before: client({ expiresAt: until }), updated: client({ accessVersion: 4, expiresAt: until }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', patch: { expiresAt: new Date(until) } })
    expect(payloadOf()).not.toHaveProperty('notice')
  })
})

describe('enqueueAccess: наследование неподтверждённой операции', () => {
  const state = (confirmedVersion: number | null) => ({ state: 'pending', confirmedVersion, checkedAt: null, lastAttempt: null, nextRetry: null, error: null })
  const previous = (over: Record<string, unknown>) => ({
    ...inserted({ version: 3 }),
    payload: JSON.stringify({
      enabled: true, name: 'Anna', chatId: 42,
      retired: [{ id: 1, wgPublicKey: 'test-key-wg-old', ovpnCert: 'test-cert-old' }, { id: 5, wgPublicKey: 'test-key-wg-5', ovpnCert: null }],
      rotateOvpn: [{ id: 1, cert: 'test-cert-old' }],
    }),
    protocols: { wg: state(null), openvpn: state(null), ikev2: state(null) },
    ...over,
  })

  it('pending: retired и rotateOvpn переходят в новую операцию целиком', async () => {
    queue({ previous: previous({}) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    expect(payloadOf().retired.map((r: { id: number }) => r.id)).toEqual([1, 5])
    expect(payloadOf().rotateOvpn).toEqual([{ id: 1, cert: 'test-cert-old' }])
  })

  it('подтверждённый протокол свои реквизиты не передаёт; пустые записи отбрасываются', async () => {
    queue({ previous: previous({ protocols: { wg: state(3), openvpn: state(null), ikev2: state(null) } }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    expect(payloadOf().retired).toEqual([{ id: 1, wgPublicKey: null, ovpnCert: 'test-cert-old' }])
    expect(payloadOf().rotateOvpn).toEqual([{ id: 1, cert: 'test-cert-old' }])
  })

  it('openvpn подтверждён - rotateOvpn пуст, сертификат из retired снят', async () => {
    queue({ previous: previous({ protocols: { wg: state(null), openvpn: state(3), ikev2: state(null) } }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    expect(payloadOf().rotateOvpn).toEqual([])
    expect(payloadOf().retired).toEqual([{ id: 1, wgPublicKey: 'test-key-wg-old', ovpnCert: null }, { id: 5, wgPublicKey: 'test-key-wg-5', ovpnCert: null }])
  })

  it('прошлая операция confirmed - ничего не наследуется', async () => {
    queue({ previous: previous({ status: 'confirmed' }) })
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin' })
    expect([payloadOf().retired, payloadOf().rotateOvpn]).toEqual([[], []])
  })

  it('повторный reissue того же устройства оставляет самые ранние реквизиты цепочки', async () => {
    queue({ devices: [device(1, { wgPublicKey: 'test-key-wg-mid', ovpnCert: 'test-cert-mid' })], previous: previous({}) })
    tail(1)
    await enqueueAccess({ clientId: 7, actor: 'admin', kind: 'reissue' })
    expect(payloadOf().retired.find((r: { id: number }) => r.id === 1)).toEqual({ id: 1, wgPublicKey: 'test-key-wg-old', ovpnCert: 'test-cert-old' })
    expect(payloadOf().rotateOvpn).toEqual([{ id: 1, cert: 'test-cert-mid' }])
  })
})

describe('replayAccess', () => {
  it('ключа нет - null', async () => {
    dbResults.push([])
    expect(await replayAccess('k', { clientId: 7 })).toBeNull()
  })

  it('ключ есть, запрос другой - 409; тот же - операция и id клиента', async () => {
    queue()
    tail()
    await enqueueAccess({ clientId: 7, actor: 'admin', key: 'k', kind: 'freeze', patch: { frozenManual: true, updatedAt: new Date() } })
    const hash = values().requestHash
    dbResults.length = 0
    dbResults.push([{ ...inserted(), requestHash: hash }])
    expect(await replayAccess('k', { clientId: 7, kind: 'freeze', patch: { updatedAt: new Date(1), frozenManual: true } })).toMatchObject({ id: 7, operation: { id: 'op-1' } })
    dbResults.push([{ ...inserted(), requestHash: hash }])
    expect((await rejection(replayAccess('k', { clientId: 7, kind: 'unfreeze' }))).statusCode).toBe(409)
  })
})

describe('retryAccess', () => {
  const NOW = 1_000_000
  const proto = (over: Record<string, unknown> = {}) => ({ state: 'failed', confirmedVersion: null, checkedAt: null, lastAttempt: null, nextRetry: 2_000_000, error: 'x', ...over })
  const op = (over: Record<string, unknown> = {}) => ({
    ...inserted(), nextRetry: 2_000_000,
    protocols: { wg: proto(), openvpn: proto({ state: 'not_required', nextRetry: null }), ikev2: proto() }, ...over,
  })
  beforeEach(() => { vi.useFakeTimers({ now: NOW }) })
  afterEach(() => { vi.useRealTimers() })

  it('операции нет - 404', async () => {
    dbResults.push([])
    expect((await rejection(retryAccess('nope'))).statusMessage).toBe('not_found')
  })

  it.each([
    ['вытесненная', op({ status: 'superseded' })],
    ['подтверждённое удаление клиента', op({ status: 'confirmed', kind: 'delete', deviceId: null })],
  ])('%s возвращается без записи', async (_name, row) => {
    dbResults.push([row])
    expect((await retryAccess('op-1')).id).toBe('op-1')
    expect(calls).not.toContain('db:update')
  })

  it('повтор уже назначен раньше, чем можно - без записи', async () => {
    dbResults.push([op({ nextRetry: NOW })])
    await retryAccess('op-1')
    expect(calls).not.toContain('db:update')
  })

  it('повтор назначается на сейчас, not_required не трогается', async () => {
    dbResults.push([op()], [op({ nextRetry: NOW, status: 'pending' })])
    await retryAccess('op-1')
    const set = sets()[0] as { status: string, nextRetry: number, protocols: Record<'wg' | 'openvpn' | 'ikev2', { nextRetry: number | null }> }
    expect([set.status, set.nextRetry]).toEqual(['pending', NOW])
    expect([set.protocols.wg.nextRetry, set.protocols.openvpn.nextRetry, set.protocols.ikev2.nextRetry]).toEqual([NOW, null, NOW])
  })

  it('недавняя попытка отодвигает повтор на 5 секунд от неё', async () => {
    const recent = op({ protocols: { wg: proto({ lastAttempt: NOW - 1000 }), openvpn: proto({ lastAttempt: NOW - 2000 }), ikev2: proto() } })
    dbResults.push([recent], [recent])
    await retryAccess('op-1')
    expect((sets()[0] as { nextRetry: number }).nextRetry).toBe(NOW - 1000 + 5000)
  })
})

describe('runAccessOperations', () => {
  const NOW = 5_000_000
  const idle = { state: 'not_required', confirmedVersion: null, checkedAt: null, lastAttempt: null, nextRetry: null, error: null }
  const pending = { ...idle, state: 'pending', nextRetry: 0 }
  const payload = (over: Record<string, unknown> = {}) => JSON.stringify({ enabled: true, retired: [], rotateOvpn: [], name: 'Anna', chatId: 42, ...over })
  const row = (over: Record<string, unknown> = {}) => ({
    ...inserted(), kind: 'unfreeze', version: 4, attempts: 0, completionRecorded: false, actor: 'admin', payload: payload(),
    protocols: { wg: pending, openvpn: idle, ikev2: idle }, nextRetry: 0, createdAt: 1, ...over,
  })
  const adapter = {
    enabled: vi.fn(),
    read: vi.fn<(p: string) => Promise<boolean>>(),
    apply: vi.fn<(p: string) => Promise<void>>(),
  }
  const run = () => runAccessOperations(adapter as never)
  const final = () => sets().find(s => 'status' in s) as { status: string, completionRecorded: boolean, nextRetry: number, protocols: Record<string, { state: string, confirmedVersion: number | null, error: string | null }> } | undefined
  const inserts = () => dbOps.filter(o => o.op === 'values').map(o => o.args[0] as Record<string, unknown>)

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW })
    dbRows.clients = [client({ accessVersion: 4 })]
    adapter.read.mockReset().mockImplementation(async (p) => { calls.push(`read:${p}`); return true })
    adapter.apply.mockReset().mockImplementation(async (p) => { calls.push(`apply:${p}`) })
  })
  afterEach(() => { vi.useRealTimers() })

  it('уже сходится - apply не зовётся, подтверждение, событие и уведомление клиенту в одной транзакции', async () => {
    dbResults.push([row()])
    await run()
    expect(adapter.apply).not.toHaveBeenCalled()
    expect(final()).toMatchObject({ status: 'confirmed', completionRecorded: true })
    expect(final()!.protocols.wg).toMatchObject({ state: 'confirmed', confirmedVersion: 4, error: null })
    expect(inserts()[0]).toMatchObject({ type: 'access_confirmed', key: 'access:op-1', object: 'client:7', actor: 'admin', title: 'Доступ включён · Anna' })
    expect(inserts()[1]).toMatchObject({ id: 'op-1', status: 'queued', label: 'Уведомление клиенту «Anna»' })
    expect(JSON.parse(inserts()[1]!.payload as string)).toEqual({ type: 'client_notify', chatId: 42, text: 'Доступ включён' })
    expect(calls.filter(c => c === 'db:delete')).toHaveLength(1)
  })

  it('не сходится - порядок read, apply, read; подтверждение после повторного чтения', async () => {
    adapter.read.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    dbResults.push([row()])
    await run()
    expect(adapter.read).toHaveBeenCalledTimes(2)
    expect(adapter.apply).toHaveBeenCalledOnce()
    expect(adapter.apply.mock.invocationCallOrder[0]).toBeGreaterThan(adapter.read.mock.invocationCallOrder[0]!)
    expect(adapter.apply.mock.invocationCallOrder[0]).toBeLessThan(adapter.read.mock.invocationCallOrder[1]!)
    expect(final()!.status).toBe('confirmed')
  })

  it('попытка записана до первого обращения к адаптеру; задержка повтора растёт с номером попытки', async () => {
    adapter.read.mockImplementation(async () => { calls.push('adapter'); return true })
    dbResults.push([row({ attempts: 1 })])
    await run()
    expect(calls.indexOf('db:update')).toBeLessThan(calls.indexOf('adapter'))
    expect(sets()[0]).toMatchObject({ attempts: 2, nextRetry: NOW + 15_000 })
  })

  it.each([[0, 5_000], [1, 15_000], [2, 30_000], [3, 60_000], [4, 300_000], [9, 300_000]])('попытка %i - следующий повтор через %i мс', async (attempts, delay) => {
    adapter.read.mockResolvedValue(false)
    adapter.apply.mockRejectedValue(new Error('boom'))
    dbResults.push([row({ attempts })])
    await run()
    expect(sets()[0]).toMatchObject({ attempts: attempts + 1, nextRetry: NOW + delay })
    expect(final()!.nextRetry).toBe(NOW + delay)
  })

  it('apply упал при читаемом состоянии - failed с общим текстом, операция остаётся pending, события нет', async () => {
    adapter.read.mockResolvedValue(false)
    adapter.apply.mockRejectedValue(new Error('секрет в stdout'))
    dbResults.push([row()])
    await run()
    expect(final()!.status).toBe('pending')
    expect(final()!.protocols.wg).toMatchObject({ state: 'failed', confirmedVersion: null, error: 'Не удалось применить изменение' })
    expect(inserts()).toEqual([])
  })

  it('чтение упало и apply упал - unknown, «Состояние не подтверждено»', async () => {
    adapter.read.mockRejectedValue(new Error('x'))
    adapter.apply.mockRejectedValue(new Error('y'))
    dbResults.push([row()])
    await run()
    expect(final()!.protocols.wg).toMatchObject({ state: 'unknown', error: 'Состояние не подтверждено' })
  })

  it('чтение упало, apply прошёл, повторное чтение не сошлось - failed «не подтверждено VPN-службой»', async () => {
    adapter.read.mockRejectedValueOnce(new Error('x')).mockResolvedValueOnce(false)
    dbResults.push([row()])
    await run()
    expect(final()!.protocols.wg).toMatchObject({ state: 'failed', error: 'Изменение не подтверждено VPN-службой' })
  })

  it('повторное чтение упало - unknown без текста ошибки сервиса', async () => {
    adapter.read.mockResolvedValueOnce(false).mockRejectedValueOnce(new Error('путь /secret'))
    dbResults.push([row()])
    await run()
    expect(final()!.protocols.wg).toMatchObject({ state: 'unknown', error: 'Состояние не подтверждено' })
  })

  it('ControlUnavailable доносит собственный текст, чужие тексты скрыты', async () => {
    const { ControlUnavailable } = await import('./control')
    adapter.read.mockResolvedValue(false)
    adapter.apply.mockRejectedValue(new ControlUnavailable())
    dbResults.push([row()])
    await run()
    expect(final()!.protocols.wg?.error).toBe('Нет связи с сервисом')
  })

  it('операция уже записала событие (completionRecorded) - подтверждение без события и уведомления', async () => {
    dbResults.push([row({ completionRecorded: true })])
    await run()
    expect(final()!.status).toBe('confirmed')
    expect(inserts()).toEqual([])
  })

  it('бот не настроен или нет chatId - события есть, уведомления клиенту нет', async () => {
    vi.mocked(botConfigured).mockReturnValue(false)
    dbResults.push([row()])
    await run()
    expect(inserts().map(i => i.type ?? 'outbox')).toEqual(['access_confirmed'])
  })

  it('удаление клиента: подтверждённая операция сносит помеченные устройства и самого клиента', async () => {
    dbRows.clients = [client({ accessVersion: 4, deleting: true, frozenManual: true })]
    dbResults.push([row({ kind: 'delete', payload: payload({ enabled: false }) })])
    await run()
    expect(calls.filter(c => c === 'db:delete')).toHaveLength(2)
    expect(inserts()[0]!.title).toBe('Доступ отключён · Anna')
  })

  it('клиент сменил версию - операция пропущена: ни попытки, ни адаптера', async () => {
    dbRows.clients = [client({ accessVersion: 5 })]
    dbResults.push([row()])
    await run()
    expect(sets()).toEqual([])
    expect(adapter.read).not.toHaveBeenCalled()
  })

  it('payload.enabled разошёлся с клиентом - адаптер не зовётся, подтверждения нет', async () => {
    dbRows.clients = [client({ accessVersion: 4, frozenManual: true })]
    dbResults.push([row()])
    await run()
    expect(adapter.read).not.toHaveBeenCalled()
    expect(final()).toBeUndefined()
  })

  it('срок истёк, пока шли команды - операция не подтверждается и событий нет', async () => {
    adapter.read.mockImplementation(async () => { dbRows.clients![0]!.frozenManual = true; return true })
    dbResults.push([row()])
    await run()
    expect(final()).toBeUndefined()
    expect(inserts()).toEqual([])
  })

  it('операция вытеснена во время команды - apply не зовётся', async () => {
    adapter.read.mockImplementation(async () => { dbRows.clients![0]!.accessVersion = 5; return false })
    dbResults.push([row()])
    await run()
    expect(adapter.apply).not.toHaveBeenCalled()
  })

  it('not_required протоколы адаптеру не передаются; все требуемые подтверждены - confirmed', async () => {
    dbResults.push([row({ protocols: { wg: { ...pending }, openvpn: { ...pending }, ikev2: idle } })])
    await run()
    expect(adapter.read.mock.calls.map(c => c[0])).toEqual(['wg', 'openvpn'])
  })

  it('один из двух протоколов не сошёлся - операция остаётся pending, подтверждённый протокол зафиксирован', async () => {
    adapter.read.mockImplementation(async (p) => p === 'wg')
    adapter.apply.mockRejectedValue(new Error('x'))
    dbResults.push([row({ protocols: { wg: { ...pending }, openvpn: { ...pending }, ikev2: idle } })])
    await run()
    expect(final()!.status).toBe('pending')
    expect([final()!.protocols.wg?.state, final()!.protocols.openvpn?.state]).toEqual(['confirmed', 'failed'])
  })

  it('параллельный запуск отдаёт тот же промис', async () => {
    dbResults.push([])
    const first = run()
    expect(run()).toBe(first)
    await first
  })
})
