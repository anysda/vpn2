import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices, events } from '../database/schema'
import { control } from './control'
import { recordEvent } from './events'
import { expiryForControl, isClientActive } from './client-status'
import { syncWireguardConfig } from './wireguard'
import { syncOpenvpnConfig } from './openvpn'
import { syncIkev2 } from './ikev2'
import { withAccessLock } from './access-lock'

/**
 * Начало завтрашнего дня (UTC) в миллисекундах — минимально допустимый
 * срок действия клиента. Дату раньше завтрашней ставить нельзя.
 */
export function minExpiryMs(): number {
  const d = new Date()
  d.setUTCHours(0, 0, 0, 0)
  d.setUTCDate(d.getUTCDate() + 1)
  return d.getTime()
}

interface ControlExpiry { protocol: 'wg' | 'openvpn' | 'ikev2', id: string | number, expires_at: string, at: string }

const PROTOCOL_LABEL = { wg: 'WireGuard', openvpn: 'OpenVPN', ikev2: 'IKEv2' }

/**
 * Локальный сервис снимает доступ по сроку и без панели. Его журнал попадает
 * в журнал панели один раз на снятие, в том числе после её простоя.
 */
export async function reportControlExpiry(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled && !cfg.ovpnEnabled && !cfg.ikev2Enabled) return
  const { removed } = await control<{ removed: ControlExpiry[] }>('expiry.run', {})
  const db = useDb()
  for (const item of removed) {
    const key = `control-expiry:${item.protocol}:${item.id}:${item.expires_at}`
    const [known] = await db.select({ id: events.id }).from(events).where(eq(events.key, key)).limit(1)
    if (known) continue
    const [row] = await db.select({ clientId: clients.id, client: clients.name, device: devices.name })
      .from(devices).innerJoin(clients, eq(devices.clientId, clients.id))
      .where(item.protocol === 'wg' ? eq(devices.wgPublicKey, String(item.id)) : eq(devices.id, Number(item.id))).limit(1)
    await recordEvent({
      type: 'access_expired_offline', level: 'info', key, at: Date.parse(item.at),
      object: row ? `client:${row.clientId}` : null, actor: 'сервер',
      title: `Срок истёк, доступ снят сервером · ${row ? `${row.client} · ${row.device}` : 'удалённое устройство'}`,
      detail: PROTOCOL_LABEL[item.protocol],
    })
  }
}

/**
 * Срок, который держит служба, сверяется с базой: после обновления панели или
 * правки базы служба снимала бы по старому сроку или не сняла бы вовсе.
 * Расхождение по действующему устройству - полная пересинхронизация протокола.
 */
export function syncControlExpiry(): Promise<void> {
  return withAccessLock(syncControlExpiryOnce)
}

async function syncControlExpiryOnce(): Promise<void> {
  const cfg = useRuntimeConfig()
  const rows = await useDb().select({
    id: devices.id, wgPublicKey: devices.wgPublicKey, ovpnCert: devices.ovpnCert, ikev2Username: devices.ikev2Username,
    deleting: devices.deleting, frozenManual: clients.frozenManual, expiresAt: clients.expiresAt, clientDeleting: clients.deleting,
  }).from(devices).innerJoin(clients, eq(devices.clientId, clients.id))
  const active = rows.filter(r => !r.deleting && !r.clientDeleting && isClientActive(r))
  const protocols = [
    { on: cfg.wgEnabled, protocol: 'wg', id: (r: typeof rows[number]) => r.wgPublicKey, sync: syncWireguardConfig },
    { on: cfg.ovpnEnabled, protocol: 'openvpn', id: (r: typeof rows[number]) => r.ovpnCert && String(r.id), sync: syncOpenvpnConfig },
    { on: cfg.ikev2Enabled, protocol: 'ikev2', id: (r: typeof rows[number]) => r.ikev2Username && String(r.id), sync: syncIkev2 },
  ]
  for (const { on, protocol, id, sync } of protocols) {
    if (!on) continue
    const { expires } = await control<{ expires?: Record<string, string> }>('state.read', { protocol })
    if (!expires) continue
    if (active.some(r => id(r) && (expires[id(r)!] ?? null) !== expiryForControl(r.expiresAt))) await sync()
  }
}
