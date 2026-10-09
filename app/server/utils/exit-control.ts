import { chmod, readFile, rename, writeFile } from 'node:fs/promises'

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

export async function readDisabledExits(): Promise<string[]> {
  try {
    const parsed = JSON.parse(await readFile(DISABLED_FILE, 'utf8')) as { disabled?: unknown }
    return Array.isArray(parsed.disabled)
      ? parsed.disabled.filter((t): t is string => typeof t === 'string')
      : []
  }
  catch {
    return [] // нет файла / битый JSON — значит ничего не выключено (так же считает watchdog)
  }
}

export async function writeDisabledExits(tags: string[]): Promise<void> {
  const body = JSON.stringify({ disabled: [...new Set(tags)].sort(), updatedAt: new Date().toISOString() }, null, 2)
  const tmp = `${DISABLED_FILE}.tmp`
  await writeFile(tmp, `${body}\n`, { mode: 0o644 })
  await chmod(tmp, 0o644)
  await rename(tmp, DISABLED_FILE) // атомарно: watchdog не прочитает полузаписанный файл
}

/** Узел экзита из тега outbound'а hy2-<node>-<kind> (как _node_of в watchdog). */
export function exitNodeOf(outbound: string | undefined | null): string | null {
  if (!outbound) return null
  const p = outbound.split('-')
  return p.length >= 3 && p[0] === 'hy2' ? p[1]! : null
}
