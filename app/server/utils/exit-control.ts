import { control, controlApply } from './control'
import { parseOutbound } from '../../shared/outbounds'

export async function readDisabledExits(): Promise<string[]> {
  return (await control<{ config: { disabled: string[] } }>('settings.read', { resource: 'disabled-exits' })).config.disabled
}

async function writeDisabledExits(tags: string[]): Promise<void> {
  await controlApply('settings.apply', { resource: 'disabled-exits', config: { disabled: [...new Set(tags)].sort() } })
}

// Чтение-изменение-запись по очереди: иначе два одновременных переключения
// читают одно состояние, и второе затирает первое.
let queue: Promise<unknown> = Promise.resolve()

/**
 * Включить или выключить экзит. false — не записано: выключался бы последний
 * включённый экзит, и watchdog'у некуда было бы уводить иностранный трафик.
 * Проверка внутри очереди, по тому состоянию, поверх которого и пишем.
 */
export function toggleExit(tag: string, disabled: boolean, exits: string[]): Promise<boolean> {
  const run = queue.then(async () => {
    const current = new Set(await readDisabledExits())
    if (disabled) current.add(tag)
    else current.delete(tag)
    if (exits.every(t => current.has(t))) return false
    await writeDisabledExits([...current].filter(t => exits.includes(t)))
    return true
  })
  queue = run.catch(() => {})
  return run
}

/** Узел только пользовательского выхода; служебные каналы не дают нагрузку экзита. */
export function exitNodeOf(outbound: string | undefined | null): string | null {
  if (!outbound || outbound === 'direct-ru') return null
  return parseOutbound(outbound)?.tag ?? null
}
