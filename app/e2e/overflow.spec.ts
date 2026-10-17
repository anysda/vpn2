import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { noHorizontalOverflow } from './support/overflow'

// Сам помощник: обрезку внутри карточки он видит, документ при этом не шире окна.
async function card(page: Page, style: string, inner = '<div class="wide" style="width:640px;height:20px"></div>') {
  await page.setViewportSize({ width: 402, height: 874 })
  await page.setContent(`<!doctype html><body style="margin:0">
    <div class="card" style="width:300px;${style}">${inner}</div></body>`)
}

test.describe('noHorizontalOverflow', () => {
  test('блок шире карточки с overflow: hidden — провал', async ({ page }) => {
    await card(page, 'overflow:hidden')
    await expect(noHorizontalOverflow(page, 'обрезка')).rejects.toThrow(/div\.wide right=640 > div\.card 300/)
  })

  test('блок шире карточки с прокруткой внутри — провал', async ({ page }) => {
    await card(page, 'overflow-y:auto;max-height:200px')
    await expect(noHorizontalOverflow(page, 'прокрутка')).rejects.toThrow(/div\.wide right=640 > div\.card 300/)
  })

  test('намеренная лента прокрутки и многоточие проходят', async ({ page }) => {
    await card(page, 'overflow-x:auto')
    await noHorizontalOverflow(page, 'лента', ['.card'])
    await card(page, 'overflow:hidden;white-space:nowrap;text-overflow:ellipsis', '<span>' + 'длинное имя клиента '.repeat(10) + '</span>')
    await noHorizontalOverflow(page, 'многоточие')
  })

  test('блок в пределах карточки и вылет за окно', async ({ page }) => {
    await card(page, 'overflow:hidden', '<div style="width:300px;height:20px"></div>')
    await noHorizontalOverflow(page, 'впору')
    await card(page, '', '<div class="wide" style="width:640px;height:20px"></div>')
    await expect(noHorizontalOverflow(page, 'окно')).rejects.toThrow(/scrollWidth 640 > 402/)
  })
})
