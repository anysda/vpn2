export const accessProtocols = ['wg', 'openvpn', 'ikev2'] as const
export type AccessProtocol = typeof accessProtocols[number]
export type AccessKind = 'freeze' | 'unfreeze' | 'delete' | 'reissue'
type AccessState = 'pending' | 'confirmed' | 'failed' | 'unknown' | 'not_required'
interface ProtocolState {
  state: AccessState
  confirmedVersion: number | null
  checkedAt: number | null
  lastAttempt: number | null
  nextRetry: number | null
  error: string | null
}
export interface AccessOperation {
  id: string
  clientId: number
  deviceId: number | null
  kind: AccessKind
  version: number
  status: 'pending' | 'confirmed' | 'superseded'
  protocols: Record<AccessProtocol, ProtocolState>
  nextRetry: number
  createdAt: number
}
export const protocolNames: Record<AccessProtocol, string> = { wg: 'WireGuard', openvpn: 'OpenVPN', ikev2: 'IKEv2' }
export const CONFIRMATION_FRESH_MS = 120_000

export function accessStatus(op: AccessOperation): string {
  if (op.status === 'superseded') return 'Заменена'
  const states = accessProtocols.map(p => op.protocols[p])
  if (states.some(s => s.state === 'unknown')) return 'Состояние не подтверждено'
  const failed = accessProtocols.filter(p => op.protocols[p].state === 'failed')
  if (failed.length) return `${op.kind === 'reissue' ? 'Перевыпуск не завершён' : `${op.kind === 'freeze' || op.kind === 'delete' ? 'Отключение' : 'Включение'} не завершено`}: ${failed.map(p => protocolNames[p]).join(', ')}`
  if (op.status === 'confirmed') {
    if (states.some(s => s.state === 'confirmed' && (!s.checkedAt || Date.now() - s.checkedAt > CONFIRMATION_FRESH_MS))) return 'Состояние не подтверждено'
    return op.kind === 'reissue' ? 'Ключи перевыпущены' : op.kind === 'unfreeze' ? 'Доступ включён' : 'Доступ отключён'
  }
  return op.kind === 'reissue' ? 'Перевыпуск выполняется' : op.kind === 'unfreeze' ? 'Включение выполняется' : 'Отключение выполняется'
}
