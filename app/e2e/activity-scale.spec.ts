import { test, expect } from '@playwright/test'
import { serverModule } from './support/server-module'

// Ступени сетки «Активность» на панелях разного размера: 3, 18 и 300 клиентов,
// 120 показанных дней, первые 10 — до установки (нули). Данные детерминированы.
const { activityScale } = serverModule<typeof import('../app/utils/activity-scale')>('app/utils/activity-scale.ts', {})

function rng(seed: number) {
  return () => {
    seed = (seed + 0x6D2B79F5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function panel(clients: number) {
  const r = rng(clients)
  const online: number[] = [], bytes: number[] = []
  for (let day = 0; day < 120; day++) {
    if (day < 10) { online.push(0); bytes.push(0); continue }
    const p = day % 7 >= 5 ? 0.8 : 0.6
    let n = 0, b = 0
    for (let c = 0; c < clients; c++) {
      if (r() >= p) continue
      n++
      b += 1.5e9 * Math.exp(r() * 2 - 1)
    }
    if (day === 100) b *= 3 // один день с большим скачиванием
    online.push(n); bytes.push(Math.round(b))
  }
  return { online, bytes }
}

for (const clients of [3, 18, 300]) {
  const data = panel(clients)
  for (const [mode, values] of [['Онлайн', data.online], ['Трафик', data.bytes]] as const) {
    test(`${clients} клиентов, ${mode}: дни расходятся по всем ступеням`, () => {
      const step = activityScale(values)
      const count = [0, 0, 0, 0, 0]
      for (const v of values) count[step(v)]!++
      const nonzero = values.filter(v => v > 0)
      console.log(`${clients} ${mode}: ${count.join(' / ')}`)

      expect(values.filter(v => v === 0).every(v => step(v) === 0)).toBe(true)
      expect(nonzero.every(v => step(v) > 0)).toBe(true)
      expect(step(Math.max(...values))).toBe(4)
      const sorted = [...values].sort((a, b) => a - b)
      for (let i = 1; i < sorted.length; i++) expect(step(sorted[i]!)).toBeGreaterThanOrEqual(step(sorted[i - 1]!))

      // ступеней не больше, чем различимых значений; иначе задействованы все
      const distinct = new Set(nonzero).size
      const used = count.slice(1).filter(n => n > 0).length
      expect(used).toBe(Math.min(4, distinct))
      // ни одна ступень не забирает больше половины ненулевых дней
      expect(Math.max(...count.slice(1))).toBeLessThanOrEqual(nonzero.length / 2)
    })
  }
}
