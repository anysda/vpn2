const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const { createRequire } = require('node:module')

// Реальные drizzle/libsql и код очереди; подменена только граница VPN-служб.
module.exports = function harness(root, database, adapter, runtime = {}) {
  const external = createRequire(path.resolve('package.json'))
  const cache = new Map()
  const overrides = new Map([
    ['app/server/utils/access-adapter.ts', { accessAdapter: adapter }],
    ['app/server/utils/telegram-config.ts', { botConfigured: () => true }],
  ])
  function load(relative) {
    if (overrides.has(relative)) return overrides.get(relative)
    if (cache.has(relative)) return cache.get(relative).exports
    const filename = path.join(root, relative)
    if (filename.endsWith('.json')) return JSON.parse(fs.readFileSync(filename, 'utf8'))
    const source = fs.readFileSync(filename, 'utf8')
    const code = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText
    const mod = { exports: {} }
    cache.set(relative, mod)
    const localRequire = (name) => {
      if (!name.startsWith('.')) return external(name)
      let target = path.normalize(path.join(path.dirname(relative), name))
      if (!path.extname(target)) target += '.ts'
      return load(target)
    }
    const execute = vm.runInThisContext(`(function(require,module,exports,useRuntimeConfig,createError,useLogger,defineEventHandler,getRouterParam,getHeader,getQuery,setResponseStatus){${code}\n})`, { filename })
    execute(localRequire, mod, mod.exports, () => ({ databaseUrl: `file:${database}`, ...runtime }),
      input => Object.assign(new Error(input.message || input.statusMessage), input),
      () => ({ info() {}, warn() {}, error() {} }), handler => handler,
      (event, name) => event.params[name], (event, name) => event.headers?.[name],
      event => event.query || {}, (event, status) => { event.status = status })
    return mod.exports
  }
  // Подмена части модуля: остальные экспорты настоящие, новый экспорт не ломает тест.
  // Настоящий модуль грузится сразу, его зависимости берутся из подмен на этот момент.
  function patch(relative, replaced) {
    overrides.delete(relative)
    overrides.set(relative, { ...load(relative), ...replaced })
  }
  return { load, overrides, patch }
}
