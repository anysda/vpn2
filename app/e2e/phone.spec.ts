import type { Page } from '@playwright/test'
import { expect, test, uniq } from './support/fixtures'
import { addDevice, createClientUi, deviceCard, openClient, openHome } from './support/clients'

/** Ширина документа не больше окна: ничего не вылезает вбок. */
async function noHorizontalOverflow(page: Page, where: string) {
  const m = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth
    const wide = [...document.querySelectorAll<HTMLElement>('body *')]
      .filter((el) => {
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.right > vw + 1 && getComputedStyle(el).position !== 'fixed'
      })
      .slice(0, 5)
      .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 4).join('.')} right=${Math.round(el.getBoundingClientRect().right)}`)
    return { vw, sw: document.documentElement.scrollWidth, wide }
  })
  expect(m.sw, `${where}: scrollWidth ${m.sw} > ${m.vw}; вылезают: ${m.wide.join('; ')}`).toBeLessThanOrEqual(m.vw)
}

test.describe('телефон 390px', () => {
  test('главная, «Я», вход и модалки без горизонтальной прокрутки; модалки закрываются', async ({ page }) => {
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
    await noHorizontalOverflow(page, '/login')

    await openHome(page)
    // Дождаться карточек мониторинга и маршрутов — они и есть самые широкие.
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    await expect(page.getByText('CPU').first()).toBeVisible()
    await noHorizontalOverflow(page, '/')

    await page.goto('/me')
    await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
    await noHorizontalOverflow(page, '/me')

    await page.goto('/')
    const client = uniq('phone')
    await createClientUi(page, client)
    const dlg = await openClient(page, client)
    await addDevice(page, 'e2e-phone')
    await noHorizontalOverflow(page, 'модалка клиента')

    const dc = deviceCard(page, 'e2e-phone')
    for (const [btn, proto] of [[dc.wg, 'WireGuard'], [dc.ovpn, 'OpenVPN'], [dc.ikev2, 'IKEv2']] as const) {
      await btn.click()
      const m = page.getByRole('dialog', { name: 'e2e-phone' }).filter({ hasText: proto })
      await expect(m).toBeVisible()
      await noHorizontalOverflow(page, `модалка ${proto}`)
      await m.getByRole('button', { name: /Close|Закрыть/ }).click()
      await expect(m).toBeHidden()
    }

    await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
    const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
    await noHorizontalOverflow(page, 'подтверждение удаления')
    await del.getByRole('button', { name: 'Удалить' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })
})
