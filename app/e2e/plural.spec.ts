import { test, expect } from '@playwright/test'
import { serverModule } from './support/server-module'
import * as plural from '../shared/plural'

const cases = [
  [0, 'уведомлений', 'устройств'],
  [1, 'уведомление', 'устройство'],
  [2, 'уведомления', 'устройства'],
  [3, 'уведомления', 'устройства'],
  [4, 'уведомления', 'устройства'],
  [5, 'уведомлений', 'устройств'],
  [11, 'уведомлений', 'устройств'],
  [12, 'уведомлений', 'устройств'],
  [14, 'уведомлений', 'устройств'],
  [20, 'уведомлений', 'устройств'],
  [21, 'уведомление', 'устройство'],
  [22, 'уведомления', 'устройства'],
  [24, 'уведомления', 'устройства'],
  [25, 'уведомлений', 'устройств'],
  [101, 'уведомление', 'устройство'],
  [111, 'уведомлений', 'устройств'],
  [112, 'уведомлений', 'устройств'],
  [114, 'уведомлений', 'устройств'],
] as const

const { botQueueText } = serverModule<typeof import('../shared/bot-delivery')>(
  new URL('../shared/bot-delivery.ts', import.meta.url), { './plural': plural },
)

for (const [n, notification, device] of cases) {
  test(`очередь: ${n} ${notification}`, () => {
    const text = botQueueText(n)
    console.log(text)
    expect(text).toBe(`Бот недоступен, ${n} ${notification} в очереди`)
  })

  if (n === 0) continue
  test(`лимит клиента: ${n} ${device}`, async () => {
    const before = { id: 1 }
    const db = { select: (fields?: unknown) => ({ from: () => ({ where: () => fields
      ? Promise.resolve([{ n }])
      : { limit: async () => [before] } }) }) }
    const { updateClient } = serverModule<typeof import('../server/utils/client-ops')>(
      new URL('../server/utils/client-ops.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': { clients: { id: 'id' }, devices: { clientId: 'clientId' } },
        '../../shared/plural': plural,
        './bot-events': {},
        './expiry': {},
        './password': {},
        './access-operations': {},
        './wireguard': {},
        './openvpn': {},
        './ikev2': {},
        './client-prefs': {},
        './route-outbound': {},
        './events': {},
      }, { createError: (error: object) => Object.assign(new Error(), error) },
    )
    const error = await updateClient(1, { deviceLimit: n - 1 }, '@admin').catch(error => error)
    console.log(error.message)
    expect(error).toMatchObject({
      statusCode: 409,
      message: `У клиента уже ${n} ${device} — лимит не может быть меньше`,
    })
  })
}
