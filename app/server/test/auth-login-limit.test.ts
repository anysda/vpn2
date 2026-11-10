import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loginAttempt, loginFinish } from '../utils/login-limit'

const WINDOW = 300
let n = 0
let key = ''

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  key = `10.0.0.${++n}|/sign-in/username`
})
afterEach(() => vi.useRealTimers())

/** Неудачная попытка целиком: занять место и отпустить с неудачей. */
function fail() {
  expect(loginAttempt(key, 3, WINDOW)).toBeNull()
  loginFinish(key, false)
}

describe('ограничитель входа', () => {
  it('пропускает max неудач, следующая - отказ с остатком окна', () => {
    fail()
    fail()
    fail()
    expect(loginAttempt(key, 3, WINDOW)).toBe(WINDOW)
    vi.advanceTimersByTime(100_000)
    expect(loginAttempt(key, 3, WINDOW)).toBe(200)
  })

  it('удачи не считаются: сколько угодно верных входов подряд', () => {
    for (let i = 0; i < 10; i++) {
      expect(loginAttempt(key, 3, WINDOW)).toBeNull()
      loginFinish(key, true)
    }
    fail()
    fail()
    fail()
    expect(loginAttempt(key, 3, WINDOW)).not.toBeNull()
  })

  it('незавершённые попытки занимают место: параллельная пачка не проскакивает', () => {
    expect(loginAttempt(key, 3, WINDOW)).toBeNull()
    expect(loginAttempt(key, 3, WINDOW)).toBeNull()
    expect(loginAttempt(key, 3, WINDOW)).toBeNull()
    expect(loginAttempt(key, 3, WINDOW)).toBe(WINDOW)
    loginFinish(key, true)
    expect(loginAttempt(key, 3, WINDOW)).toBeNull()
  })

  it('окно фиксировано от первой неудачи и по истечении сбрасывает счёт', () => {
    fail()
    vi.advanceTimersByTime(200_000)
    fail()
    fail()
    expect(loginAttempt(key, 3, WINDOW)).toBe(100)
    vi.advanceTimersByTime(100_000)
    fail()
    fail()
    fail()
    expect(loginAttempt(key, 3, WINDOW)).toBe(WINDOW)
  })

  it('счёт у каждого ключа свой', () => {
    fail()
    fail()
    fail()
    expect(loginAttempt(`${key}-other`, 3, WINDOW)).toBeNull()
  })
})
