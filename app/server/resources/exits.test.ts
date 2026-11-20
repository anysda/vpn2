import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeEvent, installStubs } from '../test/stubs'
import { control } from '../utils/control'
import exits from './exits'

vi.hoisted(() => vi.stubGlobal('defineEventHandler', (h: unknown) => h))
vi.mock('../database/client', async () => (await import('../test/stubs')).databaseModule)
vi.mock('../utils/auth', async () => (await import('../test/stubs')).authModule)
vi.mock('../utils/control', () => ({ control: vi.fn() }))

const route = (key: keyof typeof exits, params: Record<string, string> = {}, body?: object) => {
  vi.stubGlobal('readBody', async () => body)
  return (exits[key] as (event: never) => Promise<unknown>)(fakeEvent(params))
}

beforeEach(() => {
  installStubs()
  vi.stubGlobal('setResponseStatus', () => {})
  vi.mocked(control).mockReset()
})

describe('/api/exits', () => {
  it('отказ менеджера - 409 с его фразой, без переписывания', async () => {
    vi.mocked(control).mockResolvedValue({ refused: 'идёт ./deploy.sh: дождись его конца' })
    await expect(route('POST /exits', {}, { tag: 'de', host: 'root@192.0.2.9' }))
      .rejects.toMatchObject({ statusCode: 409, message: 'идёт ./deploy.sh: дождись его конца' })
  })

  it('нет связи с менеджером - 503, страница опрашивает дальше', async () => {
    vi.mocked(control).mockRejectedValue(new Error('control: unavailable'))
    await expect(route('GET /exits/jobs/:id', { id: '3' }))
      .rejects.toMatchObject({ statusCode: 503, statusMessage: 'manager_unavailable' })
  })

  it('до менеджера доходят только тег, адрес и номер задания', async () => {
    vi.mocked(control).mockResolvedValue({ id: 4 })
    await route('POST /exits', {}, { tag: 'de', host: 'root@192.0.2.9', extra: 'x' })
    await route('DELETE /exits/:tag', { tag: 'de' })
    await route('POST /exits/jobs/:id/retry', { id: '4' })
    expect(vi.mocked(control).mock.calls).toEqual([
      ['manager.exit.add', { tag: 'de', host: 'root@192.0.2.9' }],
      ['manager.exit.remove', { tag: 'de' }],
      ['manager.job.retry', { job: 4 }],
    ])
  })

  it('мусор в теге и адресе отсекается до менеджера', async () => {
    await expect(route('DELETE /exits/:tag', { tag: 'a b' })).rejects.toMatchObject({ statusCode: 404 })
    await expect(route('POST /exits/check', {}, { tag: 'de', host: 'root@1.2.3.4; reboot' })).rejects.toMatchObject({ statusCode: 400 })
    await expect(route('GET /exits/jobs/:id', { id: 'x' })).rejects.toMatchObject({ statusCode: 400 })
    expect(control).not.toHaveBeenCalled()
  })
})
