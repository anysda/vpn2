import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { parseBootstrap, upstreamLines } from '../../../utils/dns-settings'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ upstreams: z.string().max(10_000), bootstrap: z.string().max(2000) })

/** Проверить серверы выше: AdGuard спрашивает каждый и отвечает OK или текст ошибки. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const b = await readBodyAs(event, Body)
  const list = upstreamLines(b.upstreams)
  if (!list.length) throw createError({ statusCode: 422, statusMessage: 'bad_setting', message: 'Нужен хотя бы один сервер выше' })
  const boot = parseBootstrap(b.bootstrap)
  const res = await agh<Record<string, string>>('test_upstream_dns', {
    method: 'POST',
    body: { upstream_dns: list, bootstrap_dns: 'list' in boot ? boot.list : [], fallback_dns: [], private_upstream: [] },
    timeout: 20_000,
  })
  const failed = Object.entries(res).filter(([, v]) => v !== 'OK').map(([k]) => k)
  return { total: Object.keys(res).length, failed }
})
