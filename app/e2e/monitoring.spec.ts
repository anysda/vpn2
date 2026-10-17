import type { Page } from '@playwright/test'
import { expect, NODES, test } from './support/fixtures'
import { openHome } from './support/clients'

/** Карточка узла на Обзоре. */
function nodeCard(page: Page, tag: string) {
  return page.locator(`.ov-nodes .node[data-tag="${tag}"]`)
}

test.describe('мониторинг нод', () => {
  test('все ноды на месте, живые, с метриками и спарклайном', async ({ page }) => {
    await openHome(page)
    for (const tag of NODES) {
      const node = nodeCard(page, tag)
      await expect(node, `нода ${tag}`).toHaveCount(1)
      await expect(node.locator('.ft .tag')).not.toHaveText('Офлайн')
      await expect(node.locator('.cpu .big')).toHaveText(/^\d+%$/)
      await expect(node.locator('.ft .m')).toContainText(/RAM \d+%/)
    }
    // Спарклайн CPU: начало из VM, дальше точка на каждый опрос.
    const spark = nodeCard(page, NODES[0]!).locator('.sp svg path').last()
    await expect(spark).toHaveAttribute('d', /^M-?[\d.]+[ ,]-?[\d.]+\s*[LC]/, { timeout: 30_000 })
  })

  test('раскладка дорожек по экзитам: в окне узла «Полос N», «warp M»', async ({ page }) => {
    await openHome(page)
    // раскладка подставного clash API (e2e/support/fake-backends.mjs)
    const exits = NODES.filter(t => t !== 'ru')
    for (const [k, tag] of exits.entries()) {
      const lanes = Array.from({ length: 32 }, (_, i) => i).filter(i => i % exits.length === k)
      const warp = lanes.filter(i => i % 7 === 0).length
      await nodeCard(page, tag).click()
      const cell = page.getByRole('dialog').locator('.mgrid > div', { hasText: 'Полос' })
      await expect(cell.locator('b'), `нода ${tag}`).toHaveText(String(lanes.length))
      await expect(cell.locator('small')).toHaveText(`warp ${warp}`)
      await page.keyboard.press('Escape')
      await expect(page.getByRole('dialog')).toHaveCount(0)
    }
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
