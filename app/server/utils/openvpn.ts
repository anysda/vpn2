import { execFile } from 'node:child_process'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'
import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients as clientsTable } from '../database/schema'

const exec = promisify(execFile)

// CA + server PKI laid down by stage 29-openvpn; mounted into the panel
// container at the same path so the panel can issue/revoke client certs.
const OVPN_DIR = '/etc/openvpn/server'
const PKI_DIR = `${OVPN_DIR}/pki`
const CCD_DIR = `${OVPN_DIR}/ccd`
const CA_CERT = `${PKI_DIR}/ca.crt`
const CA_KEY = `${PKI_DIR}/ca.key`
const TLS_CRYPT = `${PKI_DIR}/tls-crypt.key`
const OSSL_CNF = `${PKI_DIR}/openssl.cnf`
const CRL_PEM = `${PKI_DIR}/crl.pem`

/** OpenVPN cert CN for a client — stable, derived from the DB id. */
export function ovpnCn(clientId: number): string {
  return `client_${clientId}`
}

/** True once stage 29-openvpn has seeded the CA. */
export async function caReady(): Promise<boolean> {
  try {
    await Promise.all([fs.access(CA_CERT), fs.access(CA_KEY), fs.access(OSSL_CNF)])
    return true
  }
  catch {
    return false
  }
}

/**
 * Issue an EC (prime256v1) client keypair signed by the CA via `openssl ca`,
 * which records the cert in the CA index so it can later be revoked into a
 * CRL. Returns the PEM cert + key.
 */
export async function issueClientCert(cn: string): Promise<{ cert: string, key: string }> {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'ovpn-'))
  try {
    const keyPath = path.join(tmp, 'key.pem')
    const csrPath = path.join(tmp, 'csr.pem')
    const certPath = path.join(tmp, 'cert.pem')

    await exec('openssl', [
      'genpkey', '-algorithm', 'EC',
      '-pkeyopt', 'ec_paramgen_curve:prime256v1',
      '-out', keyPath,
    ], { timeout: 15_000 })

    await exec('openssl', [
      'req', '-new', '-key', keyPath, '-out', csrPath, '-subj', `/CN=${cn}`,
    ], { timeout: 15_000 })

    await exec('openssl', [
      'ca', '-batch', '-notext', '-config', OSSL_CNF,
      '-extensions', 'client_ext', '-days', '3650',
      '-in', csrPath, '-out', certPath,
    ], { timeout: 20_000 })

    const [cert, key] = await Promise.all([
      fs.readFile(certPath, 'utf8'),
      fs.readFile(keyPath, 'utf8'),
    ])
    // Keep only the PEM block of the cert (openssl ca may prepend metadata).
    const m = cert.match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/)
    return { cert: (m ? m[0] : cert).trim() + '\n', key: key.trim() + '\n' }
  }
  finally {
    await fs.rm(tmp, { recursive: true, force: true }).catch(() => {})
  }
}

/** Revoke a client cert and regenerate the CRL OpenVPN reads via crl-verify. */
export async function revokeClientCert(certPem: string): Promise<void> {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'ovpn-rev-'))
  try {
    const certPath = path.join(tmp, 'cert.pem')
    await fs.writeFile(certPath, certPem)
    await exec('openssl', ['ca', '-config', OSSL_CNF, '-revoke', certPath], { timeout: 15_000 })
    await regenCrl()
  }
  finally {
    await fs.rm(tmp, { recursive: true, force: true }).catch(() => {})
  }
}

/** Rebuild crl.pem from the CA index. OpenVPN re-reads it per new connection. */
export async function regenCrl(): Promise<void> {
  await exec('openssl', ['ca', '-config', OSSL_CNF, '-gencrl', '-out', CRL_PEM], { timeout: 15_000 })
}

/**
 * Enable/disable a client without touching its cert: OpenVPN's client-config-dir
 * `disable` directive blocks new connections and is fully reversible (unlike a
 * CRL revoke). Active on the next connection attempt.
 */
export async function setCcdDisabled(cn: string, disabled: boolean): Promise<void> {
  await fs.mkdir(CCD_DIR, { recursive: true })
  const file = path.join(CCD_DIR, cn)
  if (disabled) {
    await fs.writeFile(file, 'disable\n')
  }
  else {
    await fs.rm(file, { force: true }).catch(() => {})
  }
}

export interface OvpnConfigParams {
  clientCert: string
  clientKey: string
  serverHost: string
  serverPort: number
  proto: string
}

/** Render a unified .ovpn with inline ca / cert / key / tls-crypt blocks. */
export async function buildOvpnConfig(p: OvpnConfigParams): Promise<string> {
  const [ca, tlsCrypt] = await Promise.all([
    fs.readFile(CA_CERT, 'utf8'),
    fs.readFile(TLS_CRYPT, 'utf8'),
  ])
  return [
    `client`,
    `dev tun`,
    `proto ${p.proto}`,
    `remote ${p.serverHost} ${p.serverPort}`,
    `resolv-retry infinite`,
    `nobind`,
    `persist-key`,
    `persist-tun`,
    `remote-cert-tls server`,
    `cipher AES-256-GCM`,
    `auth SHA256`,
    `verb 3`,
    ``,
    `<ca>`,
    ca.trim(),
    `</ca>`,
    `<cert>`,
    p.clientCert.trim(),
    `</cert>`,
    `<key>`,
    p.clientKey.trim(),
    `</key>`,
    `<tls-crypt>`,
    tlsCrypt.trim(),
    `</tls-crypt>`,
    ``,
  ].join('\n')
}

/**
 * Make sure the client has an OpenVPN cert. If absent, issue one and persist
 * the cert + key on the row. Returns the (possibly updated) record.
 */
export async function ensureClientOvpn(clientId: number) {
  const db = useDb()
  const [row] = await db.select().from(clientsTable).where(eq(clientsTable.id, clientId)).limit(1)
  if (!row) throw new Error(`client ${clientId} not found`)
  if (row.ovpnCert && row.ovpnKey) return row

  const { cert, key } = await issueClientCert(ovpnCn(clientId))
  const [updated] = await db
    .update(clientsTable)
    .set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
    .where(eq(clientsTable.id, clientId))
    .returning()
  // New cert → make sure no stale CCD-disable lingers for this CN.
  await setCcdDisabled(ovpnCn(clientId), !updated.enabled).catch(() => {})
  return updated
}

/** Reconcile CCD enable/disable flags for every client with an issued cert. */
export async function syncOpenvpnConfig(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) return
  if (!(await caReady())) return

  const db = useDb()
  const all = await db.select().from(clientsTable)
  for (const c of all) {
    if (!c.ovpnCert) continue
    await setCcdDisabled(ovpnCn(c.id), !c.enabled).catch((err) => {
      useLogger().warn({ err: (err as Error).message, id: c.id }, 'ovpn ccd sync failed')
    })
  }
}
