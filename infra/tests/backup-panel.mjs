// Реальные loadServerKeys/syncWireguardConfig, без Nuxt и клиентской БД.
// Запускается только из backup-restore.py в одноразовом контейнере с Node 24.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'

registerHooks({
  resolve(specifier, context, nextResolve) {
    const stubs = {
      'drizzle-orm': 'export const and=()=>{}, eq=()=>{}, isNull=()=>{};',
      '../database/client': `export const IP_CLAIM_ATTEMPTS=1, isUniqueViolation=()=>false;
        export const useDb=()=>({select:()=>({from:()=>({innerJoin:async()=>[]})})});`,
      '../database/schema': 'export const clients={}, devices={};',
      './allowed-ips': 'export const wgAllowedIps=()=>[];',
      './client-status': 'export const isClientActive=()=>true;',
    }
    if (specifier in stubs) {
      return { url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}`, shortCircuit: true }
    }
    return nextResolve(specifier === './coalesce' ? './coalesce.ts' : specifier, context)
  },
})
globalThis.useRuntimeConfig = () => ({ wgEnabled: true, wgServerIp: '192.0.2.1' })
globalThis.useLogger = () => ({ warn() {} })
const { loadServerKeys, syncWireguardConfig } = await import(
  pathToFileURL(`${process.argv[2]}/app/server/utils/wireguard.ts`).href
)
const expected = (await readFile('/test-expected.pub', 'utf8')).trim()
const keys = await loadServerKeys()
assert.equal(keys.publicKey, expected, 'panel must read the public key from A')
await syncWireguardConfig()
const conf = await readFile('/etc/wireguard/wg0.conf', 'utf8')
assert(conf.includes(`PrivateKey = ${keys.privateKey}\n`))
console.log('PASS: real panel loadServerKeys + syncWireguardConfig')
