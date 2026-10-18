import { execFileSync, spawnSync } from 'node:child_process'

// Только одноразовый локальный контейнер run-local.sh, никогда внешний стенд.
export const container = process.env.E2E_FAULT_CONTAINER
const docker = (process.env.DOCKER || 'docker').split(/\s+/)
const pki = '/etc/openvpn/server/pki'

export function inside(code: string, ...args: unknown[]): string {
  return execFileSync(docker[0]!, [...docker.slice(1), 'exec', container!, 'node', '--input-type=module', '-e', code,
    ...args.map(a => JSON.stringify(a))], { encoding: 'utf8' }).trim()
}

export function rows(clientId: number) {
  return JSON.parse(inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout = 10000');
    console.log(JSON.stringify(db.prepare('SELECT * FROM devices WHERE client_id = ? ORDER BY id').all(JSON.parse(process.argv[1]))));
  `, clientId)) as Array<{ id: number, ovpn_cert: string, ovpn_key: string, wg_public_key: string, wg_private_key: string, ikev2_username: string, ikev2_password: string }>
}

export function file(path: string) {
  return execFileSync(docker[0]!, [...docker.slice(1), 'exec', container!, 'cat', path], { encoding: 'utf8' }).trim()
}

export function verify(cert: string) {
  // OpenSSL already reads stdin and returns its status; a new Node process
  // per certificate only adds CPU contention between the four test panels.
  const result = spawnSync(docker[0]!, [...docker.slice(1), 'exec', '-i', container!,
    '/usr/bin/openssl', 'verify', '-CAfile', `${pki}/ca.crt`, '-CRLfile', `${pki}/crl.pem`, '-crl_check',
  ], { input: cert, encoding: 'utf8' })
  if (result.error) throw result.error
  return { status: result.status, output: result.stdout + result.stderr }
}

export function fault(mode: string, serial: string) {
  // Подмена только исполняемого openssl в одноразовом контейнере. Проверка
  // старого сертификата всегда вызывает настоящий /usr/bin/openssl.
  inside(`
    import { writeFileSync, renameSync, existsSync } from 'node:fs';
    const [mode, serial] = process.argv.slice(1).map(JSON.parse);
    if (mode === 'missing') { renameSync('${pki}/ca.key', '${pki}/ca.key.saved'); }
    else {
      writeFileSync('/tmp/revoke-fault.json', JSON.stringify({ mode, serial }));
      if (!existsSync('/usr/bin/openssl.real')) renameSync('/usr/bin/openssl', '/usr/bin/openssl.real');
      writeFileSync('/usr/bin/openssl', ${JSON.stringify(`#!/usr/local/bin/node
const fs = require('node:fs');
const cp = require('node:child_process');
const args = process.argv.slice(2);
const fault = JSON.parse(fs.readFileSync('/tmp/revoke-fault.json', 'utf8'));
const revoke = args.indexOf('-revoke');
const serial = revoke < 0 ? '' : cp.execFileSync('/usr/bin/openssl.real', ['x509', '-in', args[revoke + 1], '-noout', '-serial'], { encoding: 'utf8' }).trim().split('=')[1];
if ((fault.mode === 'revoke' && serial === fault.serial) || (fault.mode === 'crl' && args.includes('-gencrl')) || (fault.mode === 'issue' && args[0] === 'ca' && args.includes('-in'))) {
  if (fault.mode === 'crl') fs.writeFileSync(args[args.indexOf('-out') + 1], 'incomplete CRL');
  console.error('injected CA failure'); process.exit(1);
}
const r = cp.spawnSync('/usr/bin/openssl.real', args, { stdio: 'inherit' });
process.exit(r.status ?? 1);
`)}, { mode: 0o755 });
    }
  `, mode, serial)
}

export function restore() {
  inside(`
    import { existsSync, renameSync, rmSync } from 'node:fs';
    if (existsSync('${pki}/ca.key.saved')) renameSync('${pki}/ca.key.saved', '${pki}/ca.key');
    if (existsSync('/usr/bin/openssl.real')) renameSync('/usr/bin/openssl.real', '/usr/bin/openssl');
    rmSync('/tmp/revoke-fault.json', { force: true });
  `)
}
