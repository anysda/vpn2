import { expect, loginApi, uniq, downloadText, test } from './support/fixtures'
import type { Page } from '@playwright/test'
import { addDevice, configDialog, createClientUi, deviceCard, ikeField, openClient, openHome } from './support/clients'
import { waitAccess } from './support/access'

test.describe('config-name', () => {
  test('имена файлов конфигов помещаются в имя интерфейса WireGuard и различают устройства', async ({ page }) => {
    await loginApi(page)
    const response = await page.request.post('/api/clients', { data: { name: uniq('long-client-name') } })
    expect(response.ok()).toBeTruthy()
    const client = await response.json()
    for (const name of ['Щ'.repeat(63) + '1', 'Щ'.repeat(63) + '2']) {
      const r = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name } })
      expect(r.ok()).toBeTruthy()
    }
    const detail = await (await page.request.get(`/api/clients/${client.id}`)).json()
    const names = detail.devices.map((d: { configName: string }) => d.configName)
    expect(new Set(names).size).toBe(2)
    for (const name of names) expect(name).toMatch(/^[a-zA-Z0-9_=+.-]{1,15}$/)
  })
})

test.describe('config-refresh', () => {
  async function config(page: Page, device: string, protocol: 'wg' | 'ovpn' | 'ikev2') {
    await deviceCard(page, device)[protocol].click()
    const label = { wg: 'WireGuard', ovpn: 'OpenVPN', ikev2: 'IKEv2' }[protocol]
    const dlg = configDialog(page, device, label)
    let value: string
    if (protocol === 'ikev2') {
      const password = ikeField(dlg, 'Пароль')
      await expect(password).toBeVisible()
      if ((await password.textContent())!.includes('•')) {
        await dlg.getByRole('button', { name: 'Показать' }).click()
      }
      value = (await password.textContent())!.trim()
      expect(value).not.toContain('•')
    }
    else {
      const wait = page.waitForEvent('download')
      await dlg.getByRole('button', { name: 'Скачать', exact: true }).click()
      value = await downloadText(await wait)
    }
    await dlg.getByRole('button', { name: /Close|Закрыть/ }).click()
    await expect(dlg).toBeHidden()
    return value
  }

  test('после общего перевыпуска модалки выдают новые WG, OpenVPN и IKEv2', async ({ page }) => {
    await openHome(page, '/clients')
    const client = uniq('refresh')
    const device = 'phone'
    await createClientUi(page, client)
    const clientDlg = await openClient(page, client)
    await addDevice(page, device)
    const protocols = ['wg', 'ovpn', 'ikev2'] as const
    const before: string[] = []
    for (const p of protocols) before.push(await config(page, device, p))
    await clientDlg.getByRole('button', { name: 'Перевыпустить все ключи', exact: true }).click()
    const accepted = page.waitForResponse(r => r.request().method() === 'POST' && /\/api\/clients\/\d+\/reissue$/.test(r.url()))
    await page.getByRole('dialog', { name: 'Перевыпустить все ключи?' }).getByRole('button', { name: 'Перевыпустить', exact: true }).click()
    const response = await accepted
    expect(response.status()).toBe(202)
    const { operation } = await response.json()
    expect(operation.status).toBe('pending')
    await waitAccess(page.request, operation)
    await expect(clientDlg.getByRole('status')).toContainText('Ключи перевыпущены')
    for (const [i, p] of protocols.entries()) {
      expect.soft(await config(page, device, p), `${p}: показан отозванный конфиг`).not.toBe(before[i])
    }
  })
})
