// Реальные loadServerKeys/syncWireguardConfig, без Nuxt и клиентской БД.
// Запускается только из backup-restore.py в одноразовом контейнере с Node 24.
import assert from 'node:assert/strict'
import { readFile, mkdtemp, rm } from 'node:fs/promises'
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'
import { spawn } from 'node:child_process'
import { once } from 'node:events'

const transport = pathToFileURL(`${process.argv[2]}/app/server/utils/control.ts`).href

registerHooks({
  resolve(specifier, context, nextResolve) {
    const stubs = {
      'drizzle-orm': 'export const and=()=>{}, eq=()=>{}, isNull=()=>{};',
      h3: 'export const createError=(e)=>Object.assign(new Error(e.message), e);',
      '../database/client': `export const IP_CLAIM_ATTEMPTS=1, isUniqueViolation=()=>false;
        export const useDb=()=>({select:()=>({from:()=>({innerJoin:async()=>[]})})});`,
      '../database/schema': 'export const clients={}, devices={}, collectorState={};',
      './control': `import { control } from ${JSON.stringify(transport)}; export { control };
        export const controlApply=(method,params)=>control(method,{...params,version:1});`,
      './allowed-ips': 'export const wgAllowedIps=()=>[];',
      './client-status': 'export const isClientActive=()=>true, expiryForControl=(at)=>at ? at.toISOString() : null;',
    }
    if (specifier in stubs) {
      return { url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}`, shortCircuit: true }
    }
    // Nuxt импортирует свои модули без расширения, Node без сборки - нет.
    const local = /^\.\.?\//.test(specifier) && !/\.[cm]?[jt]s(on)?$/.test(specifier)
    return nextResolve(local ? `${specifier}.ts` : specifier, context)
  },
})
globalThis.useRuntimeConfig = () => ({ wgEnabled: true, wgServerIp: '192.0.2.1' })
globalThis.useLogger = () => ({ warn() {} })
const { loadServerKeys, syncWireguardConfig } = await import(
  pathToFileURL(`${process.argv[2]}/app/server/utils/wireguard.ts`).href
)
const expected = (await readFile('/test-expected.pub', 'utf8')).trim()
const privateKey = (await readFile('/etc/wireguard/server.priv', 'utf8')).trim()
const work = await mkdtemp('/tmp/backup-control-')
process.env.VPN2_CONTROL_SOCKET = `${work}/control.sock`
const child = spawn('python3', [`${process.argv[2]}/infra/tests/backup-control.py`, process.argv[2], process.env.VPN2_CONTROL_SOCKET], { stdio: ['ignore', 'pipe', 'inherit'] })
try {
  await Promise.race([once(child.stdout, 'data'), once(child, 'exit').then(() => { throw new Error('control start failed') })])
  const keys = await loadServerKeys()
  assert.equal(keys.publicKey, expected, 'panel must read the public key from A')
  assert.equal('privateKey' in keys, false, 'server private key must stay in the control service')
  await syncWireguardConfig()
  const conf = await readFile('/etc/wireguard/wg0.conf', 'utf8')
  assert(conf.includes(`PrivateKey = ${privateKey}\n`))
}
finally {
  child.kill('SIGTERM')
  await once(child, 'exit')
  await rm(work, { recursive: true, force: true })
}
console.log('PASS: real panel loadServerKeys + syncWireguardConfig')
