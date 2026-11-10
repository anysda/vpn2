import { X509Certificate } from 'node:crypto'
import { deviceCard, openClient } from './support/clients'
import { expect, hydrated, loginApi, uniq, test } from './support/fixtures'
import { container, fault, inside, restore, rows, verify, file } from './support/openvpn-ca'
import { waitAccess } from './support/access'

test.describe('delete-revocation', () => {
  const scenarios = [
    { target: 'device', mode: 'revoke', ui: true },
    { target: 'client', mode: 'revoke', ui: true },
    { target: 'device', mode: 'crl' },
    { target: 'device', mode: 'missing' },
    { target: 'bot-device', mode: 'revoke' },
    { target: 'bot-client', mode: 'revoke' },
  ]

  test('устройство без сертификата удаляется и без CA', async ({ page }) => {
    test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
    await loginApi(page)
    const client = await (await page.request.post('/api/clients', { data: { name: uniq('no-cert') } })).json()
    try {
      fault('missing', '')
      const device = await (await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })).json()
      expect(rows(client.id)[0]!.ovpn_cert).toBeNull()
      const response = await page.request.delete(`/api/clients/${client.id}/devices/${device.id}`)
      expect(response.status()).toBe(202)
      await waitAccess(page.request, (await response.json()).operation)
      expect(rows(client.id)).toEqual([])
    }
    finally {
      restore()
      const result = await (await page.request.delete(`/api/clients/${client.id}`)).json()
      if (result.operation) await waitAccess(page.request, result.operation)
    }
  })

  for (const scenario of scenarios) {
    test(`удаление ${scenario.target}: сбой CA ${scenario.mode} и повтор`, async ({ page }) => {
      test.setTimeout(180_000)
      test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
      await loginApi(page)
      const name = uniq('revoke')
      const client = await (await page.request.post('/api/clients', { data: { name, deviceLimit: null } })).json()
      const wholeClient = scenario.target.endsWith('client')
      const bot = scenario.target.startsWith('bot-')
      try {
        for (let i = 0; i < (wholeClient ? 3 : 1); i++) {
          expect((await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `phone-${i}` } })).status()).toBe(200)
        }
        const saved = rows(client.id)
        for (const d of saved) expect(verify(d.ovpn_cert).status).toBe(0)
        const failed = saved[wholeClient ? 1 : 0]!
        const apiPath = wholeClient ? `/api/clients/${client.id}` : `/api/clients/${client.id}/devices/${failed.id}`
        const botPath = wholeClient ? `/api/bot/admin/clients/${client.id}` : `/api/bot/devices/${failed.id}?chatId=${client.id}`
        if (bot) inside(`
        import { DatabaseSync } from 'node:sqlite';
        const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
        const id=JSON.parse(process.argv[1]); db.prepare('UPDATE clients SET tg_chat_id=? WHERE id=?').run(id,id);
      `, client.id)
        if (scenario.ui) {
          await page.goto('/clients')
          await hydrated(page)
          const detail = await openClient(page, name)
          if (wholeClient) await detail.getByRole('button', { name: 'Удалить клиента' }).click()
          else await deviceCard(page, 'phone-0').remove.click()
        }
        fault(scenario.mode, new X509Certificate(failed.ovpn_cert).serialNumber)
        let accepted
        if (scenario.ui) {
          const pending = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().endsWith(apiPath))
          await page.getByRole('dialog', { name: wholeClient ? 'Удалить клиента?' : 'Удалить девайс?' }).getByRole('button', { name: 'Удалить', exact: true }).click()
          const response = await pending
          expect(response.status()).toBe(202)
          accepted = await response.json()
        }
        else if (bot) {
          const result = JSON.parse(inside(`
          const r=await fetch('http://127.0.0.1:31821'+JSON.parse(process.argv[1]), {
            method:'DELETE',headers:{authorization:'Bearer '+process.env.NUXT_TGBOT_SECRET}
          }); console.log(JSON.stringify({status:r.status,body:await r.json()}));
        `, botPath))
          expect(result.status).toBe(202)
          accepted = result.body
        }
        else {
          const response = await page.request.delete(apiPath)
          expect(response.status()).toBe(202)
          accepted = await response.json()
        }
        const operation = accepted.operation
        expect(operation.status).toBe('pending')
        await expect.poll(async () => (await (await page.request.get(`/api/access-operations/${operation.id}`)).json()).protocols.openvpn.state, { timeout: 30_000 }).toBe('failed')
        // Ни каскад, ни частичный отзыв не уничтожили последний способ повтора.
        const credentials = (list: ReturnType<typeof rows>) => list.map(d => [d.id, d.wg_public_key, d.wg_private_key, d.ovpn_cert, d.ovpn_key, d.ikev2_username, d.ikev2_password])
        expect(credentials(rows(client.id))).toEqual(credentials(saved))
        expect((await page.request.get(`/api/clients/${client.id}`)).status()).toBe(200)
        const state = await (await page.request.get(`/api/access-operations/${operation.id}`)).json()
        expect(state.status).toBe('pending')
        expect(state.protocols.openvpn.confirmedVersion).toBeNull()
        if (scenario.ui) await expect(page.getByTestId('client-detail').getByRole('status')).toContainText('Отключение не завершено: OpenVPN')
        restore()
        await page.request.post(`/api/access-operations/${operation.id}/retry`)
        await waitAccess(page.request, operation)
        expect(rows(client.id)).toEqual([])
        if (wholeClient) expect((await page.request.get(`/api/clients/${client.id}`)).status()).toBe(404)
        for (const d of saved) {
          const result = verify(d.ovpn_cert)
          expect(result.status).not.toBe(0)
          expect(result.output).toContain('certificate revoked')
        }
        console.log(`${scenario.target}/${scenario.mode}: accepted pending; failure retains credentials; same operation confirmed; old certificates revoked`)
      }
      finally {
        restore()
        const result = await (await page.request.delete(`/api/clients/${client.id}`)).json()
        if (result.operation) await waitAccess(page.request, result.operation)
      }
    })
  }
})

test.describe('reissue-revocation', () => {
  for (const target of ['device', 'client', 'bot-device']) {
    for (const mode of ['missing', 'revoke', 'crl', 'issue']) {
      test(`перевыпуск ${target}: сбой CA ${mode} и повтор`, async ({ page }) => {
        test.setTimeout(180_000)
        test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
        await loginApi(page)
        const name = uniq('reissue')
        const client = await (await page.request.post('/api/clients', { data: { name, deviceLimit: null } })).json()
        const wholeClient = target === 'client'
        const bot = target === 'bot-device'
        const ui = !bot && mode === 'missing'
        try {
          for (let i = 0; i < (wholeClient ? 3 : 1); i++) {
            expect((await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `phone-${i}` } })).status()).toBe(200)
          }
          const saved = rows(client.id)
          for (const d of saved) expect(verify(d.ovpn_cert).status).toBe(0)
          const failed = saved[wholeClient && mode === 'revoke' ? 1 : 0]!
          const apiPath = wholeClient ? `/api/clients/${client.id}/reissue` : `/api/clients/${client.id}/devices/${failed.id}/reissue`
          if (bot) inside(`
          import { DatabaseSync } from 'node:sqlite';
          const db=new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
          const id=JSON.parse(process.argv[1]); db.prepare('UPDATE clients SET tg_chat_id=? WHERE id=?').run(id,id);
        `, client.id)
          if (ui) {
            await page.goto('/clients')
            await hydrated(page)
            const detail = await openClient(page, name)
            if (wholeClient) await detail.getByRole('button', { name: 'Перевыпустить все ключи', exact: true }).click()
            else await deviceCard(page, 'phone-0').reissue.click()
          }
          fault(mode, new X509Certificate(failed.ovpn_cert).serialNumber)
          let accepted
          if (ui) {
            const pending = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith(apiPath))
            await page.getByRole('dialog', { name: wholeClient ? 'Перевыпустить все ключи?' : 'Перевыпустить ключи девайса?' }).getByRole('button', { name: 'Перевыпустить', exact: true }).click()
            const response = await pending
            expect(response.status()).toBe(202)
            accepted = await response.json()
          }
          else if (bot) {
            const result = JSON.parse(inside(`
            const [deviceId,chatId]=process.argv.slice(1).map(JSON.parse);
            const r=await fetch('http://127.0.0.1:31821/api/bot/devices/'+deviceId+'/reissue',{
              method:'POST',headers:{authorization:'Bearer '+process.env.NUXT_TGBOT_SECRET,'content-type':'application/json'},
              body:JSON.stringify({chatId})
            }); console.log(JSON.stringify({status:r.status,body:await r.json()}));
          `, failed.id, client.id))
            expect(result.status).toBe(202)
            accepted = result.body
          }
          else {
            const response = await page.request.post(apiPath)
            expect(response.status()).toBe(202)
            accepted = await response.json()
          }
          const operation = accepted.operation
          expect(operation.status).toBe('pending')
          await expect.poll(async () => (await (await page.request.get(`/api/access-operations/${operation.id}`)).json()).protocols.openvpn.state, { timeout: 30_000 }).toBe('failed')
          const pendingKeys = rows(client.id)
          // Не потерян ни один старый сертификат; желаемые WG/IKEv2 уже записаны один раз.
          for (let i = 0; i < saved.length; i++) {
            expect(pendingKeys[i]!.ovpn_cert).toBe(saved[i]!.ovpn_cert)
            expect(pendingKeys[i]!.ovpn_key).toBe(saved[i]!.ovpn_key)
            expect(pendingKeys[i]!.wg_private_key).not.toBe(saved[i]!.wg_private_key)
            expect(pendingKeys[i]!.ikev2_password).not.toBe(saved[i]!.ikev2_password)
          }
          if (ui) await expect(page.getByTestId('client-detail').getByRole('status')).toContainText('Перевыпуск не завершён: OpenVPN')
          // Ручной повтор возобновляет тот же ID и не перевыпускает успешные протоколы.
          await page.request.post(`/api/access-operations/${operation.id}/retry`)
          expect(rows(client.id).map(d => d.wg_private_key)).toEqual(pendingKeys.map(d => d.wg_private_key))
          restore()
          await page.request.post(`/api/access-operations/${operation.id}/retry`)
          await waitAccess(page.request, operation)
          const updated = rows(client.id)
          for (let i = 0; i < saved.length; i++) {
            const d = updated[i]!
            expect(verify(saved[i]!.ovpn_cert).output).toContain('certificate revoked')
            expect(verify(d.ovpn_cert).status).toBe(0)
            expect(d.ovpn_key).not.toBe(saved[i]!.ovpn_key)
            expect(d.wg_private_key).toBe(pendingKeys[i]!.wg_private_key)
            expect(d.ikev2_password).toBe(pendingKeys[i]!.ikev2_password)
            expect(file('/etc/wireguard/wg0.conf')).toContain(d.wg_public_key)
            expect(file('/etc/wireguard/wg0.conf')).not.toContain(saved[i]!.wg_public_key)
            expect(file('/etc/swanctl/conf.d/anysda-clients.conf')).toContain(d.ikev2_password)
          }
          console.log(`${target}/${mode}: accepted pending; failed OpenVPN retains old cert; retry keeps WG/IKEv2 keys; confirmed, old cert revoked, new cert valid`)
        }
        finally {
          restore()
          const result = await (await page.request.delete(`/api/clients/${client.id}`)).json()
          if (result.operation) await waitAccess(page.request, result.operation)
        }
      })
    }
  }

  test('перевыпуск без CA сохраняет доступность WG/IKEv2 и не скрывает ошибку OpenVPN', async ({ page }) => {
    test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
    await loginApi(page)
    const client = await (await page.request.post('/api/clients', { data: { name: uniq('no-cert-reissue') } })).json()
    try {
      fault('missing', '')
      const device = await (await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })).json()
      const before = rows(client.id)[0]!
      expect(before.ovpn_cert).toBeNull()
      const response = await page.request.post(`/api/clients/${client.id}/devices/${device.id}/reissue`)
      expect(response.status()).toBe(202)
      const { operation } = await response.json()
      await expect.poll(async () => (await (await page.request.get(`/api/access-operations/${operation.id}`)).json()).protocols.openvpn.state, { timeout: 30_000 }).toBe('failed')
      const after = rows(client.id)[0]!
      expect(after.ovpn_cert).toBeNull()
      expect(after.wg_private_key).not.toBe(before.wg_private_key)
      expect(after.ikev2_password).not.toBe(before.ikev2_password)
      expect(file('/etc/wireguard/wg0.conf')).toContain(after.wg_public_key)
      restore()
      await page.request.post(`/api/access-operations/${operation.id}/retry`)
      await waitAccess(page.request, operation)
    }
    finally {
      restore()
      const result = await (await page.request.delete(`/api/clients/${client.id}`)).json()
      if (result.operation) await waitAccess(page.request, result.operation)
    }
  })
})
