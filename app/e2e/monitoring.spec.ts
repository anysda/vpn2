import type { Page } from '@playwright/test'
import { expect, NODES, test } from './support/fixtures'
import { openHome } from './support/clients'

/** Плитка ноды: рамка с заголовком «<флаг> TAG» и строками CPU/RAM. */
function nodeTile(page: Page, tag: string) {
  const title = page.locator('span.font-semibold', { hasText: new RegExp(`\\s${tag.toUpperCase()}$`) })
  return page.locator('div.relative.rounded-md').filter({ has: title }).filter({ hasText: 'CPU' })
}

test.describe('мониторинг нод', () => {
  test('все ноды на месте, живые, с метриками и спарклайном', async ({ page }) => {
    await openHome(page)
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    for (const tag of NODES) {
      const node = nodeTile(page, tag)
      await expect(node, `нода ${tag}`).toHaveCount(1)
      await expect(node).not.toContainText('ОФФЛАЙН')
      await expect(node).not.toHaveClass(/node-warn/)
      await expect(node).toContainText(/CPU\s*\d+(?:[.,]\d+)?\s*%/)
      await expect(node).toContainText(/RAM\s*\d+(?:[.,]\d+)?\s*%/)
      await expect(node).toContainText('Mbps')
    }
    // Спарклайн CPU появляется со второй точки истории (опрос раз в несколько секунд).
    // Плоская линия даёт path нулевой высоты, поэтому видимость — у svg.
    const spark = nodeTile(page, NODES[0]!).locator('svg')
    await expect(spark).toBeVisible({ timeout: 30_000 })
    await expect(spark.locator('path')).toHaveAttribute('d', /^M[\d.]+[ ,][\d.]+\s*L/)
  })

  test('раскладка дорожек по экзитам: полос N (warp M)', async ({ page }) => {
    await openHome(page)
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    // раскладка подставного clash API (e2e/support/fake-backends.mjs)
    const exits = NODES.filter(t => t !== 'ru')
    for (const [k, tag] of exits.entries()) {
      const lanes = Array.from({ length: 32 }, (_, i) => i).filter(i => i % exits.length === k)
      const warp = lanes.filter(i => i % 7 === 0).length
      await expect(nodeTile(page, tag), `нода ${tag}`).toContainText(new RegExp(`полос\\s*${lanes.length}\\s*\\(warp ${warp}\\)`))
    }
    // у RU дорожек нет — строки нет
    if (NODES.includes('ru')) await expect(nodeTile(page, 'ru')).not.toContainText('полос')
  })

  test('параллельные переключения экзитов не теряются и не падают', async ({ page }) => {
    const exits = NODES.filter(t => t !== 'ru')
    test.skip(exits.length < 3, 'нужно три экзита')
    const [off, on] = exits as [string, string]
    await openHome(page)
    // Порядок обработки не важен: off выключается, on включается, третий
    // экзит включён всегда — 409 «последний включённый» невозможен.
    const res = await Promise.all(Array.from({ length: 30 }, (_, i) =>
      page.request.patch(`/api/ops/exits/${i % 2 ? on : off}`, { data: { disabled: !(i % 2) } })))
    expect(res.map(r => r.status())).toEqual(Array.from({ length: 30 }, () => 200))
    const nodes = await (await page.request.get('/api/ops/nodes')).json() as Array<{ tag: string, disabled: boolean }>
    expect(nodes.filter(n => n.disabled).map(n => n.tag)).toEqual([off])
    const back = await page.request.patch(`/api/ops/exits/${off}`, { data: { disabled: false } })
    expect(back.status()).toBe(200)
  })
})
