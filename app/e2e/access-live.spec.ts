import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

test('живой OpenVPN: заморозка завершает сессию и запрещает повторное подключение', async ({ page }) => {
  test.setTimeout(180_000)
  test.skip(!container, 'VPN-клиент разрешён только в одноразовом контейнере')
  await loginApi(page)
  const client = await (await page.request.post('/api/clients', { data: { name: uniq('ovpn-session') } })).json()
  const device = await (await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'Телефон' } })).json()
  const stop = () => inside(`
    import { existsSync, readFileSync, rmSync } from 'node:fs';
    const dir='/tmp/access-openvpn-'+JSON.parse(process.argv[1]);
    if(existsSync(dir+'/pid')) {
      const pid=Number(readFileSync(dir+'/pid','utf8'));
      if(Number.isInteger(pid) && pid>1) try { process.kill(pid,'SIGTERM'); } catch {}
    }
    rmSync(dir,{recursive:true,force:true});
  `, client.id)
  const start = () => inside(String.raw`
    import { mkdirSync, writeFileSync } from 'node:fs';
    import { execFileSync } from 'node:child_process';
    import { DatabaseSync } from 'node:sqlite';
    const [clientId,deviceId]=process.argv.slice(1).map(JSON.parse);
    const dir='/tmp/access-openvpn-'+clientId;
    const db=new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout=10000');
    const d=db.prepare('SELECT ovpn_cert,ovpn_key FROM devices WHERE id=? AND client_id=?').get(deviceId,clientId);
    mkdirSync(dir,{mode:0o700});
    writeFileSync(dir+'/cert',d.ovpn_cert,{mode:0o600});
    writeFileSync(dir+'/key',d.ovpn_key,{mode:0o600});
    const config=['client','dev tun','proto udp','remote 127.0.0.1 1194','nobind',
      'route-nopull','route-noexec','ifconfig-noexec','auth-nocache','connect-retry 1 1','verb 3',
      'ca /etc/openvpn/server/pki/ca.crt','tls-crypt /etc/openvpn/server/pki/tls-crypt.key',
      'cert '+dir+'/cert','key '+dir+'/key','writepid '+dir+'/pid','log '+dir+'/log'].join('\n')+'\n';
    writeFileSync(dir+'/config',config,{mode:0o600});
    execFileSync('openvpn',['--config',dir+'/config','--daemon'],{stdio:'pipe'});
  `, client.id, device.id)
  const state = () => inside(`
    import { readFileSync } from 'node:fs';
    const log=readFileSync('/tmp/access-openvpn-'+JSON.parse(process.argv[1])+'/log','utf8');
    console.log(/AUTH_FAILED/.test(log)?'denied':/Initialization Sequence Completed/.test(log)?'connected':'pending');
  `, client.id)
  const connected = () => inside(String.raw`
    import { createConnection } from 'node:net';
    const id=JSON.parse(process.argv[1]);
    const status=await new Promise((resolve,reject)=>{
      const socket=createConnection('/etc/openvpn/server/mgmt.sock');
      let text='';
      socket.setEncoding('utf8');
      socket.setTimeout(5000,()=>socket.destroy(new Error('management timeout')));
      socket.on('error',reject);
      socket.on('connect',()=>socket.write('status 2\n'));
      socket.on('data',chunk=>{
        text+=chunk;
        if(/^END\r?$/m.test(text)) { socket.end('quit\n'); resolve(text); }
      });
    });
    console.log(status.split(/\r?\n/).some(line=>line.startsWith('CLIENT_LIST,dev_'+id+',')));
  `, device.id)
  try {
    start()
    await expect.poll(state, { timeout: 30_000 }).toBe('connected')
    await expect.poll(connected).toBe('true')
    const accepted = await (await page.request.patch(`/api/clients/${client.id}`, { data: { frozenManual: true } })).json()
    const frozen = await waitAccess(page.request, accepted.operation)
    expect(frozen.protocols.openvpn.confirmedVersion).toBe(frozen.version)
    // Management подтверждает закрытие сразу. Старый клиент узнаёт о нём
    // лишь по keepalive; его историческая строка Connected не является фактом.
    await expect.poll(connected).toBe('false')
    stop()
    start()
    await expect.poll(state, { timeout: 30_000 }).toBe('denied')
    stop()
    const resumed = await (await page.request.patch(`/api/clients/${client.id}`, { data: { frozenManual: false } })).json()
    await waitAccess(page.request, resumed.operation)
    start()
    await expect.poll(state, { timeout: 30_000 }).toBe('connected')
    console.log('live OpenVPN client: connected → freeze confirmed, active session closed → reconnect AUTH_FAILED → unfreeze, same certificate connected')
  }
  finally {
    stop()
    const removed = await (await page.request.delete(`/api/clients/${client.id}`)).json()
    if (removed.operation) await waitAccess(page.request, removed.operation)
  }
})

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
    // Удерживаем только замок одноразового исполнителя, чтобы снять принятие
    // до первого применения. API и панель при этом продолжают отвечать.
    inside(`
      import { spawn } from 'node:child_process';
      import { rmSync } from 'node:fs';
      rmSync('/tmp/access-hold-ready',{force:true}); rmSync('/tmp/access-hold-release',{force:true});
      spawn('flock',['/var/lib/anysda-vpn2/access-worker.lock','timeout','30','sh','-c',
        'touch /tmp/access-hold-ready; while [ ! -f /tmp/access-hold-release ]; do sleep 0.1; done'],
        {detached:true,stdio:'ignore'}).unref();
    `)
    await expect.poll(() => inside("import { existsSync } from 'node:fs'; console.log(existsSync('/tmp/access-hold-ready'));"), { timeout: 10_000 }).toBe('true')
    const response = page.waitForResponse(r => r.request().method() === 'PATCH' && r.url().endsWith(`/api/clients/${c.id}`))
    await detail.getByRole('button', { name: 'Заморозить', exact: true }).click()
    const accepted = await response
    expect(accepted.status()).toBe(202)
    const { operation } = await accepted.json() as { operation: AccessOperation }
    expect(operation.status).toBe('pending')
    await expect(detail.getByRole('status')).toContainText('Отключение выполняется')
    if (screenshots) await page.screenshot({ path: path.join(screenshots, 'access-pending.png'), fullPage: true })
    inside("import { writeFileSync } from 'node:fs'; writeFileSync('/tmp/access-hold-release','');")
    await expect(detail.getByRole('status').locator('b')).toHaveText('Отключение не завершено: OpenVPN', { timeout: 30_000 })
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
    inside("import { writeFileSync } from 'node:fs'; writeFileSync('/tmp/access-hold-release','');")
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
    for (const state of Object.values(recovered.protocols)) expect(state.confirmedVersion).toBe(recovered.version)
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
    for (const state of Object.values(latest.protocols)) expect(state.confirmedVersion).toBe(latest.version)
    const older = await (await page.request.get(`/api/access-operations/${ordered[0]!.id}`)).json()
    expect(older.status).toBe('superseded')
    const current = await (await page.request.get(`/api/clients/${c.id}`)).json()
    expect(current.frozenManual).toBe(latest.kind === 'freeze')
    const desiredVersion = Number(inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db=new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      db.exec('PRAGMA busy_timeout=10000');
      console.log(db.prepare('SELECT access_version FROM clients WHERE id=?').get(JSON.parse(process.argv[1])).access_version);
    `, c.id))
    expect(desiredVersion).toBe(latest.version)
    console.log('docker restart after WG apply: same operation confirmed; concurrent panel/bot: older=Заменена, latest version confirmed')
  }
  finally {
    inside("import { rmSync } from 'node:fs'; rmSync('/usr/local/bin/wg', {force:true});")
    const removed = await (await page.request.delete(`/api/clients/${c.id}`)).json()
    if (removed.operation) await waitAccess(page.request, removed.operation)
  }
})
