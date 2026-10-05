import type { Page } from '@playwright/test'
import { expect, test, toast } from './support/fixtures'
import { openHome } from './support/clients'

function botsCard(page: Page) {
  return page.locator('div').filter({ has: page.getByText('Боты', { exact: true }) })
    .filter({ has: page.getByText('Chat ID', { exact: true }) }).last()
}

test.describe('настройки Telegram', () => {
  test('никнейм админа сохраняется без @ и возвращается обратно', async ({ page }) => {
    await openHome(page)
    const card = botsCard(page)
    await expect(card.getByText(/НЕНАСТРОЕН|ОНЛАЙН|ОФФЛАЙН/)).toBeVisible()
    const save = card.getByRole('button', { name: 'Сохранить' })
    await expect(save).toBeDisabled()

    const nick = card.getByPlaceholder('@username')
    const original = await nick.inputValue()
    const temp = `e2e_admin_${Date.now().toString(36)}`
    try {
      await nick.fill(`@${temp}`)
      await expect(save).toBeEnabled()
      await save.click()
      await expect(toast(page, 'Сохранено')).toBeVisible()
      await expect(nick).toHaveValue(temp)
      await expect(save).toBeDisabled()
      const st = await (await page.request.get('/api/admin/telegram')).json()
      expect(st.admin_username).toBe(temp)
      // Токен наружу только маской.
      expect(JSON.stringify(st)).not.toMatch(/\d{6,}:[\w-]{30,}/)

      await nick.fill(original)
      if (original !== temp) {
        await save.click()
        await expect(toast(page, 'Сохранено').last()).toBeVisible()
      }
    }
    finally {
      await page.request.put('/api/admin/telegram', { data: { admin_username: original } })
    }
    const st = await (await page.request.get('/api/admin/telegram')).json()
    expect(st.admin_username).toBe(original)
  })
  // Бейдж — по /health бота, а не по открытому порту: бот с отвергнутым
  // токеном держит порт, но не отвечает (подставной бот в fake-backends так же).
  test('бейдж бота: принятый токен — ОНЛАЙН, отвергнутый — ОФФЛАЙН', async ({ page }) => {
    await openHome(page)
    const before = await (await page.request.get('/api/admin/telegram')).json()
    test.skip(before.configured, 'на стенде настоящий токен — маска не даст его вернуть')
    const card = botsCard(page)
    const setToken = async (bot_token: string, chat_id: string) => {
      const res = await page.request.put('/api/admin/telegram', { data: { bot_token, chat_id } })
      expect(res.ok()).toBe(true)
    }
    try {
      await setToken('123456789:E2E_ACCEPTED_aaaaaaaaaaaaaaaaaaaaaaaaaa', '123456789')
      await page.reload()
      await expect(card.getByText('ОНЛАЙН', { exact: true })).toBeVisible()
      expect((await (await page.request.get('/api/admin/telegram')).json()).running).toBe(true)

      await setToken('123456789:E2E_REJECTED_bbbbbbbbbbbbbbbbbbbbbbbbbb', '123456789')
      await page.reload()
      await expect(card.getByText('ОФФЛАЙН', { exact: true })).toBeVisible()
      expect((await (await page.request.get('/api/admin/telegram')).json()).running).toBe(false)
    }
    finally {
      await setToken('', String(before.chat_id ?? ''))
    }
    await page.reload()
    await expect(card.getByText('НЕНАСТРОЕН', { exact: true })).toBeVisible()
  })
})
