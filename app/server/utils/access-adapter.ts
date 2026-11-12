import { createHash } from 'node:crypto'
import { eq } from 'drizzle-orm'
import type { AccessOperation, AccessProtocol } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'

/** Узкий контракт для локального сервиса: только целевое состояние и его проверка. */
export interface AccessAdapter {
  enabled(): Promise<Record<AccessProtocol, boolean>>
  read(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<boolean>
  apply(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<void>
}
type Expires = Record<string, string> | undefined

function certificateState(certPem: string) {
  const { device_id, serial } = certificateOf(certPem)
  return control<{ valid: boolean, revoked: boolean, allowed: boolean, present: boolean }>('openvpn.certificate', { device_id, serial })
}

/** Устройства клиента с целевым состоянием; вытесненная операция - ошибка. */
async function desired(operation: AccessOperation) {
  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, operation.clientId))
  if (!client || client.accessVersion !== operation.version) throw new Error('superseded')
  const rows = await db.select().from(devices).where(eq(devices.clientId, client.id))
  const enabled = !client.deleting && isClientActive(client)
  return rows.map(d => ({ ...d, filterTraffic: client.filterTraffic, expires: expiryForControl(client.expiresAt), enabled: enabled && !d.deleting }))
}

/** Сервис снимает доступ по сроку сам; продление должно дойти до него, иначе снимет по старому. */
const sameExpiry = (expires: Expires, id: string, expected: string | null) => !expires || (expires[id] ?? null) === expected
/** Устройство, которому эта операция перевыпускает реквизиты. */
const reissued = (operation: AccessOperation, id: number) => operation.kind === 'reissue' && (!operation.deviceId || operation.deviceId === id)

async function readWg(operation: AccessOperation, payload: AccessPayload, rows: Awaited<ReturnType<typeof desired>>) {
  const state = await control<{ peers: Record<string, string[]>, expires?: Expires }>('state.read', { protocol: 'wg' })
  const peers = new Set(Object.keys(state.peers))
  if (rows.some(d => !d.deleting && reissued(operation, d.id) && (!d.wgPublicKey || !d.wgPrivateKey || !d.wgPresharedKey || !d.wgIp))) return false
  // Ключ из середины цепочки перевыпусков в retired не хранится: на адресе устройства только его текущий ключ.
  const strangers = rows.some(d => d.wgIp && Object.entries(state.peers)
    .some(([key, addresses]) => addresses.includes(`${d.wgIp}/32`) && (!d.enabled || key !== d.wgPublicKey)))
  return !strangers
    && rows.every(d => !d.wgPublicKey || (peers.has(d.wgPublicKey) === d.enabled && (!d.enabled || sameExpiry(state.expires, d.wgPublicKey, d.expires))))
    && payload.retired.every(d => !d.wgPublicKey || !peers.has(d.wgPublicKey))
}

async function readOpenvpn(operation: AccessOperation, payload: AccessPayload, rows: Awaited<ReturnType<typeof desired>>) {
  const { sessions, expires } = await control<{ sessions: Record<string, string>, expires?: Expires }>('state.read', { protocol: 'openvpn' })
  const connected = new Set(Object.keys(sessions))
  for (const d of rows.filter(d => d.ovpnCert)) {
    const cert = await certificateState(d.ovpnCert!)
    if (!cert.present || cert.allowed !== d.enabled) return false
    if ((d.deleting || payload.retired.some(old => old.ovpnCert === d.ovpnCert)) && !cert.revoked) return false
    if (d.enabled ? !sameExpiry(expires, String(d.id), d.expires) : connected.has(ovpnCn(d.id))) return false
  }
  for (const old of payload.retired.filter(old => old.ovpnCert)) {
    if (operation.protocols.openvpn.confirmedVersion !== operation.version && connected.has(ovpnCn(old.id))) return false
    if (!(await certificateState(old.ovpnCert!)).revoked) return false
  }
  for (const pending of payload.rotateOvpn) {
    const d = rows.find(d => d.id === pending.id)
    if (d && !d.deleting && (!d.ovpnCert || d.ovpnCert === pending.cert || !(await certificateState(d.ovpnCert)).valid)) return false
  }
  return true
}

async function readIkev2(operation: AccessOperation, rows: Awaited<ReturnType<typeof desired>>) {
  const status = await control<{ devices: number[], sas: Array<{ username: string }>, credentials: Record<string, string>, expires?: Expires }>('state.read', { protocol: 'ikev2' })
  for (const d of rows) {
    if (!d.ikev2Username) {
      if (reissued(operation, d.id)) return false
      continue
    }
    if (status.devices.includes(d.id) !== d.enabled) return false
    if (!d.enabled) {
      if (status.sas.some(sa => sa.username === d.ikev2Username)) return false
      continue
    }
    // Каноническая запись совпадает с сервисом; пароль EAP он не возвращает.
    const credential = { device_id: d.id, dns: clientDns(d.filterTraffic), ip: d.ikev2Ip, password: d.ikev2Password, username: d.ikev2Username }
    if (status.credentials[d.ikev2Username] !== createHash('sha256').update(JSON.stringify(credential)).digest('hex')) return false
    if (!sameExpiry(status.expires, String(d.id), d.expires)) return false
  }
  return true
}

/** Выдача и отзыв ведут к цели по шагам; перед каждым шагом операция ещё текущая. */
async function applyOpenvpn(operation: AccessOperation, payload: AccessPayload) {
  const rows = await desired(operation)
  const retiredCert = (cert: string | null) => payload.retired.some(old => old.ovpnCert === cert)
  for (const old of payload.retired) {
    await desired(operation)
    if (old.ovpnCert) await revokeAccessCert(old.ovpnCert)
  }
  for (const d of rows) {
    await desired(operation)
    if (d.deleting && d.ovpnCert && !retiredCert(d.ovpnCert)) await revokeAccessCert(d.ovpnCert)
    if (d.enabled && reissued(operation, d.id) && (!d.ovpnCert || !d.ovpnKey)) await ensureDeviceOvpn(d.id)
  }
  for (const pending of payload.rotateOvpn) {
    const d = (await desired(operation)).find(d => d.id === pending.id)
    // Сохранённый новый сертификат значит, что повтор не должен выпускать ещё один.
    if (d && !d.deleting && d.ovpnCert === pending.cert) await reissueDeviceOvpn(d.id, operation.clientId, operation.version)
  }
  await desired(operation)
  await syncOpenvpnConfig()
  for (const d of rows) {
    if (!d.enabled || payload.retired.some(old => old.id === d.id)) await killOvpnClient(ovpnCn(d.id))
  }
}

/** Все действия и наблюдения в ОС ведёт локальный сервис управления. */
export const accessAdapter: AccessAdapter = {
  async enabled() {
    const cfg = useRuntimeConfig()
    return { wg: Boolean(cfg.wgEnabled), openvpn: Boolean(cfg.ovpnEnabled), ikev2: Boolean(cfg.ikev2Enabled) }
  },
  async read(protocol, operation, payload) {
    const rows = await desired(operation)
    if (protocol === 'wg') return readWg(operation, payload, rows)
    return protocol === 'openvpn' ? readOpenvpn(operation, payload, rows) : readIkev2(operation, rows)
  },
  async apply(protocol, operation, payload) {
    if (protocol === 'openvpn') return applyOpenvpn(operation, payload)
    const complete = protocol === 'wg'
      ? (d: typeof devices.$inferSelect) => d.wgPublicKey && d.wgPrivateKey && d.wgPresharedKey && d.wgIp
      : (d: typeof devices.$inferSelect) => d.ikev2Username && d.ikev2Password && d.ikev2Ip
    for (const d of (await desired(operation)).filter(d => reissued(operation, d.id) && !d.deleting && !complete(d))) {
      await desired(operation)
      await (protocol === 'wg' ? ensureDeviceWg(d.id) : ensureDeviceIkev2(d.id))
    }
    await desired(operation)
    return protocol === 'wg' ? syncWireguardConfig() : syncIkev2()
  },
}
