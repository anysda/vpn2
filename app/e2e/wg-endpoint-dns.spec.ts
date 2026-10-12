import { Resolver } from 'node:dns/promises'
import { mock } from 'node:test'
import { expect, test } from '@playwright/test'
import { wgEndpointHasAAAA } from '../server/utils/wg-endpoint-dns'

test.afterEach(() => mock.restoreAll())

test('WG DNS: IP-литералы не вызывают DNS', async () => {
  const dns = mock.method(Resolver.prototype, 'resolve6', async () => ['2001:db8::1'])
  for (const host of ['', '203.0.113.7', '2001:db8::1', '[2001:db8::1]']) {
    expect(await wgEndpointHasAAAA(host)).toBe(false)
  }
  expect(dns.mock.callCount()).toBe(0)
})

test('WG DNS: кэш AAAA истекает через минуту, смена домена проверяется сразу', async () => {
  let now = 0
  mock.method(Date, 'now', () => now)
  const dns = mock.method(Resolver.prototype, 'resolve6', async () => ['2001:db8::1'])
  expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
  now = 59_999
  expect(await wgEndpointHasAAAA('CACHE.example.test.')).toBe(true)
  expect(dns.mock.callCount()).toBe(1)
  now = 60_000
  expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
  expect(dns.mock.callCount()).toBe(2)
  expect(await wgEndpointHasAAAA('changed.example.test')).toBe(true)
  expect(dns.mock.callCount()).toBe(3)
})

test('WG DNS: ошибка резолвера не выходит наружу и кэшируется', async () => {
  const dns = mock.method(Resolver.prototype, 'resolve6', async () => { throw new Error('DNS unavailable') })
  expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
  expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
  expect(dns.mock.callCount()).toBe(1)
})

test('WG DNS: зависший резолвер отменяется по дедлайну', async () => {
  mock.method(Resolver.prototype, 'resolve6', () => new Promise(() => {}))
  const cancel = mock.method(Resolver.prototype, 'cancel', () => {})
  const start = performance.now()
  expect(await wgEndpointHasAAAA('timeout.example.test')).toBe(false)
  expect(performance.now() - start).toBeLessThan(1000)
  expect(cancel.mock.callCount()).toBe(1)
})
