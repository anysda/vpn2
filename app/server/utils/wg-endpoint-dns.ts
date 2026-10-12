import { Resolver } from 'node:dns/promises'
import { isIP } from 'node:net'

// Endpoint один на панель. Кэшируем и ошибки; параллельные открытия диалога
// используют тот же запрос. Смена домена сразу сбрасывает кэш.
let cached: { host: string, expires: number, result: Promise<boolean> } | undefined

export function wgEndpointHasAAAA(endpoint: string): Promise<boolean> {
  const host = endpoint.trim().toLowerCase().replace(/\.$/, '')
  if (!host || isIP(host.replace(/^\[(.*)\]$/, '$1'))) return Promise.resolve(false)
  if (cached?.host === host && cached.expires > Date.now()) return cached.result

  const resolver = new Resolver({ timeout: 250, tries: 1 })
  // Явный дедлайн ограничивает всю проверку, включая несколько DNS-серверов.
  const result = new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => {
      resolve(false)
      resolver.cancel()
    }, 250)
    resolver.resolve6(host)
      .then(addresses => resolve(addresses.length > 0), () => resolve(false))
      .finally(() => clearTimeout(timer))
  })
  cached = { host, expires: Date.now() + 60_000, result }
  return result
}
