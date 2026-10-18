import { expect, hydrated, test, toast, uniq } from './support/fixtures'
import { addDevice, configDialog, createClientUi, deviceCard, openClient, openHome } from './support/clients'
import { checkBotQueue } from './support/bot-queue'
import { noHorizontalOverflow } from './support/overflow'
import { waitAccess } from './support/access'

test.describe('окно 1024px', () => {
  test('очередь: короткий тост один раз за сессию вкладки без горизонтальной прокрутки', async ({ page }) => {
    await checkBotQueue(page, () => noHorizontalOverflow(page, 'очередь уведомлений'))
  })
  test('разделы, вход и модалки без горизонтальной прокрутки; модалки закрываются', async ({ page }) => {
    await page.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: Array.from({ length: 5 }, (_, i) => ({
      id: `phone-queue-${i}`, label: `Уведомление ${i + 1}`, status: 'queued', remaining: 5,
    })) }))
    const queueToast = toast(page, 'Бот недоступен, 5 уведомлений в очереди')
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
    await noHorizontalOverflow(page, '/login')

    await openHome(page)
    await expect(queueToast).toBeVisible()
    // Дождаться карточек мониторинга — они и есть самые широкие.
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    await expect(page.getByText('CPU').first()).toBeVisible()
    await noHorizontalOverflow(page, '/')

    await page.goto('/routes')
    await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
    await noHorizontalOverflow(page, '/routes')
    await page.goto('/dns')
    await expect(page.getByText('DNS').first()).toBeVisible()
    await noHorizontalOverflow(page, '/dns')

    await page.goto('/settings')
    await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
    await noHorizontalOverflow(page, '/settings')

    await page.goto('/clients')
    await hydrated(page)
    const client = uniq('phone')
    await createClientUi(page, client)
    const dlg = await openClient(page, client)
    await expect(queueToast, 'фоновая очередь не накрывает кнопки модалки').toBeHidden()
    await addDevice(page, 'e2e-phone')
    await noHorizontalOverflow(page, 'панель клиента')

    const dc = deviceCard(page, 'e2e-phone')
    for (const [btn, proto] of [[dc.wg, 'WireGuard'], [dc.ovpn, 'OpenVPN'], [dc.ikev2, 'IKEv2']] as const) {
      await btn.click()
      const m = configDialog(page, 'e2e-phone', proto)
      await expect(m).toBeVisible()
      await noHorizontalOverflow(page, `модалка ${proto}`)
      await m.getByRole('button', { name: /Close|Закрыть/ }).click()
      await expect(m).toBeHidden()
    }

    await dlg.getByRole('button', { name: 'Удалить клиента' }).click()
    const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
    await noHorizontalOverflow(page, 'подтверждение удаления')
    // Принятие ещё не означает отзыв: ждём подтверждения всех VPN-служб.
    const [removed] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'DELETE'
        && /^\/api\/clients\/\d+$/.test(new URL(r.url()).pathname)),
      del.getByRole('button', { name: 'Удалить' }).click(),
    ])
    expect(removed.status()).toBe(202)
    const { operation } = await removed.json()
    expect(operation.status).toBe('pending')
    await waitAccess(page.request, operation)
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(queueToast, 'переходы и закрытие модалок не повторяют тост очереди').toHaveCount(0)
  })
})
