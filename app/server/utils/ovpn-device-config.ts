import type { clients } from '../database/schema'
import { clientDns } from './client-dns'
import { buildOvpnConfig, caReady, ensureDeviceOvpn } from './openvpn'

/**
 * Проверки до поиска в базе: OpenVPN включён, публичный адрес задан, CA готов.
 * `beforeCa` - проверка маршрута между адресом и готовностью CA (секрет бота у отправки в Telegram).
 */
export async function requireOvpnEndpoint(beforeCa?: () => void): Promise<string> {
  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) throw createError({ statusCode: 503, statusMessage: 'ovpn_disabled', message: 'OpenVPN выключен' })
  const endpoint = String(cfg.ovpnPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'ovpn_public_host_not_configured' })
  beforeCa?.()
  if (!(await caReady())) {
    throw createError({ statusCode: 503, statusMessage: 'ovpn_ca_not_ready', message: 'OpenVPN CA ещё не инициализирован (стадия 29-openvpn)' })
  }
  return endpoint
}

/** Клиентский конфиг OpenVPN девайса; сертификат девайса выдаётся при первом обращении. */
export async function deviceOvpnConfig(deviceId: number, filterTraffic: typeof clients.$inferSelect.filterTraffic, endpoint: string): Promise<string> {
  const cfg = useRuntimeConfig()
  const dev = await ensureDeviceOvpn(deviceId)
  return buildOvpnConfig({
    clientCert: dev.ovpnCert!,
    clientKey: dev.ovpnKey!,
    serverHost: endpoint,
    serverPort: Number(cfg.ovpnPort),
    proto: String(cfg.ovpnProto),
    dns: clientDns(filterTraffic),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.mgmtMeshIpPrefix)],
  })
}
