import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

const RUNTIME_PATH = '/var/lib/anysda-vpn2/runtime/telegram-runtime.json'

export interface TelegramRuntime {
  bot_token: string
  chat_id: string | number | ''
  // Никнейм администратора бота (без @) — бот указывает его людям без доступа.
  admin_username: string
}

export function readTelegramRuntime(): TelegramRuntime {
  try {
    if (!existsSync(RUNTIME_PATH)) return { bot_token: '', chat_id: '', admin_username: '' }
    const raw = readFileSync(RUNTIME_PATH, 'utf-8')
    const parsed = JSON.parse(raw)
    return {
      bot_token: String(parsed.bot_token ?? ''),
      chat_id: parsed.chat_id ?? '',
      admin_username: String(parsed.admin_username ?? ''),
    }
  }
  catch (err) {
    useLogger().warn({ err }, 'failed to read telegram-runtime.json')
    return { bot_token: '', chat_id: '', admin_username: '' }
  }
}

export function writeTelegramRuntime(data: TelegramRuntime) {
  const dir = dirname(RUNTIME_PATH)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  writeFileSync(RUNTIME_PATH, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 })
}

// Настроен = есть кому доставлять. Секрет панели стадия бота выдаёт при любом
// деплое; токен и чат из настроек — то же условие, что у значка «НЕНАСТРОЕН».
export function botConfigured(): boolean {
  const runtime = readTelegramRuntime()
  return !!useRuntimeConfig().tgbotSecret && !!runtime.bot_token && !!runtime.chat_id
}

export function maskToken(token: string): string {
  if (!token) return ''
  if (token.length < 10) return '••••••'
  return `${token.slice(0, 6)}..${token.slice(-4)}`
}

// Жив = /health бота ответил 200, как у docker HEALTHCHECK. Голого TCP-connect
// мало: с отвергнутым токеном бот ждёт перезапуска, его порт остаётся в LISTEN
// без обработчика, ядро принимает соединение, и панель писала «ОНЛАЙН».
// Имя бот узнаёт у Telegram (getMe) и отдаёт в том же ответе.
export async function botHealth(): Promise<{ running: boolean, username: string }> {
  const port = Number(useRuntimeConfig().tgbotEventPort ?? 8877)
  try {
    const res = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1000) })
    if (!res.ok) return { running: false, username: '' }
    const body = await res.json().catch(() => ({})) as { username?: unknown }
    return { running: true, username: typeof body.username === 'string' ? body.username : '' }
  }
  catch {
    return { running: false, username: '' }
  }
}
