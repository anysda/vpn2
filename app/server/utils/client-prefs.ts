import { randomBytes } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { eq, isNotNull } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'

/**
 * Файл предпочитаемых экзитов для роутера: { "<узел>": ["10.66.66.2", …] }.
 * anysda-apply-routes (стадия 20) кладёт IP каждого узла в свой набор правил
 * client-pref-<узел> с выходом pin-hy2-<узел>-direct: sing-box перечитывает
 * набор на ходу, соединения не рвутся. Пока узел выключен, мёртв или в
 * штрафной, failover-watchdog держит pin на foreign-best. Берём статические IP
 * WireGuard и IKEv2; OpenVPN выдаёт адрес из пула при подключении — его не
 * покрываем.
 */
async function writePrefs(): Promise<void> {
  const path = useRuntimeConfig().clientPrefsFilePath as string
  const rows = await useDb()
    .select({ node: clients.preferredExit, wg: devices.wgIp, ikev2: devices.ikev2Ip })
    .from(devices)
    .innerJoin(clients, eq(clients.id, devices.clientId))
    .where(isNotNull(clients.preferredExit))

  const byNode: Record<string, Set<string>> = {}
  for (const r of rows) {
    for (const ip of [r.wg, r.ikev2]) {
      if (r.node && ip) (byNode[r.node] ??= new Set()).add(ip)
    }
  }
  const body = JSON.stringify(
    Object.fromEntries(Object.keys(byNode).sort().map(n => [n, [...byNode[n]!].sort()])),
    null, 2,
  ) + '\n'

  // Без изменений не пишем: .path-юнит запускает пересборку на каждую запись.
  if (await readFile(path, 'utf8').catch(() => '') === body) return
  await mkdir(dirname(path), { recursive: true })
  const tmp = `${path}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  try {
    await writeFile(tmp, body, { mode: 0o644 })
    await rename(tmp, path) // атомарно: роутер не прочитает полузаписанный файл
  }
  catch (err) {
    await rm(tmp, { force: true })
    throw err
  }
  useLogger().info({ path }, 'client prefs synced')
}

// По очереди: иначе запись по устаревшему снимку базы могла лечь последней.
let queue: Promise<unknown> = Promise.resolve()

export function syncClientPrefsFile(): Promise<void> {
  const run = queue.then(writePrefs)
  queue = run.catch(() => {})
  return run
}
