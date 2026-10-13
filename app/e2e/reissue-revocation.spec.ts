import { X509Certificate } from 'node:crypto'
import { deviceCard, openClient } from './support/clients'
import { expect, hydrated, loginApi, test, toast, uniq } from './support/fixtures'
import { container, fault, file, inside, pki, restore, rows, verify } from './support/openvpn-ca'

for (const target of ['device', 'client', 'bot-device']) {
  for (const mode of ['missing', 'revoke', 'crl', 'issue']) {
    test(`перевыпуск ${target}: сбой CA ${mode} и повтор`, async ({ page, guard }) => {
      test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере run-local.sh')
      await loginApi(page)
      const name = uniq('reissue')
      const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
      expect(created.status()).toBe(200)
      const client = await created.json()
      const wholeClient = target === 'client'
      const bot = target === 'bot-device'
      const ui = !bot && mode === 'missing'
      // При адресном сбое отзыва массовый запрос успевает заменить первый cert.
      const partial = wholeClient && mode === 'revoke'
      try {
        for (let i = 0; i < (wholeClient ? 3 : 1); i++) {
          const added = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `phone-${i}` } })
          expect(added.status()).toBe(200)
        }
        const saved = rows(client.id)
        expect(saved).toHaveLength(wholeClient ? 3 : 1)
        for (const d of saved) expect(verify(d.ovpn_cert).status).toBe(0)
        const failed = saved[partial ? 1 : 0]!
        const apiPath = wholeClient
          ? `/api/clients/${client.id}/reissue`
          : `/api/clients/${client.id}/devices/${failed.id}/reissue`
        if (bot) {
          inside(`
            import { DatabaseSync } from 'node:sqlite';
            const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
            db.exec('PRAGMA busy_timeout = 10000');
            const id = JSON.parse(process.argv[1]);
            db.prepare('UPDATE clients SET tg_chat_id = ? WHERE id = ?').run(id, id);
          `, client.id)
        }
        const reissue = async () => {
          if (!bot) {
            const r = await page.request.post(apiPath)
            return { status: r.status(), body: await r.json() }
          }
          return JSON.parse(inside(`
            const [deviceId, chatId] = process.argv.slice(1).map(JSON.parse);
            const r = await fetch('http://127.0.0.1:51821/api/bot/devices/' + deviceId + '/reissue', {
              method: 'POST', headers: { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'content-type': 'application/json' },
              body: JSON.stringify({ chatId })
            }); console.log(JSON.stringify({ status: r.status, body: await r.json() }));
          `, failed.id, client.id))
        }
        const dialog = page.getByRole('dialog', { name: wholeClient ? 'Перевыпустить все ключи?' : 'Перевыпустить ключи девайса?' })
        if (ui) {
          await page.goto('/')
          await hydrated(page)
          const modal = await openClient(page, name)
          if (wholeClient) await modal.getByRole('button', { name: 'Перевыпустить все ключи', exact: true }).click()
          else await deviceCard(page, 'phone-0').reissue.click()
          guard.allow(503, /\/api\/clients\/.+\/reissue$/, 'POST')
        }
        const beforeCrl = file(`${pki}/crl.pem`)
        const beforeWg = file('/etc/wireguard/wg0.conf')
        const beforeIke = file('/etc/swanctl/conf.d/anysda-clients.conf')
        fault(mode, new X509Certificate(failed.ovpn_cert).serialNumber)
        const intermediate: string[] = []
        for (let attempt = 0; attempt < 2; attempt++) {
          let result
          if (ui) {
            const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith(apiPath))
            await dialog.getByRole('button', { name: 'Перевыпустить', exact: true }).click()
            const r = await response
            result = { status: r.status(), body: await r.json() }
          }
          else result = await reissue()
          expect(result.status).toBe(503)
          expect(result.body).toMatchObject({ statusMessage: 'ovpn_reissue_failed', message: 'Ошибка перевыпуска' })
          if (ui) {
            await expect(toast(page, 'Ошибка перевыпуска')).toBeVisible()
            await expect(dialog).toBeVisible()
            await expect(toast(page, wholeClient ? 'Перевыпущены ключи всех девайсов (3)' : 'Ключи девайса «phone-0» перевыпущены')).toBeHidden()
          }
          const current = rows(client.id)
          // Невыполненный отзыв/выдача сохраняет cert + key и остальные поля.
          expect(current.slice(partial ? 1 : 0)).toEqual(saved.slice(partial ? 1 : 0))
          for (let i = 0; i < saved.length; i++) {
            expect(current[i]!.wg_private_key).toBe(saved[i]!.wg_private_key)
            expect(current[i]!.ikev2_password).toBe(saved[i]!.ikev2_password)
          }
          expect(file('/etc/wireguard/wg0.conf')).toBe(beforeWg)
          expect(file('/etc/swanctl/conf.d/anysda-clients.conf')).toBe(beforeIke)
          if (partial) {
            expect(verify(saved[0]!.ovpn_cert).output).toContain('certificate revoked')
            expect(verify(current[0]!.ovpn_cert).status).toBe(0)
            intermediate.push(current[0]!.ovpn_cert)
          }
          else if (mode !== 'issue') expect(file(`${pki}/crl.pem`)).toBe(beforeCrl)
          if (mode === 'issue') expect(verify(failed.ovpn_cert).output).toContain('certificate revoked')
          else expect(verify(failed.ovpn_cert).status).toBe(0)
        }
        restore()
        if (ui) {
          const response = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith(apiPath))
          await dialog.getByRole('button', { name: 'Перевыпустить', exact: true }).click()
          expect((await response).status()).toBe(200)
          await expect(dialog).toBeHidden()
          await expect(toast(page, wholeClient ? 'Перевыпущены ключи всех девайсов (3)' : 'Ключи девайса «phone-0» перевыпущены')).toBeVisible()
        }
        else expect((await reissue()).status).toBe(200)
        const updated = rows(client.id)
        expect(updated).toHaveLength(saved.length)
        for (const cert of [...saved.map(d => d.ovpn_cert), ...intermediate]) {
          const result = verify(cert)
          expect(result.status).not.toBe(0)
          expect(result.output).toContain('certificate revoked')
        }
        for (let i = 0; i < saved.length; i++) {
          const d = updated[i]!
          expect(verify(d.ovpn_cert).status).toBe(0)
          expect(d.ovpn_key).not.toBe(saved[i]!.ovpn_key)
          expect(d.wg_private_key).not.toBe(saved[i]!.wg_private_key)
          expect(d.ikev2_password).not.toBe(saved[i]!.ikev2_password)
          expect(file('/etc/wireguard/wg0.conf')).toContain(d.wg_public_key)
          expect(file('/etc/wireguard/wg0.conf')).not.toContain(saved[i]!.wg_public_key)
          expect(file('/etc/swanctl/conf.d/anysda-clients.conf')).toContain(d.ikev2_password)
          expect(file(`/etc/openvpn/server/ccd/dev_${d.id}`)).toContain('ifconfig-push')
        }
        console.log(`${target}/${mode}: 503 twice, cert/key retained; retry 200; old/intermediate certificates revoked; new certificates valid; WG/IKEv2 synced`)
      }
      finally {
        restore()
        await page.request.delete(`/api/clients/${client.id}`)
      }
    })
  }
}

test('перевыпуск без сертификата и CA сохраняет доступность WG/IKEv2', async ({ page }) => {
  test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере run-local.sh')
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('no-cert-reissue') } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  try {
    fault('missing', '')
    const added = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
    expect(added.status()).toBe(200)
    const device = await added.json()
    const before = rows(client.id)[0]!
    expect(before.ovpn_cert).toBeNull()
    expect((await page.request.post(`/api/clients/${client.id}/devices/${device.id}/reissue`)).status()).toBe(200)
    const after = rows(client.id)[0]!
    expect(after.ovpn_cert).toBeNull()
    expect(after.wg_private_key).not.toBe(before.wg_private_key)
    expect(after.ikev2_password).not.toBe(before.ikev2_password)
  }
  finally {
    restore()
    await page.request.delete(`/api/clients/${client.id}`)
  }
})
