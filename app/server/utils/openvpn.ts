import { execFile } from 'node:child_process'
import { promises as fs } from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'
import { and, eq, isNull } from 'drizzle-orm'
import { IP_CLAIM_ATTEMPTS, isUniqueViolation, useDb } from '../database/client'
import { clients as clientsTable, devices as devicesTable } from '../database/schema'
import { ovpnLocalRoutes } from './allowed-ips'
import { isClientActive } from './client-status'

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
// Management-сокет сервера (stage 29-openvpn, `management ... unix`).
const MGMT_SOCK = `${OVPN_DIR}/mgmt.sock`

// Постоянные адреса устройств: 10.67.67.2-239, .1 у сервера. Хвост .240-.253
// стадия 29 отдаёт пулу сервера (ifconfig-pool): туда попадёт устройство, чей
// ccd ещё не записан. Синк ниже выдаст ему адрес и переподключит.
const OVPN_SUBNET = '10.67.67.'
const OVPN_PINNED_LAST = 239

function nextAvailableOvpnIp(usedIps: Array<string | null | undefined>): string {
  const used = new Set(usedIps.filter(Boolean) as string[])
  for (let i = 2; i <= OVPN_PINNED_LAST; i++) {
    const ip = `${OVPN_SUBNET}${i}`
    if (!used.has(ip)) return ip
  }
  throw new Error(`OpenVPN: свободных адресов нет (${OVPN_SUBNET}2-${OVPN_PINNED_LAST})`)
}

/**
 * Выдать устройству постоянный адрес OpenVPN, если его нет. Повтор при
 * конфликте UNIQUE — та же гонка выбора свободного адреса, что у WG и IKEv2.
 */
async function ensureDeviceOvpnIp(deviceId: number) {
  const db = useDb()
  for (let attempt = 1; ; attempt++) {
    const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
    if (!row) throw new Error(`device ${deviceId} not found`)
    if (row.ovpnIp) return row
    const all = await db.select({ ip: devicesTable.ovpnIp }).from(devicesTable)
    try {
      const [updated] = await db
        .update(devicesTable)
        .set({ ovpnIp: nextAvailableOvpnIp(all.map(r => r.ip)), updatedAt: new Date() })
        .where(and(eq(devicesTable.id, deviceId), isNull(devicesTable.ovpnIp)))
        .returning()
      if (updated) return updated
    }
    catch (err) {
      if (!isUniqueViolation(err)) throw err
    }
    if (attempt >= IP_CLAIM_ATTEMPTS) throw new Error(`device ${deviceId}: не удалось занять адрес OpenVPN`)
  }
}

/** OpenVPN cert CN for a device — stable, derived from the DB device id. */
export function ovpnCn(deviceId: number): string {
  return `dev_${deviceId}`
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
async function issueClientCert(cn: string): Promise<{ cert: string, key: string }> {
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
async function regenCrl(): Promise<void> {
  await exec('openssl', ['ca', '-config', OSSL_CNF, '-gencrl', '-out', CRL_PEM], { timeout: 15_000 })
}

// Management OpenVPN обслуживает одно подключение за раз — команды панели
// выстраиваем в очередь.
let mgmtQueue: Promise<unknown> = Promise.resolve()

/**
 * Одна команда management-интерфейса. Шлём её и сразу `quit`, читаем до
 * закрытия соединения; строки уведомлений (`>INFO:` и т.п.) отбрасываем.
 */
function ovpnMgmt(command: string, timeoutMs = 5_000): Promise<string[]> {
  const run = () => new Promise<string[]>((resolve, reject) => {
    const sock = net.createConnection(MGMT_SOCK)
    let buf = ''
    const timer = setTimeout(() => {
      sock.destroy()
      reject(new Error(`openvpn management: нет ответа на «${command}»`))
    }, timeoutMs)
    sock.setEncoding('utf8')
    // `quit` нельзя слать вместе с командой: OpenVPN 2.7 закрывает сокет, не
    // отдав ответ, и `status 2` приходит пустым. Ждём конец ответа — `END`
    // у многострочных, `SUCCESS:`/`ERROR:` у остальных — и только потом выходим.
    sock.on('connect', () => sock.write(`${command}\n`))
    sock.on('data', (chunk: string) => {
      buf += chunk
      const lines = buf.split(/\r?\n/).filter(l => l && !l.startsWith('>'))
      if (lines.some(l => l === 'END' || /^(SUCCESS|ERROR):/.test(l))) {
        clearTimeout(timer)
        sock.end('quit\n')
        resolve(lines)
      }
    })
    sock.on('error', (err) => {
      clearTimeout(timer)
      reject(err)
    })
    sock.on('close', () => {
      clearTimeout(timer)
      reject(new Error(`openvpn management: сокет закрыт без ответа на «${command}»`))
    })
  })
  const p = mgmtQueue.then(run, run)
  mgmtQueue = p.catch(() => {})
  return p
}

/**
 * Рвёт все установленные сессии устройства. Без этого заморозка/удаление
 * действуют только на следующее подключение, а уже подключённый клиент
 * сидит в туннеле до своего переподключения (VPN2-39). true — кого-то выбили.
 */
async function killOvpnClient(cn: string): Promise<boolean> {
  const out = await ovpnMgmt(`kill ${cn}`)
  if (out.some(l => l.startsWith('SUCCESS:'))) {
    useLogger().info({ cn }, 'ovpn: сессия разорвана')
    return true
  }
  // «ERROR: common name 'dev_N' not found» — не подключён, это норма.
  return false
}

/**
 * Подключённые сейчас клиенты: CN → адрес в туннеле (management `status 2`,
 * CLIENT_LIST,<CN>,<реальный адрес>,<адрес в туннеле>,...).
 */
async function connectedOvpnClients(): Promise<Map<string, string>> {
  const out = await ovpnMgmt('status 2')
  const clients = new Map<string, string>()
  for (const l of out) {
    if (l.startsWith('CLIENT_LIST,')) {
      const [, cn, , vaddr] = l.split(',')
      if (cn) clients.set(cn, vaddr ?? '')
    }
  }
  return clients
}

/** Рвёт сессии без падения вызывающего: сервер не поднят или старый (без management). */
export async function killOvpnClientQuiet(cn: string): Promise<void> {
  await killOvpnClient(cn).catch((err) => {
    useLogger().warn({ err: (err as Error).message, cn }, 'ovpn: kill через management не удался')
  })
}

/**
 * ccd устройства: постоянный адрес (`ifconfig-push`) и, у замороженного,
 * `disable`. Обе директивы OpenVPN читает при каждом новом подключении, так
 * что заморозка обратима без отзыва сертификата. Файл переписываем, только
 * если он изменился.
 */
async function writeCcd(cn: string, ip: string | null, disabled: boolean): Promise<void> {
  const body = [
    ip ? `ifconfig-push ${ip} 255.255.255.0` : '',
    disabled ? 'disable' : '',
  ].filter(Boolean).map(l => `${l}\n`).join('')
  const file = path.join(CCD_DIR, cn)
  if (!body) {
    await removeCcd(cn)
    return
  }
  if (await fs.readFile(file, 'utf8').catch(() => null) === body) return
  await fs.mkdir(CCD_DIR, { recursive: true })
  await fs.writeFile(file, body)
}

/** Удаление устройства: его ccd больше не нужен, адрес освобождается вместе со строкой. */
export async function removeCcd(cn: string): Promise<void> {
  await fs.rm(path.join(CCD_DIR, cn), { force: true }).catch(() => {})
}

export interface OvpnConfigParams {
  clientCert: string
  clientKey: string
  serverHost: string
  serverPort: number
  proto: string
  dns: string[]
  /** true (по умолчанию) — локалка клиента остаётся мимо туннеля. */
  splitLocal?: boolean
  /** Сети, которые всё-таки должны идти В туннель: mgmt (DNS AdGuard). */
  tunnelPrefixes?: string[]
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
    // Туннель только IPv4 (tun0 10.67.67.0/24). Загоняем весь IPv6 в tun
    // (route-ipv6 ::/1 + 8000::/1) и отбиваем его block-ipv6 — иначе IPv6
    // клиента уходит мимо туннеля напрямую (leak), а приложения падают на
    // IPv4, который идёт через VPN.
    `block-ipv6`,
    `route-ipv6 ::/1`,
    `route-ipv6 8000::/1`,
    `remote-cert-tls server`,
    `cipher AES-256-GCM`,
    `auth SHA256`,
    `verb 3`,
    ...p.dns.map(d => `dhcp-option DNS ${d}`),
    ...ovpnLocalRoutes(p.splitLocal ?? true, p.tunnelPrefixes ?? ['10.99.0.']),
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
 * Make sure the DEVICE has an OpenVPN cert. If absent, issue one and persist
 * the cert + key on the device row. Returns the (possibly updated) record.
 */
export async function ensureDeviceOvpn(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)
  if (row.ovpnCert && row.ovpnKey && row.ovpnIp) return row

  if (!row.ovpnCert || !row.ovpnKey) {
    const { cert, key } = await issueClientCert(ovpnCn(deviceId))
    const [issued] = await db
      .update(devicesTable)
      .set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
      .where(eq(devicesTable.id, deviceId))
      .returning()
    if (!issued) throw new Error(`device ${deviceId} not found`)
  }
  // Адрес и ccd — до того, как клиент получит конфиг: первое же подключение
  // идёт с постоянным адресом.
  const updated = await ensureDeviceOvpnIp(deviceId)
  const [client] = await db
    .select({ frozenManual: clientsTable.frozenManual, expiresAt: clientsTable.expiresAt })
    .from(clientsTable)
    .where(eq(clientsTable.id, row.clientId))
    .limit(1)
  await writeCcd(ovpnCn(deviceId), updated.ovpnIp, client ? !isClientActive(client) : false).catch(() => {})
  return updated
}

/** Перевыпуск OVPN-сертификата девайса: старый отзывается в CRL, выдаётся новый. */
export async function reissueDeviceOvpn(deviceId: number) {
  const db = useDb()
  const [row] = await db.select().from(devicesTable).where(eq(devicesTable.id, deviceId)).limit(1)
  if (!row) throw new Error(`device ${deviceId} not found`)

  const ready = await caReady()
  if (row.ovpnCert && ready) {
    await revokeClientCert(row.ovpnCert).catch(() => {})
    // CRL проверяется только при подключении — живую сессию со старым
    // сертификатом рвём сами.
    await killOvpnClientQuiet(ovpnCn(deviceId))
  }
  if (!ready) {
    // CA ещё не готов — просто чистим, ensureDeviceOvpn выдаст cert позже.
    const [cleared] = await db
      .update(devicesTable)
      .set({ ovpnCert: null, ovpnKey: null, updatedAt: new Date() })
      .where(eq(devicesTable.id, deviceId))
      .returning()
    return cleared
  }

  const { cert, key } = await issueClientCert(ovpnCn(deviceId))
  const [updated] = await db
    .update(devicesTable)
    .set({ ovpnCert: cert, ovpnKey: key, updatedAt: new Date() })
    .where(eq(devicesTable.id, deviceId))
    .returning()
  if (!updated) throw new Error(`device ${deviceId} not found`)
  return updated
}

/**
 * Сверка ccd с базой для всех устройств с сертификатом: постоянный адрес
 * (устройствам до VPN2-115 выдаётся здесь), флаг disable, удаление ccd
 * исчезнувших устройств. Подключённых замороженных и тех, кто сидит не на
 * своём адресе (подключился до записи ccd), выбиваем: и то и другое OpenVPN
 * применяет только при новом подключении.
 */
export async function syncOpenvpnConfig(): Promise<void> {
  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) return
  if (!(await caReady())) return

  const db = useDb()
  const rows = await db
    .select({
      id: devicesTable.id,
      ovpnCert: devicesTable.ovpnCert,
      ovpnIp: devicesTable.ovpnIp,
      frozenManual: clientsTable.frozenManual,
      expiresAt: clientsTable.expiresAt,
    })
    .from(devicesTable)
    .innerJoin(clientsTable, eq(devicesTable.clientId, clientsTable.id))

  const pinned = new Map<string, string | null>()
  const disabledCns = new Set<string>()
  for (const r of rows) {
    if (!r.ovpnCert) continue
    const cn = ovpnCn(r.id)
    let ip = r.ovpnIp
    if (!ip) {
      ip = (await ensureDeviceOvpnIp(r.id).catch((err) => {
        useLogger().warn({ err: (err as Error).message, id: r.id }, 'ovpn: адрес устройству не выдан')
        return null
      }))?.ovpnIp ?? null
    }
    const disabled = !isClientActive({ frozenManual: r.frozenManual, expiresAt: r.expiresAt })
    if (disabled) disabledCns.add(cn)
    pinned.set(cn, ip)
    await writeCcd(cn, ip, disabled).catch((err) => {
      useLogger().warn({ err: (err as Error).message, id: r.id }, 'ovpn ccd sync failed')
    })
  }

  const files = await fs.readdir(CCD_DIR).catch(() => [] as string[])
  for (const f of files) {
    if (/^dev_\d+$/.test(f) && !pinned.has(f)) await removeCcd(f)
  }

  let connected: Map<string, string>
  try {
    connected = await connectedOvpnClients()
  }
  catch (err) {
    // Сервер ещё не поднят или без management: без подключённых выбивать некого.
    if (disabledCns.size) useLogger().warn({ err: (err as Error).message }, 'ovpn: management недоступен, подключённые замороженные не выбиты')
    return
  }
  for (const [cn, vaddr] of connected) {
    const ip = pinned.get(cn)
    if (disabledCns.has(cn)) {
      await killOvpnClientQuiet(cn)
    }
    else if (ip && vaddr && vaddr !== ip) {
      useLogger().info({ cn, vaddr, ip }, 'ovpn: устройство не на своём адресе, переподключаю')
      await killOvpnClientQuiet(cn)
    }
  }
}
