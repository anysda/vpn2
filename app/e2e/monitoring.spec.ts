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
})
