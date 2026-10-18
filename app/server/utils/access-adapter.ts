import { execFile, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import { promisify } from 'node:util'
import { eq } from 'drizzle-orm'
import type { AccessOperation, AccessProtocol } from '../../shared/access-operation'
import { CONFIRMATION_FRESH_MS } from '../../shared/access-operation'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import type { AccessPayload } from './access-operations'
import { isClientActive } from './client-status'
import { syncWireguardConfig } from './wireguard'
import { connectedOvpnClients, killOvpnClient, ovpnCn, reissueDeviceOvpn, revokeAccessCert, syncOpenvpnConfig } from './openvpn'
import { syncIkev2 } from './ikev2'

/** Узкий контракт для локального сервиса: только целевое состояние и его проверка. */
export interface AccessAdapter {
  enabled(): Promise<Record<AccessProtocol, boolean>>
  read(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<boolean>
  apply(protocol: AccessProtocol, operation: AccessOperation, payload: AccessPayload): Promise<void>
}
const exec = promisify(execFile)
const OVPN = '/etc/openvpn/server'

/** Проверка опубликованного CRL; сертификат передаётся только через stdin. */
async function certificateState(cert: string): Promise<'valid' | 'revoked' | 'unknown'> {
  return new Promise((resolve, reject) => {
    const child = spawn('openssl', ['verify', '-CAfile', `${OVPN}/pki/ca.crt`, '-CRLfile', `${OVPN}/pki/crl.pem`, '-crl_check'], { stdio: ['pipe', 'pipe', 'pipe'] })
    let output = ''
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('timeout')) }, 10_000)
    child.stdout.on('data', chunk => { output += chunk })
    child.stderr.on('data', chunk => { output += chunk })
    child.on('error', err => { clearTimeout(timer); reject(err) })
    child.on('close', code => {
      clearTimeout(timer)
      resolve(code === 0 ? 'valid' : /error 23 at 0 depth lookup: certificate revoked/.test(output) ? 'revoked' : 'unknown')
    })
    child.stdin.on('error', () => {})
    child.stdin.end(cert)
  })
}

async function desired(operation: AccessOperation) {
  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, operation.clientId))
  if (!client || client.accessVersion !== operation.version) throw new Error('superseded')
  const rows = await db.select().from(devices).where(eq(devices.clientId, client.id))
  return rows.map(d => ({ ...d, enabled: !d.deleting && !client.deleting && isClientActive(client) }))
}

/** Нынешний применитель; сервис подставит реализацию этого интерфейса. */
export const accessAdapter: AccessAdapter = {
  async enabled() {
    const cfg = useRuntimeConfig()
    return { wg: Boolean(cfg.wgEnabled), openvpn: Boolean(cfg.ovpnEnabled), ikev2: Boolean(cfg.ikev2Enabled) }
  },
  async read(protocol, operation, payload) {
    const rows = await desired(operation)
    if (protocol === 'wg') {
      const { stdout } = await exec('flock', ['-w', '10', '/etc/wireguard/.access-apply.lock', 'wg', 'show', 'wg0', 'peers'], { timeout: 20_000, maxBuffer: 1024 * 1024 })
      const peers = new Set(stdout.trim().split(/\s+/))
      return rows.every(d => !d.wgPublicKey || peers.has(d.wgPublicKey) === d.enabled)
        && payload.retired.every(d => !d.wgPublicKey || !peers.has(d.wgPublicKey))
    }
    if (protocol === 'openvpn') {
      const connected = await connectedOvpnClients()
      for (const d of rows) {
        if (!d.ovpnCert) continue
        const ccd = await fs.readFile(`${OVPN}/ccd/${ovpnCn(d.id)}`, 'utf8').catch((error: NodeJS.ErrnoException) => {
          if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return null
          throw error
        })
        if (ccd === null) return false
        if (/^disable\s*$/m.test(ccd) === d.enabled) return false
        if (!d.enabled && connected.has(ovpnCn(d.id))) return false
      }
      for (const old of payload.retired) {
        if (!old.ovpnCert) continue
        if (operation.protocols.openvpn.confirmedVersion !== operation.version && connected.has(ovpnCn(old.id))) return false
        if (await certificateState(old.ovpnCert) !== 'revoked') return false
      }
      for (const pending of payload.rotateOvpn) {
        const d = rows.find(d => d.id === pending.id)
        if (d && !d.deleting && (!d.ovpnCert || d.ovpnCert === pending.cert || await certificateState(d.ovpnCert) !== 'valid')) return false
      }
      return true
    }
    // Хостовый применитель публикует digest только после load-all и разрыва старых SA.
    const status = JSON.parse(await fs.readFile('/etc/anysda/ikev2-status.json', 'utf8')) as {
      updated: number, applied_digest?: string, connections?: string[], sessions: Array<{ username: string }>
    }
    if (!status.updated || Date.now() - status.updated * 1000 > CONFIRMATION_FRESH_MS || !status.applied_digest || !Array.isArray(status.connections)) throw new Error('stale')
    const config = await fs.readFile('/etc/swanctl/conf.d/anysda-clients.conf', 'utf8')
    if (createHash('sha256').update(config).digest('hex') !== status.applied_digest) return false
    for (const d of rows) {
      if (!d.ikev2Username) continue
      if (status.connections.includes(`anysda-dev-${d.id}`) !== d.enabled) return false
      const secret = `id = "${d.ikev2Username}"\n    secret = "${d.ikev2Password}"`
      if (config.includes(secret) !== d.enabled) return false
      if (!d.enabled && status.sessions.some(s => s.username === d.ikev2Username)) return false
    }
    return true
  },
  async apply(protocol, operation, payload) {
    const rows = await desired(operation)
    if (protocol === 'wg') return syncWireguardConfig()
    if (protocol === 'ikev2') return syncIkev2()
    for (const old of payload.retired) {
      await desired(operation)
      if (old.ovpnCert) await revokeAccessCert(old.ovpnCert)
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
