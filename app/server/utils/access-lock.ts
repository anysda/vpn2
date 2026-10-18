import { spawn } from 'node:child_process'
import { dirname, join } from 'node:path'

/** flock отпускается ядром и при SIGKILL. Перезапуск не ждёт истечения аренды. */
export async function withAccessLock(action: () => Promise<void>): Promise<void> {
  const url = String(useRuntimeConfig().databaseUrl)
  if (!url.startsWith('file:')) throw new Error('access worker requires a local database')
  const lock = join(dirname(url.slice(5)), 'access-worker.lock')
  const child = spawn('flock', ['--exclusive', '--nonblock', lock, 'sh', '-c', 'printf ready; cat >/dev/null'], { stdio: ['pipe', 'pipe', 'pipe'] })
  child.stdin.on('error', () => {})
  const exited = new Promise<void>((resolve) => { child.once('close', () => resolve()) })
  const acquired = await new Promise<boolean>((resolve, reject) => {
    child.once('error', reject)
    child.stdout.once('data', () => resolve(true))
    child.once('close', code => code === 1 ? resolve(false) : code === 0 ? resolve(false) : reject(new Error('access lock unavailable')))
  })
  if (!acquired) return
  try { await action() }
  finally { child.stdin.end(); await exited }
}
