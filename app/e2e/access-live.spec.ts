import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

test('живые VPN: сбой OpenVPN, честный статус и ручной повтор', async ({ page }) => {
  test.setTimeout(180_000)
  test.skip(!container, 'изменение файлов разрешено только в одноразовом контейнере')
  await loginApi(page)
  const telegram = await (await page.request.get('/api/admin/telegram')).json()
  test.skip(telegram.configured, 'настоящие настройки бота менять нельзя')
  await page.request.put('/api/admin/telegram', { data: { bot_token: '123456789:E2E_ACCEPTED_aaaaaaaaaaaaaaaaaaaaaaaaaa', chat_id: '123456789' } })
  const name = uniq('access')
  const c = await (await page.request.post('/api/clients', { data: { name } })).json()
  await page.request.post(`/api/clients/${c.id}/devices`, { data: { name: 'Телефон' } })
  inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db=new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
    db.prepare('UPDATE clients SET tg_chat_id=? WHERE id=?').run(987654321,JSON.parse(process.argv[1]));
  `, c.id)
  const delivered = (id: string) => JSON.parse(inside(`
    const events=await (await fetch('http://127.0.0.1:8877/events')).json();
    console.log(JSON.stringify(events.filter(e=>e.deliveryId===JSON.parse(process.argv[1]))));
  `, id)) as Array<{ text: string }>
  let broken = false
  try {
    await page.goto('/clients')
    await hydrated(page)
    const detail = await openClient(page, name)
    const screenshots = process.env.E2E_SCREENSHOT_DIR
    if (screenshots) {
      mkdirSync(screenshots, { recursive: true })
      await page.screenshot({ path: path.join(screenshots, 'access-clients.png'), fullPage: true })
    }
    inside(`
      import { renameSync, writeFileSync } from 'node:fs';
      renameSync('/etc/openvpn/server/ccd','/etc/openvpn/server/ccd.saved');
      writeFileSync('/etc/openvpn/server/ccd','injected apply failure');
    `)
    broken = true
    const response = page.waitForResponse(r => r.request().method() === 'PATCH' && r.url().endsWith(`/api/clients/${c.id}`))
    await detail.getByRole('button', { name: 'Заморозить', exact: true }).click()
    const accepted = await response
    expect(accepted.status()).toBe(202)
    const { operation } = await accepted.json() as { operation: AccessOperation }
    expect(operation.status).toBe('pending')
    await expect(detail.getByRole('status')).toContainText('Отключение не завершено: OpenVPN', { timeout: 30_000 })
    const failed = await (await page.request.get(`/api/access-operations/${operation.id}`)).json()
    expect(failed.protocols.wg.confirmedVersion).toBe(operation.version)
    expect(failed.protocols.openvpn.confirmedVersion).toBeNull()
    expect(delivered(operation.id)).toHaveLength(0)
    if (screenshots) await page.screenshot({ path: path.join(screenshots, 'access-openvpn-failed.png'), fullPage: true })
    inside(`
      import { rmSync, renameSync } from 'node:fs';
      rmSync('/etc/openvpn/server/ccd');
      renameSync('/etc/openvpn/server/ccd.saved','/etc/openvpn/server/ccd');
    `)
    broken = false
    await detail.getByRole('button', { name: 'Повторить', exact: true }).click()
    const confirmed = await waitAccess(page.request, operation)
    for (const state of Object.values(confirmed.protocols)) expect(state.confirmedVersion).toBe(operation.version)
    await expect(detail.getByRole('status')).toContainText('Доступ отключён', { timeout: 15_000 })
    await expect.poll(() => delivered(operation.id), { timeout: 15_000 }).toHaveLength(1)
    expect(delivered(operation.id)[0]!.text).toBe('Доступ отключён')
    await page.request.post(`/api/access-operations/${operation.id}/retry`)
    await waitAccess(page.request, operation)
    expect(delivered(operation.id)).toHaveLength(1)
    if (screenshots) await page.screenshot({ path: path.join(screenshots, 'access-confirmed.png'), fullPage: true })
    console.log('live WG/OpenVPN/IKEv2: pending → Отключение не завершено: OpenVPN → Доступ отключён; confirmedVersion=desiredVersion; bot notifications: 0 → 1')
  }
  finally {
    if (broken) inside(`import { rmSync, renameSync } from 'node:fs'; rmSync('/etc/openvpn/server/ccd'); renameSync('/etc/openvpn/server/ccd.saved','/etc/openvpn/server/ccd');`)
    const removed = await (await page.request.delete(`/api/clients/${c.id}`)).json()
    if (removed.operation) await waitAccess(page.request, removed.operation)
    await page.request.put('/api/admin/telegram', { data: { bot_token: '', chat_id: String(telegram.chat_id ?? '') } })
  }
})

test('docker restart во время применения; гонка панели и бота сходится к последней версии', async ({ page }) => {
  test.setTimeout(180_000)
  test.skip(!container, 'перезапуск разрешён только для одноразового контейнера')
  const docker = (process.env.DOCKER || 'docker').split(/\s+/)
  const command = (...args: string[]) => execFileSync(docker[0]!, [...docker.slice(1), ...args], { encoding: 'utf8' })
  await loginApi(page)
  const c = await (await page.request.post('/api/clients', { data: { name: uniq('restart') } })).json()
  await page.request.post(`/api/clients/${c.id}/devices`, { data: { name: 'Телефон' } })
  try {
    inside(String.raw`
      import { writeFileSync } from 'node:fs';
      writeFileSync('/usr/local/bin/wg', '#!/bin/sh\n/usr/bin/wg "$@"\nrc=$?\nif [ "$1" = syncconf ]; then touch /tmp/access-applied; sleep 30; fi\nexit "$rc"\n', {mode:0o755});
    `)
    const response = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
    expect(response.status()).toBe(202)
    const { operation } = await response.json()
    await expect.poll(() => inside("import { existsSync } from 'node:fs'; console.log(existsSync('/tmp/access-applied'));"), { timeout: 15_000 }).toBe('true')
    command('restart', '-t', '0', container!)
    inside("import { rmSync } from 'node:fs'; rmSync('/usr/local/bin/wg', {force:true});")
    command('exec', '-d', container!, 'node', '/tmp/fake-backends.mjs')
    command('exec', '-d', container!, 'bash', '/tmp/vpn-services.sh')
    await expect.poll(async () => (await page.request.get('/api/version').catch(() => null))?.status(), { timeout: 30_000 }).toBe(200)
    const recovered = await waitAccess(page.request, operation)
    expect(recovered.id).toBe(operation.id)
    const botUnfreeze = () => inside(`
      const r=await fetch('http://127.0.0.1:31821/api/bot/admin/clients/'+JSON.parse(process.argv[1]), {
        method:'PATCH', headers:{authorization:'Bearer '+process.env.NUXT_TGBOT_SECRET,'content-type':'application/json'},body:JSON.stringify({frozenManual:false})
      }); console.log(JSON.stringify(await r.json()));
    `, c.id)
    const freeze = page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
    const bot = JSON.parse(botUnfreeze())
    const panel = await (await freeze).json()
    const ordered = [bot.operation, panel.operation].sort((a, b) => a.version - b.version)
    const latest = await waitAccess(page.request, ordered[1]!)
    const older = await (await page.request.get(`/api/access-operations/${ordered[0]!.id}`)).json()
    expect(older.status).toBe('superseded')
    const current = await (await page.request.get(`/api/clients/${c.id}`)).json()
    expect(current.frozenManual).toBe(latest.kind === 'freeze')
    console.log('docker restart after WG apply: same operation confirmed; concurrent panel/bot: older=Заменена, latest version confirmed')
  }
  finally {
    inside("import { rmSync } from 'node:fs'; rmSync('/usr/local/bin/wg', {force:true});")
    const removed = await (await page.request.delete(`/api/clients/${c.id}`)).json()
    if (removed.operation) await waitAccess(page.request, removed.operation)
  }
})
