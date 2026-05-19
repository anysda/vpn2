import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { execSync } from 'node:child_process'
import { eq } from 'drizzle-orm'
import { dump as dumpYaml } from 'js-yaml'
import { useDb } from '../database/client'
import { clients } from '../database/schema'

interface OutlineSsKey {
  id: string
  port: number
  cipher: string
  secret: string
}

export function generateSsSecret(): string {
  return randomBytes(32).toString('base64')
}

export async function syncShadowsocksConfig(): Promise<{ count: number, path: string }> {
  const cfg = useRuntimeConfig()
  const log = useLogger()
  const db = useDb()

  const enabled = await db.select().from(clients).where(eq(clients.enabled, true))

  const keys: OutlineSsKey[] = enabled.map(c => ({
    id: `client_${c.id}`,
    port: Number(cfg.ssPort),
    cipher: c.cipher,
    secret: c.ssSecret,
  }))

  const path = cfg.ssConfigPath
  const yamlBody = dumpYaml({ keys })

  try {
    const dir = dirname(path)
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    writeFileSync(path, yamlBody, { mode: 0o600 })
  }
  catch (err) {
    log.error({ err, path }, 'failed to write outline-ss-server config')
    throw err
  }

  // SIGHUP outline-ss-server for hot-reload (silent if not running yet)
  try {
    execSync('pkill -SIGHUP -x outline-ss-server', { stdio: 'ignore' })
  }
  catch {
    log.debug('outline-ss-server not running yet, SIGHUP skipped')
  }

  log.info({ count: keys.length, path }, 'shadowsocks config synced')
  return { count: keys.length, path }
}
