import { randomBytes } from 'node:crypto'
import { chmod, readFile, rename, rm, writeFile } from 'node:fs/promises'

/**
 * Ручное выключение экзитов из панели.
 *
 * Источник истины — файл на хосте (/etc/anysda смонтирован в контейнер rw).
 * Его каждый тик читает anysda-failover-watchdog: выключенный узел для него —
 * как мёртвый (уводит foreign-best сразу, рвёт висящие через узел соединения),
 * поэтому панель сама selector не трогает. 0644 — watchdog бежит под
 * DynamicUser и должен уметь прочитать файл.
 */
const DISABLED_FILE = '/etc/anysda/exits-disabled.json'

export async function readDisabledExits(file = DISABLED_FILE): Promise<string[]> {
  try {
    const parsed = JSON.parse(await readFile(file, 'utf8')) as { disabled?: unknown }
    return Array.isArray(parsed.disabled)
      ? parsed.disabled.filter((t): t is string => typeof t === 'string')
      : []
  }
  catch {
    return [] // нет файла / битый JSON — значит ничего не выключено (так же считает watchdog)
  }
}

async function writeDisabledExits(tags: string[], file: string): Promise<void> {
  const body = JSON.stringify({ disabled: [...new Set(tags)].sort(), updatedAt: new Date().toISOString() }, null, 2)
  // Своё имя у каждой записи: с общим .tmp параллельная запись успевала
  // переименовать чужой файл, и rename второй падал с ENOENT.
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  try {
    await writeFile(tmp, `${body}\n`, { mode: 0o644 })
    await chmod(tmp, 0o644)
    await rename(tmp, file) // атомарно: watchdog не прочитает полузаписанный файл
  }
  catch (err) {
    await rm(tmp, { force: true })
    throw err
  }
}

// Чтение-изменение-запись по очереди: иначе два одновременных переключения
// читают одно состояние, и второе затирает первое.
let queue: Promise<unknown> = Promise.resolve()

/**
 * Включить или выключить экзит. false — не записано: выключался бы последний
 * включённый экзит, и watchdog'у некуда было бы уводить иностранный трафик.
 * Проверка внутри очереди, по тому состоянию, поверх которого и пишем.
 */
export function toggleExit(tag: string, disabled: boolean, exits: string[], file = DISABLED_FILE): Promise<boolean> {
  const run = queue.then(async () => {
    const current = new Set(await readDisabledExits(file))
    if (disabled) current.add(tag)
    else current.delete(tag)
    if (exits.every(t => current.has(t))) return false
    await writeDisabledExits([...current].filter(t => exits.includes(t)), file)
    return true
  })
  queue = run.catch(() => {})
  return run
}

/** Узел экзита из тега outbound'а hy2-<node>-<kind> (как _node_of в watchdog). */
export function exitNodeOf(outbound: string | undefined | null): string | null {
  if (!outbound) return null
  const p = outbound.split('-')
  return p.length >= 3 && p[0] === 'hy2' ? p[1]! : null
}
