import { Resolver } from 'node:dns/promises'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { wgEndpointHasAAAA } from './wg-endpoint-dns'

describe('wg-endpoint-dns', () => {
  afterEach(() => vi.restoreAllMocks())

  it('WG DNS: IP-литералы не вызывают DNS', async () => {
    const dns = vi.spyOn(Resolver.prototype, 'resolve6').mockImplementation(async () => ['2001:db8::1'])
    for (const host of ['', '203.0.113.7', '2001:db8::1', '[2001:db8::1]']) {
      expect(await wgEndpointHasAAAA(host)).toBe(false)
    }
    expect(dns).toHaveBeenCalledTimes(0)
  })

  it('WG DNS: кэш AAAA истекает через минуту, смена домена проверяется сразу', async () => {
    let now = 0
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const dns = vi.spyOn(Resolver.prototype, 'resolve6').mockImplementation(async () => ['2001:db8::1'])
    expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
    now = 59_999
    expect(await wgEndpointHasAAAA('CACHE.example.test.')).toBe(true)
    expect(dns).toHaveBeenCalledTimes(1)
    now = 60_000
    expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
    expect(dns).toHaveBeenCalledTimes(2)
    expect(await wgEndpointHasAAAA('changed.example.test')).toBe(true)
    expect(dns).toHaveBeenCalledTimes(3)
  })

  it('WG DNS: ошибка резолвера не выходит наружу и кэшируется', async () => {
    const dns = vi.spyOn(Resolver.prototype, 'resolve6').mockImplementation(async () => { throw new Error('DNS unavailable') })
    expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
    expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
    expect(dns).toHaveBeenCalledTimes(1)
  })

  it('WG DNS: зависший резолвер отменяется по дедлайну', async () => {
    vi.spyOn(Resolver.prototype, 'resolve6').mockImplementation(() => new Promise(() => {}))
    const cancel = vi.spyOn(Resolver.prototype, 'cancel').mockImplementation(() => {})
    const start = performance.now()
    expect(await wgEndpointHasAAAA('timeout.example.test')).toBe(false)
    expect(performance.now() - start).toBeLessThan(1000)
    expect(cancel).toHaveBeenCalledTimes(1)
  })
})
