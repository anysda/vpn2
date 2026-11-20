import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Refused } from '../ui/transport'
import { call, expired, offlineSince, readState, transport } from './api'

const reply = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status })

describe('транспорт мастера', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    expired.value = false
    offlineSince.value = null
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('обрыв связи и 502-504 повторяет тихо, помня, с какого момента нет связи', async () => {
    const fetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(reply(503))
      .mockResolvedValueOnce(reply(200, { ok: 1 }))
    vi.stubGlobal('fetch', fetch)
    const result = call('GET', '/api/job')
    await vi.advanceTimersByTimeAsync(1000)
    expect(offlineSince.value).not.toBeNull()
    await vi.advanceTimersByTimeAsync(1000)
    await expect(result).resolves.toEqual({ ok: 1 })
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(offlineSince.value).toBeNull()
  })

  it('401 - ссылка устарела, отказ - текст сервера как есть', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(reply(409, { message: 'Тег «de» уже занят другим экзитом' }))
      .mockResolvedValueOnce(reply(401, { message: 'нет сессии' })))
    await expect(call('PUT', '/api/draft', { exits: [] })).rejects.toEqual(new Refused('Тег «de» уже занят другим экзитом'))
    await expect(call('GET', '/api/state')).rejects.not.toBeInstanceOf(Refused)
    expect(expired.value).toBe(true)
  })

  it('«Забыть» установку не предлагается ни из состояния, ни из опроса задания', async () => {
    const job = { id: 1, state: 'interrupted', actions: ['retry', 'forget'] }
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(reply(200, { phase: 'installing', job }))
      .mockResolvedValueOnce(reply(200, job)))
    expect((await readState()).job?.actions).toEqual(['retry'])
    expect((await transport.job(1)).actions).toEqual(['retry'])
  })
})
