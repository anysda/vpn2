import { createHash, X509Certificate } from 'node:crypto'
import { clientDns } from './client-dns'
import { control } from './control'
import { eq } from 'drizzle-orm'
import type { AccessOperation, AccessProtocol } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import type { AccessPayload } from './access-operations'
import { expiryForControl, isClientActive } from './client-status'
import { ensureDeviceWg, syncWireguardConfig } from './wireguard'
import { ensureDeviceOvpn, killOvpnClient, ovpnCn, reissueDeviceOvpn, revokeAccessCert, syncOpenvpnConfig } from './openvpn'
import { ensureDeviceIkev2, syncIkev2 } from './ikev2'

/** Узкий контракт для локального сервиса: только целевое состояние и его проверка. */
export interface AccessAdapter {
  enabled(): Promise<Record<AccessProtocol, boolean>>
  read(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<boolean>
  apply(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<void>
}
async function certificateState(certPem: string): Promise<{ valid: boolean, revoked: boolean, allowed: boolean, present: boolean }> {
  const cert = new X509Certificate(certPem)
  const match = cert.subject.match(/(?:^|\n)CN=dev_(\d+)(?:\n|$)/)
  if (!match) throw new Error('Invalid managed certificate')
  return control('openvpn.certificate', { device_id: Number(match[1]), serial: cert.serialNumber })
}

async function desired(operation: AccessOperation) {
  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, operation.clientId))
  if (!client || client.accessVersion !== operation.version) throw new Error('superseded')
  const rows = await db.select().from(devices).where(eq(devices.clientId, client.id))
  return rows.map(d => ({ ...d, filterTraffic: client.filterTraffic, expires: expiryForControl(client.expiresAt), enabled: !d.deleting && !client.deleting && isClientActive(client) }))
}

/** Сервис снимает доступ по сроку сам; продление должно дойти до него, иначе снимет по старому. */
function sameExpiry(expires: Record<string, string> | undefined, id: string, expected: string | null): boolean {
  return !expires || (expires[id] ?? null) === expected
}

/** All OS effects and observations are owned by the local control service. */
export const accessAdapter: AccessAdapter = {
  async enabled() {
    const cfg = useRuntimeConfig()
    return { wg: Boolean(cfg.wgEnabled), openvpn: Boolean(cfg.ovpnEnabled), ikev2: Boolean(cfg.ikev2Enabled) }
  },
  async read(protocol, operation, payload) {
    const rows = await desired(operation)
    if (protocol === 'wg') {
      const state = await control<{ peers: Record<string, string[]>, expires?: Record<string, string> }>('state.read', { protocol: 'wg' })
      const peers = new Set(Object.keys(state.peers))
      if (operation.kind === 'reissue' && rows.some(d => !d.deleting && (!operation.deviceId || operation.deviceId === d.id)
        && (!d.wgPublicKey || !d.wgPrivateKey || !d.wgPresharedKey || !d.wgIp))) return false
      return rows.every(d => !d.wgPublicKey || (peers.has(d.wgPublicKey) === d.enabled && (!d.enabled || sameExpiry(state.expires, d.wgPublicKey, d.expires))))
        && payload.retired.every(d => !d.wgPublicKey || !peers.has(d.wgPublicKey))
    }
    if (protocol === 'openvpn') {
      const { sessions, expires } = await control<{ sessions: Record<string, string>, expires?: Record<string, string> }>('state.read', { protocol: 'openvpn' })
      const connected = new Set(Object.keys(sessions))
      for (const d of rows) {
        if (!d.ovpnCert) continue
        const cert = await certificateState(d.ovpnCert)
        if (!cert.present) return false
        if ((d.deleting || payload.retired.some(old => old.ovpnCert === d.ovpnCert)) && !cert.revoked) return false
        if (cert.allowed !== d.enabled) return false
        if (d.enabled && !sameExpiry(expires, String(d.id), d.expires)) return false
        if (!d.enabled && connected.has(ovpnCn(d.id))) return false
      }
      for (const old of payload.retired) {
        if (!old.ovpnCert) continue
        if (operation.protocols.openvpn.confirmedVersion !== operation.version && connected.has(ovpnCn(old.id))) return false
        if (!(await certificateState(old.ovpnCert)).revoked) return false
      }
      for (const pending of payload.rotateOvpn) {
        const d = rows.find(d => d.id === pending.id)
        if (d && !d.deleting && (!d.ovpnCert || d.ovpnCert === pending.cert || !(await certificateState(d.ovpnCert)).valid)) return false
      }
      return true
    }
    const status = await control<{ devices: number[], sas: Array<{ username: string }>, credentials: Record<string, string>, expires?: Record<string, string> }>('state.read', { protocol: 'ikev2' })
    for (const d of rows) {
      if (!d.ikev2Username) {
        if (operation.kind === 'reissue' && (!operation.deviceId || operation.deviceId === d.id)) return false
        continue
      }
      if (status.devices.includes(d.id) !== d.enabled) return false
      // Canonical encoding matches the service; no EAP password is returned.
      const credential = { device_id: d.id, dns: clientDns(d.filterTraffic), ip: d.ikev2Ip, password: d.ikev2Password, username: d.ikev2Username }
      const digest = createHash('sha256').update(JSON.stringify(credential)).digest('hex')
      if (d.enabled && status.credentials[d.ikev2Username] !== digest) return false
      if (d.enabled && !sameExpiry(status.expires, String(d.id), d.expires)) return false
      if (!d.enabled && status.sas.some(sa => sa.username === d.ikev2Username)) return false
    }
    return true
  },
  async apply(protocol, operation, payload) {
    const rows = await desired(operation)
    if (protocol === 'wg' || protocol === 'ikev2') {
      for (const d of rows.filter(d => operation.kind === 'reissue' && !d.deleting && (!operation.deviceId || operation.deviceId === d.id))) {
        if (protocol === 'wg' && d.wgPublicKey && d.wgPrivateKey && d.wgPresharedKey && d.wgIp) continue
        if (protocol === 'ikev2' && d.ikev2Username && d.ikev2Password && d.ikev2Ip) continue
        await desired(operation)
        if (protocol === 'wg') await ensureDeviceWg(d.id)
        else await ensureDeviceIkev2(d.id)
      }
      await desired(operation)
      return protocol === 'wg' ? syncWireguardConfig() : syncIkev2()
    }
    for (const old of payload.retired) {
      await desired(operation)
      if (old.ovpnCert) await revokeAccessCert(old.ovpnCert)
    }
    for (const d of rows) {
      await desired(operation)
      if (d.deleting && d.ovpnCert && !payload.retired.some(old => old.ovpnCert === d.ovpnCert)) await revokeAccessCert(d.ovpnCert)
      if (d.enabled && operation.kind === 'reissue' && (!operation.deviceId || operation.deviceId === d.id) && (!d.ovpnCert || !d.ovpnKey)) await ensureDeviceOvpn(d.id)
    }
    for (const pending of payload.rotateOvpn) {
      const currentRows = await desired(operation)
      const d = currentRows.find(d => d.id === pending.id)
      // Сохранённый новый сертификат означает, что повтор не должен выпускать ещё один.
      if (d && !d.deleting && d.ovpnCert === pending.cert) await reissueDeviceOvpn(d.id, operation.clientId, operation.version)
    }
    await desired(operation)
    await syncOpenvpnConfig()
    for (const d of rows) {
      if (!d.enabled || payload.retired.some(old => old.id === d.id)) await killOvpnClient(ovpnCn(d.id))
    }
  },
}
