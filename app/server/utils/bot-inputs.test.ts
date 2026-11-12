import { beforeEach, describe, expect, it, vi } from 'vitest'

// Маршруты бота проверяют вход так же, как маршруты админки.
const calls = vi.hoisted(() => ({
  createClient: vi.fn(async (body: unknown) => ({ created: body })),
  updateClient: vi.fn(async (_id: number, body: unknown) => ({ updated: body })),
  createDevice: vi.fn(async (..._args: unknown[]) => ({ device: { id: 7 } })),
  enqueueAccess: vi.fn(async (..._args: unknown[]) => ({ operation: { id: 1 } })),
  syncPrefs: vi.fn(async () => {}),
  logged: vi.fn(),
}))

vi.mock('./bot-api', () => ({
  requireBotAuth: () => {},
  clientByChat: async () => ({ id: 5 }),
  botClientView: async (id: number) => ({ id }),
}))
vi.mock('./client-ops', async importOriginal => ({
  ...await importOriginal<typeof import('./client-ops')>(), createClient: calls.createClient, updateClient: calls.updateClient,
}))
vi.mock('./bot', async importOriginal => ({ ...await importOriginal<object>(), BOT_ACTOR: 'bot' }))
vi.mock('./access-operations', () => ({ accessRequestKey: () => 'key', enqueueAccess: calls.enqueueAccess }))
vi.mock('./create-device', () => ({
  createDeviceWithAddresses: calls.createDevice,
  deviceRequestKey: () => 'key',
  rejectDeletedDeviceRepeat: async () => {},
  provisionDeviceKeys: async () => {},
}))
vi.mock('./openvpn', () => ({ caReady: async () => true }))
vi.mock('./client-prefs', () => ({ syncClientPrefsFile: calls.syncPrefs }))

type Event = { body?: unknown, params?: Record<string, string>, query?: Record<string, string>, status?: number }
type Handler = (event: Event) => Promise<unknown>

vi.stubGlobal('defineEventHandler', (handler: Handler) => handler)
vi.stubGlobal('createError', (fields: { statusMessage?: string, message?: string }) =>
  Object.assign(new Error(fields.message ?? fields.statusMessage), fields))
vi.stubGlobal('isError', () => false)
vi.stubGlobal('readBody', async (event: Event) => event.body)
vi.stubGlobal('getRouterParam', (event: Event, name: string) => event.params?.[name])
vi.stubGlobal('getQuery', (event: Event) => event.query ?? {})
vi.stubGlobal('setResponseStatus', (event: Event, status: number) => { event.status = status })
vi.stubGlobal('useRuntimeConfig', () => ({ wgEnabled: true, ovpnEnabled: true }))
vi.stubGlobal('useLogger', () => ({ error: calls.logged }))

const table = async (name: 'clients' | 'devices', key: string) => {
  const module = name === 'clients' ? await import('../resources/clients') : await import('../resources/devices')
  return (module.default as unknown as Record<string, Handler>)[key]!
}
const createClientRoute = () => table('clients', 'POST /bot/admin/clients')
const patchClientRoute = () => table('clients', 'PATCH /bot/admin/clients/:id')
const createDeviceRoute = () => table('devices', 'POST /bot/devices')
const deleteDeviceRoute = () => table('devices', 'DELETE /bot/devices/:deviceId')

async function failure(run: Promise<unknown>) {
  const error = await run.then(() => null, (e: unknown) => e as { statusCode: number, statusMessage: string, message: string })
  return error && { statusCode: error.statusCode, statusMessage: error.statusMessage, message: error.message }
}

beforeEach(() => {
  for (const mock of Object.values(calls)) mock.mockClear()
})

describe('имя из пробелов в маршрутах бота', () => {
  const emptyName = { statusCode: 400, statusMessage: 'invalid_body', message: 'Проверьте поле «Имя»' }

  it('создание клиента отвечает как на пустое имя', async () => {
    const handler = await createClientRoute()
    expect(await failure(handler({ body: { name: '' } }))).toEqual(emptyName)
    expect(await failure(handler({ body: { name: '   ' } }))).toEqual(emptyName)
    expect(calls.createClient).not.toHaveBeenCalled()
    await handler({ body: { name: '  Иван  ' } })
    expect(calls.createClient).toHaveBeenCalledWith({ name: 'Иван' }, 'bot')
  })

  it('переименование клиента отвечает как на пустое имя', async () => {
    const handler = await patchClientRoute()
    expect(await failure(handler({ params: { id: '3' }, body: { name: '' } }))).toEqual(emptyName)
    expect(await failure(handler({ params: { id: '3' }, body: { name: ' \t ' } }))).toEqual(emptyName)
    expect(calls.updateClient).not.toHaveBeenCalled()
    await handler({ params: { id: '3' }, body: { name: ' Анна ' } })
    expect(calls.updateClient).toHaveBeenCalledWith(3, { name: 'Анна' }, 'bot', 'key')
  })

  it('создание устройства отвечает как на пустое имя', async () => {
    const handler = await createDeviceRoute()
    expect(await failure(handler({ body: { chatId: 200, name: '' } }))).toEqual(emptyName)
    expect(await failure(handler({ body: { chatId: 200, name: '   ' } }))).toEqual(emptyName)
    expect(calls.createDevice).not.toHaveBeenCalled()
  })
})

describe('устройство из бота', () => {
  it('после создания обновляет файл предпочтений, как панель', async () => {
    const handler = await createDeviceRoute()
    expect(await handler({ body: { chatId: 200, name: ' Телефон ' } })).toEqual({ id: 5 })
    expect(calls.createDevice.mock.calls[0]!.slice(0, 2)).toEqual([5, 'Телефон'])
    expect(calls.syncPrefs).toHaveBeenCalledTimes(1)
  })

  it('сбой файла предпочтений не отменяет созданное устройство', async () => {
    calls.syncPrefs.mockRejectedValueOnce(new Error('control is down'))
    const handler = await createDeviceRoute()
    expect(await handler({ body: { chatId: 200, name: 'Телефон' } })).toEqual({ id: 5 })
    expect(calls.logged).toHaveBeenCalledTimes(1)
  })

  it('удаление не принимает дробный и неположительный id', async () => {
    const handler = await deleteDeviceRoute()
    const invalid = { statusCode: 400, statusMessage: 'invalid_params', message: 'invalid_params' }
    for (const deviceId of ['1.5', '0', '-1', 'abc', '1e400']) {
      expect(await failure(handler({ params: { deviceId }, query: { chatId: '200' } })), deviceId).toEqual(invalid)
    }
    expect(await failure(handler({ params: { deviceId: '7' }, query: { chatId: '200.5' } }))).toEqual(invalid)
    expect(calls.enqueueAccess).not.toHaveBeenCalled()
  })

  it('удаление по целому id ставит операцию в очередь', async () => {
    const event: Event = { params: { deviceId: '7' }, query: { chatId: '-100200' } }
    expect(await (await deleteDeviceRoute())(event)).toEqual({ id: 5, operation: { id: 1 } })
    expect(event.status).toBe(202)
    expect(calls.enqueueAccess.mock.calls[0]![0]).toMatchObject({ clientId: 5, chatId: -100200, deviceId: 7, kind: 'delete' })
  })
})
