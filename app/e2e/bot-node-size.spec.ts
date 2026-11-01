import { test, expect } from '@playwright/test'
import * as mskDay from '../server/utils/msk-day'
import * as outbounds from '../shared/outbounds'
import { serverModule } from './support/server-module'

const GIB = 1024 ** 3
const CORES = 'count by(instance)(node_cpu_seconds_total{mode="idle"})'
const MEM_TOTAL = 'node_memory_MemTotal_bytes'

test('бот получает число ядер и память ноды, без метрики - null', async () => {
  let sized = true
  const metrics = serverModule<typeof import('../server/utils/vm-client')>(
    new URL('../server/utils/vm-client.ts', import.meta.url), {
      './logger': { useLogger: () => ({ warn: () => {} }) },
      './msk-day': mskDay,
    }, {
      useRuntimeConfig: () => ({ vmUrl: 'http://metrics.invalid', mgmtIps: 'ru:192.0.2.1' }),
      $fetch: async (_url: string, options: { query: { query: string } }) => {
        const promql = options.query.query
        const cores = promql === CORES
        const size = cores || promql === MEM_TOTAL
        const value = cores ? '1' : promql === MEM_TOTAL ? String(2 * GIB) : '50'
        return {
          status: 'success',
          data: {
            resultType: 'vector',
            result: size && !sized ? [] : [{ metric: { instance: '192.0.2.1:9100' }, value: [0, value] }],
          },
        }
      },
    },
  )
  const handler = serverModule<{ default: (event: unknown) => Promise<{ nodes: Array<{ cores: number | null, memTotalBytes: number | null }> }> }>(
    new URL('../server/api/ops/bot-snapshot.get.ts', import.meta.url), {
      '../../database/client': { useDb: () => ({ select: () => ({ from: () => ({ orderBy: async () => [] }) }) }) },
      '../../database/schema': { clients: {} },
      '../../utils/bot-api': { requireBotAuth: () => {} },
      '../../utils/client-status': { clientStatus: () => 'active' },
      '../../utils/clash-client': {
        fetchProxies: async () => [], fetchConnections: async () => null,
        userVisibleOutbounds: (proxies: Array<{ name: string }>) => proxies.filter(p => outbounds.parseOutbound(p.name)),
      },
      '../../utils/vm-client': metrics,
    }, { defineEventHandler: (fn: unknown) => fn },
  )
  const node = (await handler.default({})).nodes[0]!
  expect(node.cores).toBe(1)
  expect(node.memTotalBytes).toBe(2 * GIB)
  sized = false
  const unsized = (await handler.default({})).nodes[0]!
  expect(unsized.cores).toBeNull()
  expect(unsized.memTotalBytes).toBeNull()
})
