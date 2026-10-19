import { expect, test } from '@playwright/test'
import { serverModule } from './support/server-module'

// Настоящие PATCH /api/ops/exits/<tag> и toggleExit; подменён только сервис
// управления: чтение и запись отвечают с разной задержкой, как под нагрузкой.
test('одновременные переключения экзита: журнал повторяет фактические переходы', async () => {
  const disabled = new Set<string>()
  const applied: Array<{ before: boolean, after: boolean }> = []
  let call = 0
  const pause = () => new Promise(resolve => setTimeout(resolve, (call++ * 7) % 5))
  const control = async (_method: string, _params: unknown) => {
    await pause()
    return { config: { disabled: [...disabled] } }
  }
  const controlApply = async (_method: string, params: { config: { disabled: string[] } }) => {
    await pause()
    const before = disabled.has('se')
    disabled.clear()
    for (const tag of params.config.disabled) disabled.add(tag)
    applied.push({ before, after: disabled.has('se') })
  }
  const exitControl = serverModule('server/utils/exit-control.ts', {
    './control': { control, controlApply },
    '../../shared/outbounds': { parseOutbound: () => null },
  })

  const events: string[] = []
  const logs: Array<{ wasDisabled?: boolean, disabled: boolean }> = []
  const handler = serverModule<{ default: (event: { tag: string, disabled: boolean }) => Promise<unknown> }>('server/api/ops/exits/[tag].patch.ts', {
    '../../../utils/auth': { requireAuth: async () => ({ name: 'admin' }) },
    '../../../utils/events': {
      actorOf: () => 'admin',
      joinDetail: (...parts: string[]) => parts.join(' · '),
      recordEvent: async (row: { type: string }) => { events.push(row.type) },
    },
    '../../../utils/exit-control': exitControl,
    '../../../utils/vm-client': { nodeInstances: () => ['ru', 'se', 'fi', 'at'].map(tag => ({ tag })) },
  }, {
    defineEventHandler: (fn: unknown) => fn,
    getRouterParam: (event: { tag: string }) => event.tag,
    readValidatedBody: async (event: { disabled: boolean }, parse: (body: unknown) => unknown) => parse({ disabled: event.disabled }),
    createError: (init: { statusMessage: string }) => Object.assign(new Error(init.statusMessage), init),
    useLogger: () => ({ info: (fields: { wasDisabled?: boolean, disabled: boolean }) => { logs.push(fields) } }),
    setTimeout,
  }).default

  const results = await Promise.all(Array.from({ length: 30 }, (_, i) => handler({ tag: 'se', disabled: i % 2 === 0 })))
  expect(results).toHaveLength(30)

  const transitions = applied.filter(row => row.before !== row.after).map(row => row.after ? 'exit_disabled' : 'exit_enabled')
  console.log(`записей ${applied.length}, переходов ${transitions.length}, событий журнала ${events.length}`)
  expect(events, 'по событию на каждый фактический переход, в том же порядке').toEqual(transitions)
  expect(logs.map(row => row.wasDisabled), 'прежнее состояние в строке журнала - то, поверх которого записали').toEqual(applied.map(row => row.before))
})
