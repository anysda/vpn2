import { execFileSync } from 'node:child_process'
import { container } from './openvpn-ca'

const docker = (process.env.DOCKER || 'docker').split(/\s+/)

export function proxiesBarrier(method: string, mutation?: { clientName?: string, device?: { clientId: number, name: string } }) {
  // Не тратим дедлайн заблокированного запроса на запуск Node/undici при
  // каждом опросе и освобождении барьера. curl уже есть в образе панели.
  const body = mutation ? ['-H', 'content-type: application/json', '--data', JSON.stringify(mutation)] : []
  return JSON.parse(execFileSync(docker[0]!, [...docker.slice(1), 'exec', container!,
    'curl', '-fsS', '--max-time', '10', '-X', method, ...body, 'http://127.0.0.1:9090/__test/proxies',
  ], { encoding: 'utf8' })) as { insertedId: number | null }
}
