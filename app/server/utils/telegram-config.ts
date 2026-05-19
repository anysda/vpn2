import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { createConnection } from 'node:net'

const RUNTIME_PATH = '/etc/anysda/telegram-runtime.json'

export interface TelegramRuntime {
  bot_token: string
  chat_id: string | number | ''
}

export function readTelegramRuntime(): TelegramRuntime {
  try {
    if (!existsSync(RUNTIME_PATH)) return { bot_token: '', chat_id: '' }
    const raw = readFileSync(RUNTIME_PATH, 'utf-8')
    const parsed = JSON.parse(raw)
    return {
      bot_token: String(parsed.bot_token ?? ''),
      chat_id: parsed.chat_id ?? '',
    }
  }
  catch (err) {
    useLogger().warn({ err }, 'failed to read telegram-runtime.json')
    return { bot_token: '', chat_id: '' }
  }
}

export function writeTelegramRuntime(data: TelegramRuntime) {
  const dir = dirname(RUNTIME_PATH)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  writeFileSync(RUNTIME_PATH, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
}

export function maskToken(token: string): string {
  if (!token) return ''
  if (token.length < 10) return '••••••'
  return `${token.slice(0, 6)}..${token.slice(-4)}`
}

export async function botRunning(): Promise<boolean> {
  const port = Number(useRuntimeConfig().tgbotEventPort ?? 8877)
  return new Promise((resolve) => {
    const sock = createConnection({ host: '127.0.0.1', port, timeout: 500 })
    sock.once('connect', () => { sock.destroy(); resolve(true) })
    sock.once('error', () => resolve(false))
    sock.once('timeout', () => { sock.destroy(); resolve(false) })
  })
}
