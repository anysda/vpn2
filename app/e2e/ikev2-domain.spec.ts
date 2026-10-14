import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside } from './support/openvpn-ca'

test('переход IKEv2 на домен сохраняет пароль и не требует перевыпуска старых профилей', async ({ page }) => {
  test.skip(!container, 'нужен одноразовый контейнер run-local.sh')
  await loginApi(page)
  const name = uniq('ike-domain')
  const client = await (await page.request.post('/api/clients', { data: { name } })).json()
  const device = await (await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })).json()
  const url = `/api/clients/${client.id}/devices/${device.id}/ikev2-credentials`
  const saved = inside(`
    import fs from 'node:fs';
    const names = ['mode', 'server-host', 'domain'];
    console.log(JSON.stringify(Object.fromEntries(names.map(n => [n, fs.existsSync('/etc/anysda/ikev2-' + n) ? fs.readFileSync('/etc/anysda/ikev2-' + n, 'utf8') : null]))));
  `)
  try {
    const old = await (await page.request.get(url)).json()
    inside(`
      import fs from 'node:fs';
      fs.writeFileSync('/etc/anysda/ikev2-domain', 'vpn.example.test');
    `)
    const pending = await (await page.request.get(url)).json()
    expect(pending.certificatePending).toBe(true)
    expect(pending).not.toHaveProperty('profileNotice')
    expect(pending.server).toBe(old.server)
    inside(`
      import fs from 'node:fs';
      fs.writeFileSync('/etc/anysda/ikev2-server-host', 'vpn.example.test');
      fs.writeFileSync('/etc/anysda/ikev2-mode', 'letsencrypt');
    `)
    const current = await (await page.request.get(url)).json()
    expect(current).toMatchObject({
      server: 'vpn.example.test', remoteId: 'vpn.example.test', caCertPem: null,
      username: old.username, password: old.password, certificatePending: false,
    })
    await page.goto('/clients')
    await hydrated(page)
    await openClient(page, name)
    await page.getByRole('button', { name: 'IKEv2', exact: true }).click()
    await expect(page.getByText('Remote ID (iOS) / Server identity (Android)', { exact: true })).toBeVisible()
    await expect(page.getByText('нужно скачать заново')).toHaveCount(0)
  }
  finally {
    inside(`
      import fs from 'node:fs';
      for (const [n, value] of Object.entries(JSON.parse(process.argv[1]))) {
        const p = '/etc/anysda/ikev2-' + n;
        if (value === null) fs.rmSync(p, { force: true }); else fs.writeFileSync(p, value);
      }
    `, JSON.parse(saved))
    await page.request.delete(`/api/clients/${client.id}`)
  }
})
