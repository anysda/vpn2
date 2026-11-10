import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clientStatus, expiryForControl, isClientActive } from './client-status'

const NOW = new Date('2026-06-15T12:00:00.000Z')

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => vi.useRealTimers())

const at = (offsetMs: number) => new Date(NOW.getTime() + offsetMs)

describe('isClientActive', () => {
  it('бессрочный и не заморожен - активен', () => {
    expect(isClientActive({ frozenManual: false, expiresAt: null })).toBe(true)
  })

  it('заморожен вручную - неактивен при любом сроке, в том числе бессрочном', () => {
    expect(isClientActive({ frozenManual: true, expiresAt: null })).toBe(false)
    expect(isClientActive({ frozenManual: true, expiresAt: at(86_400_000) })).toBe(false)
    expect(isClientActive({ frozenManual: true, expiresAt: at(-1000) })).toBe(false)
  })

  it('граница срока: секунда до - активен, ровно момент истечения и секунда после - нет', () => {
    expect(isClientActive({ frozenManual: false, expiresAt: at(1000) })).toBe(true)
    expect(isClientActive({ frozenManual: false, expiresAt: at(1) })).toBe(true)
    expect(isClientActive({ frozenManual: false, expiresAt: at(0) })).toBe(false)
    expect(isClientActive({ frozenManual: false, expiresAt: at(-1) })).toBe(false)
    expect(isClientActive({ frozenManual: false, expiresAt: at(-1000) })).toBe(false)
  })

  it('время идёт: клиент становится неактивным, когда часы доходят до срока', () => {
    const c = { frozenManual: false, expiresAt: at(5000) }
    expect(isClientActive(c)).toBe(true)
    vi.advanceTimersByTime(4999)
    expect(isClientActive(c)).toBe(true)
    vi.advanceTimersByTime(1)
    expect(isClientActive(c)).toBe(false)
  })

  it('продление срока возвращает в активные', () => {
    expect(isClientActive({ frozenManual: false, expiresAt: at(-1000) })).toBe(false)
    expect(isClientActive({ frozenManual: false, expiresAt: at(86_400_000) })).toBe(true)
  })

  it('дата 1970 (нулевой timestamp) - срок истёк, не бессрочный', () => {
    expect(isClientActive({ frozenManual: false, expiresAt: new Date(0) })).toBe(false)
  })
})

describe('clientStatus', () => {
  it('active / frozen', () => {
    expect(clientStatus({ frozenManual: false, expiresAt: null })).toBe('active')
    expect(clientStatus({ frozenManual: false, expiresAt: at(1) })).toBe('active')
    expect(clientStatus({ frozenManual: false, expiresAt: at(0) })).toBe('frozen')
    expect(clientStatus({ frozenManual: true, expiresAt: null })).toBe('frozen')
  })
})

describe('expiryForControl', () => {
  it('бессрочный - null; срок - ISO в UTC с миллисекундами', () => {
    expect(expiryForControl(null)).toBeNull()
    expect(expiryForControl(new Date('2026-06-16T00:00:00Z'))).toBe('2026-06-16T00:00:00.000Z')
    expect(expiryForControl(new Date(Date.UTC(2026, 0, 1, 23, 59, 59, 999)))).toBe('2026-01-01T23:59:59.999Z')
  })

  it('не зависит от часового пояса процесса: строка заканчивается на Z', () => {
    expect(expiryForControl(new Date(86_400_000))).toBe('1970-01-02T00:00:00.000Z')
  })
})
