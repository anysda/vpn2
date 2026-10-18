import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { serverModule } from './support/server-module'

const infra = resolve(process.env.E2E_SERVER_ROOT || '.', '../infra/control')

// Настоящий serve() из infra/control/server.py: соединения по одному, очередь
// listen как Backlog в vpn2-control.socket. Ответ занимает 20 мс, как чтение
// настроек на нагруженной машине.
const SERVICE = `
import os, socket, sys, time
sys.path.insert(0, sys.argv[1])
from server import serve

class Backend:
    def execute(self, method, params):
        time.sleep(0.02)
        return {'config': {'disabled': []}}

with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(sys.argv[2])
    listener.listen(int(sys.argv[3]))
    print('ready', flush=True)
    serve(listener, Backend(), {os.getuid()}, {})
`

test('всплеск запросов к сервису управления сверх очереди listen не теряется', async () => {
  const backlog = Number(/^Backlog=(\d+)$/m.exec(readFileSync(join(infra, 'vpn2-control.socket'), 'utf8'))![1])
  const directory = mkdtempSync(join(tmpdir(), 'vpn2-control-'))
  const path = join(directory, 'control.sock')
  const service = spawn('python3', ['-c', SERVICE, infra, path, String(backlog)], { stdio: ['ignore', 'pipe', 'inherit'] })
  try {
    await once(service.stdout, 'data')
    const { control } = serverModule<typeof import('../server/utils/control')>('server/utils/control.ts', {
      'drizzle-orm': {},
      '../database/client': {},
      '../database/schema': {},
    }, { process: { env: { VPN2_CONTROL_SOCKET: path } }, setTimeout, clearTimeout, Buffer })
    // Тридцать одновременных PATCH экзитов — тридцать чтений сразу; берём втрое больше очереди.
    const calls = await Promise.allSettled(Array.from({ length: backlog * 3 }, () =>
      control('settings.read', { resource: 'disabled-exits' })))
    const failed = calls.filter(call => call.status === 'rejected').length
    console.log(`очередь listen ${backlog}, запросов ${calls.length}, отказов ${failed}`)
    expect(failed).toBe(0)
  }
  finally {
    service.kill()
    rmSync(directory, { recursive: true, force: true })
  }
})
