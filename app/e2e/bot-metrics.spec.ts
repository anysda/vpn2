import { test, expect } from '@playwright/test'
import * as mskDay from '../server/utils/msk-day'
import { serverModule } from './support/server-module'

test('бот получает отсутствие CPU/RAM, а не сохранённую нагрузку прошлого опроса', async () => {
  let missing = false
  const metrics = serverModule<typeof import('../server/utils/vm-client')>(
    new URL('../server/utils/vm-client.ts', import.meta.url), {
      './logger': { useLogger: () => ({ warn: () => {} }) },
      './msk-day': mskDay,
    }, {
      useRuntimeConfig: () => ({ vmUrl: 'http://metrics.invalid', mgmtIps: 'ru:192.0.2.1' }),
      $fetch: async () => ({
        status: 'success',
        data: { resultType: 'vector', result: missing ? [] : [{ metric: { instance: '192.0.2.1:9100' }, value: [0, '95'] }] },
      }),
    },
  )
  const handler = serverModule<{ default: (event: unknown) => Promise<{ nodes: Array<{ cpu: number | null, ram: number | null, staleSec: number | null }> }> }>(
    new URL('../server/api/ops/bot-snapshot.get.ts', import.meta.url), {
      '../../database/client': { useDb: () => ({ select: () => ({ from: () => ({ orderBy: async () => [] }) }) }) },
      '../../database/schema': { clients: {} },
      '../../utils/bot-api': { requireBotAuth: () => {} },
      '../../utils/client-status': { clientStatus: () => 'active' },
      '../../utils/clash-client': { fetchProxies: async () => [], fetchConnections: async () => null },
      '../../utils/vm-client': metrics,
    }, { defineEventHandler: (fn: unknown) => fn },
  )
  expect((await handler.default({})).nodes[0]!.cpu).toBe(95)
  // Кэш панели тоже заполнен; потеря ответа VM не должна превратить одно
  // наблюдение нагрузки в несколько подтверждений CPU/RAM-алерта.
  await metrics.fetchNodeMetrics(['192.0.2.1:9100'])
  missing = true
  const node = (await handler.default({})).nodes[0]!
  expect(node.staleSec).toBeNull()
  expect(node.cpu).toBeNull()
  expect(node.ram).toBeNull()
  // Отображение последней известной нагрузки в UI сохранено.
  expect((await metrics.fetchNodeMetrics(['192.0.2.1:9100']))[0]!.cpuPct).toBe(95)
})
