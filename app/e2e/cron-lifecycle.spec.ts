import { EventEmitter } from 'node:events'
import { expect, test } from '@playwright/test'
import { FakeClock, serverModule } from './support/server-module'

function harness(collect?: () => Promise<void>) {
  const clock = new FakeClock()
  const process = new EventEmitter()
  const calls: string[] = []
  const imports: Record<string, unknown> = {}
  imports['../utils/access-operations'] = {
    enqueueExpiredAccess: async () => { calls.push('enqueueExpiredAccess') },
    runAccessOperations: async () => { calls.push('runAccessOperations') },
  }
  imports['../utils/expiry'] = {
    reportControlExpiry: async () => { calls.push('reportControlExpiry') },
    syncControlExpiry: async () => { calls.push('syncControlExpiry') },
  }
  for (const [module, method] of [
    ['client-prefs', 'syncClientPrefsFile'],
    ['traffic-collector', 'collectTraffic'],
  ]) {
    imports[`../utils/${module}`] = { [method!]: async () => {
      calls.push(method!)
      if (method === 'collectTraffic') await collect?.()
    } }
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

test('cron: медленный сбор не запускается повторно, закрытие дожидается записи', async () => {
  let finish!: () => void
  const held = new Promise<void>((resolve) => { finish = resolve })
  const h = harness(() => held)
  const close = h.start()
  await h.clock.advance(125_000)
  expect(h.calls.filter(call => call === 'collectTraffic')).toHaveLength(1)
  let closed = false
  const closing = close().then(() => { closed = true })
  await h.clock.flush()
  expect(closed).toBe(false)
  finish()
  await closing
  const completed = h.calls.length
  await h.clock.advance(120_000)
  expect(h.calls).toHaveLength(completed)
})

test('cron: после ошибки следующий интервал снова выполняет сбор', async () => {
  let runs = 0
  const h = harness(async () => {
    if (++runs === 1) throw new Error('temporary database failure')
  })
  const close = h.start()
  await h.clock.advance(65_000)
  expect(runs).toBe(2)
  await close()
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

test('исполнитель доступа ждёт готовности панели и завершает текущую попытку при закрытии', async () => {
  const clock = new FakeClock()
  let ready = false
  let calls = 0
  let finish!: () => void
  const held = new Promise<void>(resolve => { finish = resolve })
  let close!: () => Promise<void>
  const plugin = serverModule('server/plugins/access-operations.ts', {
    '../utils/access-operations': { runAccessOperations: async () => { calls++; await held } },
    '../utils/panel-ready': { isPanelReady: () => ready },
  }, {
    defineNitroPlugin: (fn: unknown) => fn,
    useLogger: () => ({ error: () => {} }),
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
  }).default as (app: unknown) => void
  plugin({ hooks: { hook: (_name: string, fn: () => Promise<void>) => { close = fn } } })
  await clock.advance(3000)
  expect(calls).toBe(0)
  ready = true
  await clock.advance(3000)
  expect(calls).toBe(1)
  let closed = false
  const closing = close().then(() => { closed = true })
  await clock.flush()
  expect(closed).toBe(false)
  finish()
  await closing
  await clock.advance(3000)
  expect(calls).toBe(1)
})
