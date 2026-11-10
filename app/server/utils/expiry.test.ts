import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { minExpiryMs } from './expiry'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

const at = (iso: string) => vi.setSystemTime(new Date(iso))

describe('minExpiryMs', () => {
  it('начало завтрашнего дня по UTC: посреди дня', () => {
    at('2026-06-15T12:34:56.789Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('ровно полночь UTC - всё равно следующая полночь', () => {
    at('2026-06-15T00:00:00.000Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('последняя миллисекунда дня - полночь через 1 мс', () => {
    at('2026-06-15T23:59:59.999Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00.000Z'))
  })

  it('переход через границу месяца и года', () => {
    at('2026-01-31T10:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-02-01T00:00:00Z'))
    at('2026-12-31T23:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2027-01-01T00:00:00Z'))
  })

  it('29 февраля високосного и непросекосного года', () => {
    at('2028-02-28T05:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2028-02-29T00:00:00Z'))
    at('2027-02-28T05:00:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2027-03-01T00:00:00Z'))
  })

  it('зона процесса не влияет: решает UTC, а не локальная дата', () => {
    // 23:30 UTC 15-го - для UTC+3 уже 16-е, завтра по UTC всё равно 16-е
    at('2026-06-15T23:30:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-16T00:00:00Z'))
    at('2026-06-16T00:30:00Z')
    expect(minExpiryMs()).toBe(Date.parse('2026-06-17T00:00:00Z'))
  })

  it('результат всегда больше «сейчас» и в пределах суток', () => {
    for (const iso of ['2026-03-29T00:00:00Z', '2026-10-25T01:59:59Z', '2026-06-15T12:00:00Z']) {
      at(iso)
      const diff = minExpiryMs() - Date.now()
      expect(diff).toBeGreaterThan(0)
      expect(diff).toBeLessThanOrEqual(86_400_000)
    }
  })
})
