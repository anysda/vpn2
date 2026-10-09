import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { eq, isNotNull } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'

/**
 * Файл предпочитаемых экзитов для роутера: { "<узел>": ["10.66.66.2", …] }.
 * anysda-apply-routes (стадия 20) строит из него selector'ы pref-<узел>-lane-NN
 * = [hy2-<узел>-direct, lane-NN] и правила по IP устройств перед дорожками;
 * failover-watchdog держит selector на экзите, пока узел включён и жив, иначе —
 * на дорожке устройства (обычная логика). Берём статические IP WireGuard и
 * IKEv2; OpenVPN выдаёт адрес из пула при подключении — его не покрываем.
 *
 * Пишем только при изменении содержимого: .path-юнит дёргает пересборку
 * конфига sing-box на каждую запись.
 */
export async function syncClientPrefsFile(): Promise<{ changed: boolean, nodes: number }> {
  const path = useRuntimeConfig().clientPrefsFilePath as string
  const rows = await useDb()
    .select({ node: clients.preferredExit, wg: devices.wgIp, ikev2: devices.ikev2Ip })
    .from(devices)
    .innerJoin(clients, eq(clients.id, devices.clientId))
    .where(isNotNull(clients.preferredExit))

  const byNode: Record<string, string[]> = {}
  for (const r of rows) {
    if (!r.node) continue
    for (const ip of [r.wg, r.ikev2]) {
      if (ip) (byNode[r.node] ??= []).push(ip)
    }
  }
  const sorted = Object.fromEntries(
    Object.keys(byNode).sort().map(n => [n, [...new Set(byNode[n])].sort()]),
  )
  const body = JSON.stringify(sorted, null, 2) + '\n'

  let current = ''
  try {
    current = readFileSync(path, 'utf8')
  }
  catch { /* файла ещё нет */ }
  if (current === body) return { changed: false, nodes: Object.keys(sorted).length }

  const dir = dirname(path)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  writeFileSync(path, body, { mode: 0o644 })
  useLogger().info({ nodes: Object.keys(sorted).length, path }, 'client prefs synced')
  return { changed: true, nodes: Object.keys(sorted).length }
}
