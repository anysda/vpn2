import { test, expect } from '@playwright/test'
import * as outbounds from '../shared/outbounds'
import type { ClashProxy } from '../server/utils/clash-client'
import { serverModule } from './support/server-module'

const proxies: ClashProxy[] = [
  'hy2-se-direct', 'hy2-se-warp', 'hy2-us-2-direct', 'direct-ru',
  'hy2-se-mgmt', 'hy2-fr-mgmt', 'hy2-de-mgmt', 'hy2-se-dns',
  'foreign-best', 'lane-01', 'pin-hy2-se-direct', 'youtube-ru', 'youtube-auto',
  'block', 'dns-out', 'hy2--direct',
].map(name => ({ name, type: 'Hysteria2', history: [] }))
const visible = ['direct-ru', 'hy2-se-direct', 'hy2-us-2-direct', 'hy2-se-warp']

function clash() {
  return serverModule<typeof import('../server/utils/clash-client')>(
    new URL('../server/utils/clash-client.ts', import.meta.url), {
      './logger': { useLogger: () => ({ warn: () => {} }) },
      '../../shared/outbounds': outbounds,
    }, {
      useRuntimeConfig: () => ({}),
      $fetch: async () => ({ proxies: Object.fromEntries(proxies.map(p => [p.name, p])) }),
    },
  )
}

test('API маршрутов и бота показывают только пользовательские выходы, даже без RTT', async () => {
  const client = clash()
  const routes = serverModule<{ default: (event: unknown) => Promise<Array<{ name: string }>> }>(
    new URL('../server/api/routes/outbounds.get.ts', import.meta.url), {
      '../../utils/auth': { requireAuth: async () => {} },
      '../../utils/clash-client': client,
    }, { defineEventHandler: (fn: unknown) => fn },
  )
  const bot = serverModule<{ default: (event: unknown) => Promise<{ outbounds: Array<{ name: string }> }> }>(
    new URL('../server/api/ops/bot-snapshot.get.ts', import.meta.url), {
      '../../database/client': { useDb: () => ({ select: () => ({ from: () => ({ orderBy: async () => [] }) }) }) },
      '../../database/schema': { clients: {} },
      '../../utils/bot-api': { requireBotAuth: () => {} },
      '../../utils/client-status': { clientStatus: () => 'active' },
      '../../utils/clash-client': { ...client, fetchConnections: async () => null },
      '../../utils/vm-client': { nodeInstances: () => [], fetchNodeMetrics: async () => [] },
    }, { defineEventHandler: (fn: unknown) => fn },
  )
  expect.soft((await routes.default({})).map(p => p.name)).toEqual(visible)
  expect((await bot.default({})).outbounds.map(p => p.name)).toEqual(visible)
})

test('карточки узлов и парсер UI сохраняют тег с дефисом и исключают служебные выходы', () => {
  const control = serverModule<typeof import('../server/utils/exit-control')>(
    new URL('../server/utils/exit-control.ts', import.meta.url), { '../../shared/outbounds': outbounds },
  )
  const ui = serverModule<typeof import('../app/composables/useRoutes')>(
    new URL('../app/composables/useRoutes.ts', import.meta.url), { '#shared/outbounds': outbounds },
  )
  expect.soft(control.exitNodeOf('hy2-us-2-direct')).toBe('us-2')
  expect(ui.parseOutbound('hy2-us-2-direct')).toEqual({ tag: 'us-2', variant: 'direct' })
  for (const proxy of proxies.filter(p => !visible.includes(p.name))) {
    expect(control.exitNodeOf(proxy.name)).toBeNull()
    expect(ui.parseOutbound(proxy.name)).toBeNull()
  }
})
