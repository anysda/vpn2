import { X509Certificate } from 'node:crypto'
import { deviceCard, openClient } from './support/clients'
import { expect, hydrated, loginApi, test, toast, uniq } from './support/fixtures'
import { container, fault, file, inside, pki, restore, rows, verify } from './support/openvpn-ca'

const scenarios = [
  { target: 'device', mode: 'revoke', ui: true },
  { target: 'client', mode: 'revoke', ui: true },
  { target: 'device', mode: 'crl' },
  { target: 'device', mode: 'missing' },
  { target: 'bot-device', mode: 'revoke' },
  { target: 'bot-client', mode: 'revoke' },
]

test('устройство без сертификата удаляется и без CA', async ({ page }) => {
  test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере run-local.sh')
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('no-cert') } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  try {
    fault('missing', '')
    const added = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
    expect(added.status()).toBe(200)
    const device = await added.json()
    expect(rows(client.id)[0]!.ovpn_cert).toBeNull()
    expect((await page.request.delete(`/api/clients/${client.id}/devices/${device.id}`)).status()).toBe(200)
    expect(rows(client.id)).toEqual([])
  }
  finally {
    restore()
    await page.request.delete(`/api/clients/${client.id}`)
  }
})

for (const scenario of scenarios) {
  test(`удаление ${scenario.target}: сбой CA ${scenario.mode} и повтор`, async ({ page, guard }) => {
    test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере run-local.sh')
    await loginApi(page)
    const name = uniq('revoke')
    const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    const wholeClient = scenario.target.endsWith('client')
    const bot = scenario.target.startsWith('bot-')
    try {
      for (let i = 0; i < (wholeClient ? 3 : 1); i++) {
        const added = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `phone-${i}` } })
        expect(added.status()).toBe(200)
      }
      const saved = rows(client.id)
      expect(saved).toHaveLength(wholeClient ? 3 : 1)
      for (const d of saved) expect(verify(d.ovpn_cert).status).toBe(0)
      const failed = saved[wholeClient ? 1 : 0]!
      const beforeCrl = file(`${pki}/crl.pem`)
      const apiPath = wholeClient ? `/api/clients/${client.id}` : `/api/clients/${client.id}/devices/${failed.id}`
      const botPath = wholeClient ? `/api/bot/admin/clients/${client.id}` : `/api/bot/devices/${failed.id}?chatId=${client.id}`
      if (bot) {
        inside(`
          import { DatabaseSync } from 'node:sqlite';
          const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
          db.exec('PRAGMA busy_timeout = 10000');
          const id = JSON.parse(process.argv[1]);
          db.prepare('UPDATE clients SET tg_chat_id = ? WHERE id = ?').run(id, id);
        `, client.id)
      }
      const remove = async () => {
        if (!bot) return (await page.request.delete(apiPath)).status()
        return Number(inside(`
          const r = await fetch('http://127.0.0.1:31821' + JSON.parse(process.argv[1]), {
            method: 'DELETE', headers: { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET }
          }); console.log(r.status);
        `, botPath))
      }
      if (scenario.ui) {
        await page.goto('/clients')
        await hydrated(page)
        const modal = await openClient(page, name)
        if (wholeClient) await modal.getByRole('button', { name: 'Удалить клиента' }).click()
        else await deviceCard(page, 'phone-0').remove.click()
      }
      fault(scenario.mode, new X509Certificate(failed.ovpn_cert).serialNumber)
      if (scenario.ui) {
        guard.allow(503, /\/api\/clients\//, 'DELETE')
        const dialog = page.getByRole('dialog', { name: wholeClient ? 'Удалить клиента?' : 'Удалить девайс?' })
        const response = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().endsWith(apiPath))
        await dialog.getByRole('button', { name: 'Удалить', exact: true }).click()
        expect((await response).status()).toBe(503)
        await expect(toast(page, 'Ошибка удаления')).toBeVisible()
        await expect(dialog).toBeVisible()
      }
      else expect(await remove()).toBe(503)

      // Сертификаты и прочие реквизиты остаются в БД для повтора.
      expect(rows(client.id)).toEqual(saved)
      expect((await page.request.get(`/api/clients/${client.id}`)).status()).toBe(200)
      const wg = file('/etc/wireguard/wg0.conf')
      const ike = file('/etc/swanctl/conf.d/anysda-clients.conf')
      for (const d of saved) {
        expect(wg).toContain(d.wg_public_key)
        expect(ike).toContain(d.ikev2_username)
        expect(file(`/etc/openvpn/server/ccd/dev_${d.id}`)).toContain('ifconfig-push')
      }
      if (wholeClient) {
        expect(verify(saved[0]!.ovpn_cert).output).toContain('certificate revoked')
        expect(verify(failed.ovpn_cert).status).toBe(0)
        expect(verify(saved[2]!.ovpn_cert).status).toBe(0)
      }
      else {
        expect(file(`${pki}/crl.pem`)).toBe(beforeCrl)
        expect(verify(failed.ovpn_cert).status).toBe(0)
      }
      expect(await remove()).toBe(503)
      expect(rows(client.id)).toEqual(saved)
      restore()
      if (scenario.ui) {
        const dialog = page.getByRole('dialog', { name: wholeClient ? 'Удалить клиента?' : 'Удалить девайс?' })
        const response = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().endsWith(apiPath))
        await dialog.getByRole('button', { name: 'Удалить', exact: true }).click()
        expect((await response).status()).toBe(200)
        await expect(dialog).toBeHidden()
        await expect(toast(page, wholeClient ? `Клиент «${name}» удалён` : 'Девайс «phone-0» удалён')).toBeVisible()
      }
      else expect(await remove()).toBe(200)
      expect(rows(client.id)).toEqual([])
      if (wholeClient) expect((await page.request.get(`/api/clients/${client.id}`)).status()).toBe(404)
      for (const d of saved) {
        const verified = verify(d.ovpn_cert)
        expect(verified.status).not.toBe(0)
        expect(verified.output).toContain('certificate revoked')
        expect(file('/etc/wireguard/wg0.conf')).not.toContain(d.wg_public_key)
        expect(file('/etc/swanctl/conf.d/anysda-clients.conf')).not.toContain(d.ikev2_username)
        expect(inside(`import { existsSync } from 'node:fs'; console.log(existsSync(JSON.parse(process.argv[1])));`,
          `/etc/openvpn/server/ccd/dev_${d.id}`)).toBe('false')
      }
      console.log(`${scenario.target}/${scenario.mode}: 503, credentials retained; retry 200; old certificates revoked; WG/IKEv2/CCD removed`)
    }
    finally {
      restore()
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })
}
