import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { parseBlock, parseBootstrap, parseRatelimit, upstreamError, upstreamLines, type SettingsView } from '../../../utils/dns-settings'
import { settingsState, viewOf } from '../../../utils/dns-settings-read'
import { readBodyAs } from '../../../utils/validate'

const HOUR = 3_600_000
const LOG_HOURS = [24, 168, 2160]
const STATS_HOURS = [24, 168, 720]

// Одно поле за раз — как в макете: переключатель, сегмент или «Изменить».
const Body = z.union([
  z.object({ upstreams: z.string().max(10_000) }),
  z.object({ mode: z.enum(['load_balance', 'parallel', 'fastest_addr']) }),
  z.object({ bootstrap: z.string().max(2000) }),
  z.object({ safebrowsing: z.boolean() }),
  z.object({ parental: z.boolean() }),
  z.object({ safesearch: z.boolean() }),
  z.object({ dnssec: z.boolean() }),
  z.object({ noAaaa: z.boolean() }),
  z.object({ optimistic: z.boolean() }),
  z.object({ ratelimit: z.string().max(100) }),
  z.object({ block: z.string().max(100) }),
  z.object({ logHours: z.number().int() }),
  z.object({ statsHours: z.number().int() }),
  z.object({ anonymize: z.boolean() }),
])

const bad = (message: string) => createError({ statusCode: 422, statusMessage: 'bad_setting', message })

/** Изменить одну настройку DNS и вернуть, что AdGuard показывает после. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const b = await readBodyAs(event, Body) as Partial<SettingsView> & { logHours?: number, statsHours?: number }
  const cur = await settingsState()
  const dns = (body: Record<string, unknown>) => agh('dns_config', { method: 'POST', body })

  if (b.upstreams !== undefined) {
    const list = upstreamLines(b.upstreams)
    if (!list.length) throw bad('Нужен хотя бы один сервер выше')
    try {
      await dns({ upstream_dns: list })
    }
    catch (e) {
      const x = e as { statusMessage?: string, message?: string }
      if (x.statusMessage !== 'agh_rejected') throw e
      throw bad(upstreamError(x.message ?? ''))
    }
  }
  else if (b.mode) await dns({ upstream_mode: b.mode })
  else if (b.bootstrap !== undefined) {
    const r = parseBootstrap(b.bootstrap)
    if ('error' in r) throw bad(r.error)
    await dns({ bootstrap_dns: r.list })
  }
  else if (b.safebrowsing !== undefined) await agh(`safebrowsing/${b.safebrowsing ? 'enable' : 'disable'}`, { method: 'POST' })
  else if (b.parental !== undefined) await agh(`parental/${b.parental ? 'enable' : 'disable'}`, { method: 'POST' })
  else if (b.safesearch !== undefined) await agh('safesearch/settings', { method: 'PUT', body: { ...cur.ss, enabled: b.safesearch } })
  else if (b.dnssec !== undefined) await dns({ dnssec_enabled: b.dnssec })
  else if (b.noAaaa !== undefined) await dns({ disable_ipv6: b.noAaaa })
  else if (b.optimistic !== undefined) await dns({ cache_optimistic: b.optimistic })
  else if (b.ratelimit !== undefined) {
    const n = parseRatelimit(b.ratelimit)
    if (n === null) throw bad('Лимит запросов: число в секунду или «без лимита»')
    await dns({ ratelimit: n })
  }
  else if (b.block !== undefined) {
    const r = parseBlock(b.block, cur.info)
    if ('error' in r) throw bad(r.error)
    if (r.set) await dns(r.set as Record<string, unknown>)
  }
  else if (b.logHours !== undefined) {
    if (!LOG_HOURS.includes(b.logHours)) throw bad('Журнал хранится 1, 7 или 90 дней')
    await agh('querylog/config/update', { method: 'PUT', body: { ...cur.log, interval: b.logHours * HOUR } })
  }
  else if (b.statsHours !== undefined) {
    if (!STATS_HOURS.includes(b.statsHours)) throw bad('Статистика хранится 24 часа, 7 или 30 дней')
    await agh('stats/config/update', { method: 'PUT', body: { ...cur.stats, interval: b.statsHours * HOUR } })
  }
  else if (b.anonymize !== undefined) await agh('querylog/config/update', { method: 'PUT', body: { ...cur.log, anonymize_client_ip: b.anonymize } })

  const view = viewOf(await settingsState())
  const expect: Record<string, unknown> = { ...b }
  if (b.upstreams !== undefined) expect.upstreams = upstreamLines(b.upstreams).join('\n')
  if (b.bootstrap !== undefined) expect.bootstrap = (parseBootstrap(b.bootstrap) as { list: string[] }).list.join(', ')
  if (b.ratelimit !== undefined) expect.ratelimit = undefined
  if (b.block !== undefined) expect.block = undefined
  const miss = Object.entries(expect).find(([k, v]) => v !== undefined && view[k as keyof SettingsView] !== v)
  if (miss) throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение настройки' })
  return view
})
