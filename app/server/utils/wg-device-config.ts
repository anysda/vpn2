import type { H3Event } from 'h3'
import type { clients } from '../database/schema'
import { adminClientDevice } from './device-route'
import { clientDns } from './client-dns'
import { buildWgClientConfig, ensureDeviceWg, loadServerKeys, syncWireguardConfig } from './wireguard'

/** Проверки до поиска в базе: WireGuard включён и публичный адрес задан. */
export function requireWgEndpoint(): string {
  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) throw createError({ statusCode: 503, statusMessage: 'wg_disabled', message: 'WireGuard выключен' })
  const endpoint = String(cfg.wgPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })
  return endpoint
}

/** Клиентский конфиг WireGuard девайса; ключи девайса выдаются при первом обращении. */
export async function deviceWgConfig(deviceId: number, filterTraffic: typeof clients.$inferSelect.filterTraffic, endpoint: string): Promise<string> {
  const server = await loadServerKeys().catch(() => null)
  if (!server) {
    throw createError({ statusCode: 503, statusMessage: 'wg_keys_not_ready', message: 'WireGuard server-ключи ещё не инициализированы (стадия 28-wireguard)' })
  }

  const dev = await ensureDeviceWg(deviceId)
  await syncWireguardConfig().catch(() => {})

  const cfg = useRuntimeConfig()
  return buildWgClientConfig({
    clientPrivateKey: dev.wgPrivateKey!,
    clientPresharedKey: dev.wgPresharedKey!,
    clientIp: dev.wgIp!,
    serverPublicKey: server.publicKey,
    serverEndpoint: endpoint,
    serverPort: Number(cfg.wgListenPort),
    dns: clientDns(filterTraffic).join(', '),
    mtu: Number(cfg.wgMtu),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.wgSubnetPrefix), String(cfg.mgmtMeshIpPrefix)],
  })
}


/** Конфиг девайса для маршрутов админки `clients/[id]/devices/[deviceId]/wg-*`. */
export async function adminDeviceWgConfig(event: H3Event): Promise<string> {
  const { client, device, ready: endpoint } = await adminClientDevice(event, requireWgEndpoint)
  return deviceWgConfig(device.id, client.filterTraffic, endpoint)
}
