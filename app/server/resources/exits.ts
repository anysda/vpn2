import type { H3Event } from 'h3'
import { z } from 'zod'
import type { ExitJob } from '../../ui/transport'

// Состав экзитов - задания менеджера установки через vpn2-control (manager.*); правила тега и адреса - его.
const Tag = z.string().regex(/^[!-~]{1,64}$/)
const Exit = z.object({ tag: Tag, host: z.string().regex(/^[!-~]{1,260}$/) })

type Refusal = { refused: string }
interface Status { deploy: 'idle' | 'busy', job: ExitJob | null, nodes: Array<{ tag: string, role: 'entry' | 'exit', host: string, monitoring: 'up' | 'down' | 'unknown' }> }

/** Ответ менеджера; отказ - 409 с его текстом, нет связи - 503 (страница опрашивает дальше). */
async function reply<T extends object>(call: Promise<T | Refusal>): Promise<T> {
  const result = await call.catch(() => {
    throw createError({ statusCode: 503, statusMessage: 'manager_unavailable', message: 'Нет связи с менеджером установки' })
  })
  if (Object.hasOwn(result, 'refused')) throw createError({ statusCode: 409, statusMessage: 'refused', message: (result as Refusal).refused })
  return result as T
}

const jobId = (event: H3Event) => recordIds(event, 'id').id

export default {
  'GET /exits': admin(async () => {
    const [status, { key }] = await Promise.all([
      reply(control<Status>('manager.status.read', {})),
      reply(control<{ key: string }>('manager.key.read', {})),
    ])
    return { ...status, key }
  }),
  'POST /exits/check': admin(async (event) => {
    const { tag, host } = await readBodyAs(event, Exit)
    return reply(control<{ problems: string[] }>('manager.exit.check', { tag, host }))
  }),
  'POST /exits': admin(async (event) => {
    const { tag, host } = await readBodyAs(event, Exit)
    return accepted(event, await reply(control<ExitJob>('manager.exit.add', { tag, host })))
  }),
  'DELETE /exits/:tag': admin(async (event) => {
    const parsed = Tag.safeParse(getRouterParam(event, 'tag'))
    if (!parsed.success) throw notFound()
    const tag = parsed.data
    return accepted(event, await reply(control<ExitJob>('manager.exit.remove', { tag })))
  }),
  'GET /exits/jobs/:id': admin(event => reply(control<ExitJob>('manager.job.read', { job: jobId(event) }))),
  'POST /exits/jobs/:id/retry': admin(event => reply(control<ExitJob>('manager.job.retry', { job: jobId(event) }))),
  'POST /exits/jobs/:id/forget': admin(event => reply(control<ExitJob>('manager.job.forget', { job: jobId(event) }))),
}
