import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test, expect } from '@playwright/test'

// Nuxt и Nitro подставляют экспорты utils/composables без импорта. Два экспорта
// с одним именем Nuxt не считает ошибкой: берёт один, второй молча теряется,
// и чужой экран получает чужую функцию.
const root = new URL('..', import.meta.url).pathname

function names(dirs: string[]): Map<string, string[]> {
  const seen = new Map<string, string[]>()
  for (const d of dirs) {
    for (const f of readdirSync(join(root, d), { recursive: true, encoding: 'utf8' })) {
      if (!/\.[cm]?[jt]s$/.test(f)) continue
      const src = readFileSync(join(root, d, f), 'utf8')
      for (const m of src.matchAll(/^export (?:async )?(?:function\*?|const|let|class) ([\w$]+)/gm)) {
        seen.set(m[1]!, [...seen.get(m[1]!) ?? [], `${d}/${f}`])
      }
    }
  }
  return seen
}

const dupes = (m: Map<string, string[]>) => [...m].filter(([, at]) => at.length > 1)

test('автоимпорт страниц: имя экспорта из app/utils и app/composables одно на всё', () => {
  expect(dupes(names(['app/utils', 'app/composables']))).toEqual([])
})

test('автоимпорт сервера: имя экспорта из server/utils одно на всё', () => {
  expect(dupes(names(['server/utils']))).toEqual([])
})
