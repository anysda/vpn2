import { EventEmitter } from 'node:events'
import { expect, test } from '@playwright/test'
import { FakeClock, serverModule } from './support/server-module'

function harness() {
  const clock = new FakeClock()
  const process = new EventEmitter()
  const calls: string[] = []
  const imports: Record<string, unknown> = {}
  for (const [module, method] of [
    ['wireguard', 'syncWireguardConfig'],
    ['openvpn', 'syncOpenvpnConfig'],
    ['ikev2', 'syncIkev2'],
    ['client-prefs', 'syncClientPrefsFile'],
    ['traffic-collector', 'collectTraffic'],
  ]) {
    imports[`../utils/${module}`] = { [method!]: async () => { calls.push(method!) } }
  }
  const plugin = serverModule('server/plugins/cron.ts', imports, {
    defineNitroPlugin: (fn: unknown) => fn,
    useLogger: () => ({ error: () => {} }),
    setTimeout: clock.setTimeout,
    setInterval: clock.setInterval,
    clearTimeout: clock.clearTimeout,
    clearInterval: clock.clearInterval,
    process,
  }).default as (app: unknown) => void
  function start() {
    const closeHooks: Array<() => void | Promise<void>> = []
    plugin({ hooks: { hook: (name: string, fn: () => void | Promise<void>) => {
      expect(name).toBe('close')
      closeHooks.push(fn)
    } } })
    return async () => { for (const close of closeHooks) await close() }
  }
  return { clock, process, calls, start }
}

test('cron: закрытие до первого тика отменяет отложенный запуск и интервал', async () => {
  const h = harness()
  const close = h.start()
  await close()
  await h.clock.advance(180_000)
  expect(h.calls).toEqual([])
  expect(h.process.listenerCount('beforeExit')).toBe(0)
})

test('cron: пересоздание Nitro оставляет только один работающий планировщик', async () => {
  const h = harness()
  for (let i = 0; i < 3; i++) {
    const close = h.start()
    await h.clock.advance(5_000)
    await close()
  }
  const close = h.start()
  h.calls.length = 0
  await h.clock.advance(65_000)
  expect(h.calls.filter(call => call === 'collectTraffic')).toHaveLength(2)
  await close()
  h.calls.length = 0
  await h.clock.advance(120_000)
  expect(h.calls).toEqual([])
  expect(h.process.listenerCount('beforeExit')).toBe(0)
})
