import { inside } from './openvpn-ca'

export function proxiesBarrier(method: string) {
  return JSON.parse(inside(`
    const response = await fetch('http://127.0.0.1:9090/__test/proxies', { method: JSON.parse(process.argv[1]) });
    console.log(await response.text());
  `, method)) as { pending: number }
}
