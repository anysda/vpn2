import type { AccessProtocol } from '../../shared/access-operation'
import type { accessOperations } from '../database/schema'

interface RetiredAccess { id: number, wgPublicKey: string | null, ovpnCert: string | null }
export interface AccessPayload {
  enabled: boolean
  retired: RetiredAccess[]
  rotateOvpn: Array<{ id: number, cert: string | null }>
  name: string
  chatId: number | null
  notice?: string
}
export type StoredOperation = typeof accessOperations.$inferSelect

/**
 * Что новая операция берёт у неподтверждённой предыдущей. Протокол, который
 * она уже подтвердила, свои старые реквизиты убрал: их не тянем дальше.
 */
export function carried(row: StoredOperation): Pick<AccessPayload, 'retired' | 'rotateOvpn'> {
  const payload: AccessPayload = JSON.parse(row.payload)
  const done = (protocol: AccessProtocol) => row.protocols[protocol].confirmedVersion === row.version
  const retired = payload.retired
    .map(old => ({ id: old.id, wgPublicKey: done('wg') ? null : old.wgPublicKey, ovpnCert: done('openvpn') ? null : old.ovpnCert }))
    .filter(old => old.wgPublicKey || old.ovpnCert)
  return { retired, rotateOvpn: done('openvpn') ? [] : payload.rotateOvpn }
}

/**
 * Одна запись на устройство: реквизиты, с которых началась цепочка
 * неподтверждённых операций (их последними подтвердила VPN-служба).
 * Промежуточный ключ WG снимает полная замена пиров (wg.apply), а проверка
 * находит его по адресу устройства; промежуточный сертификат OpenVPN отозван
 * своей же ротацией, текущий отзывают rotateOvpn и удаление устройства.
 */
export function retire(retired: RetiredAccess[], entry: RetiredAccess): RetiredAccess[] {
  const own = retired.find(old => old.id === entry.id)
  if (!own) return [...retired, entry]
  return retired.map(old => old !== own ? old : {
    id: own.id, wgPublicKey: own.wgPublicKey ?? entry.wgPublicKey, ovpnCert: own.ovpnCert ?? entry.ovpnCert,
  })
}
