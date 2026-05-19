import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { eq } from 'drizzle-orm'
import { dump as dumpYaml } from 'js-yaml'
import { useDb } from '../database/client'
import { clients } from '../database/schema'

const OUTLINE_BIN_PATH = '/usr/local/bin/outline-ss-server'

function findOutlinePid(): number | null {
  try {
    for (const entry of readdirSync('/proc')) {
      if (!/^\d+$/.test(entry)) continue
      try {
        const cmdline = readFileSync(`/proc/${entry}/cmdline`, 'utf-8')
        if (cmdline.startsWith(OUTLINE_BIN_PATH)) return Number(entry)
      }
      catch {
        // process exited or /proc/PID/cmdline unreadable
      }
    }
  }
  catch {
    // /proc not mounted, e.g. in dev
  }
  return null
}

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

  // SIGHUP outline-ss-server for hot-reload. We can't use pkill -f here
  // (docker exec returns 255 even with --pid host), so we scan /proc/<pid>/cmdline
  // ourselves and process.kill the matching PID.
  const pid = findOutlinePid()
  if (pid) {
    try {
      process.kill(pid, 'SIGHUP')
      log.info({ pid }, 'sent SIGHUP to outline-ss-server')
    }
    catch (err) {
      log.warn({ err, pid }, 'failed to send SIGHUP')
    }
  }
  else {
    log.debug('outline-ss-server process not found, SIGHUP skipped')
  }

  log.info({ count: keys.length, path }, 'shadowsocks config synced')
  return { count: keys.length, path }
}
