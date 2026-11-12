const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const { createRequire } = require('node:module')
const { utilsImports } = require('../../server/test/auto-imports.cjs')

// Реальные drizzle/libsql и код очереди; подменена только граница VPN-служб.
module.exports = function harness(root, database, adapter, runtime = {}) {
  const external = createRequire(path.resolve('package.json'))
  const cache = new Map()
  const overrides = new Map([
    ['app/server/utils/access-adapter.ts', { accessAdapter: adapter }],
  ])
  function load(relative) {
    if (overrides.has(relative)) return overrides.get(relative)
    if (cache.has(relative)) return cache.get(relative).exports
    const filename = path.join(root, relative)
    if (filename.endsWith('.json')) return JSON.parse(fs.readFileSync(filename, 'utf8'))
    const raw = fs.readFileSync(filename, 'utf8')
    const source = (relative.includes('/database/') ? '' : utilsImports(raw, filename, ['useLogger'])) + raw
    const code = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText
    const mod = { exports: {} }
    cache.set(relative, mod)
    const localRequire = (name) => {
      if (!name.startsWith('.')) return external(name)
      let target = path.normalize(path.join(path.dirname(relative), name))
      if (!path.extname(target)) target += '.ts'
      // Утилиты берутся при обращении: подмена, поставленная после загрузки соседа, действует и в нём.
      if (target.includes('server/utils/')) return new Proxy({}, { get: (_, key) => load(target)[key] })
      return load(target)
    }
    const execute = vm.runInThisContext(`(function(require,module,exports,useRuntimeConfig,createError,useLogger,defineEventHandler,getRouterParam,getHeader,getQuery,setResponseStatus,setHeader){${code}\n})`, { filename })
    execute(localRequire, mod, mod.exports, () => ({ databaseUrl: `file:${database}`, ...runtime }),
      input => Object.assign(new Error(input.message || input.statusMessage), input),
      () => ({ info() {}, warn() {}, error() {} }), handler => handler,
      (event, name) => event.params[name], (event, name) => event.headers?.[name],
      event => event.query || {}, (event, status) => { event.status = status },
      (event, name, value) => { event.responseHeaders = { ...event.responseHeaders, [name]: value } })
    return mod.exports
  }
  // Подмена части модуля: остальные экспорты настоящие, новый экспорт не ломает тест.
  // Повторная подмена того же модуля дополняет прежнюю.
  function patch(relative, replaced) {
    overrides.set(relative, { ...load(relative), ...replaced })
  }
  // Обработчик из таблицы маршрутов: route('clients PATCH /clients/:id').
  function route(name) {
    const [resource, key] = name.split(/ (.*)/)
    return load(`app/server/resources/${resource}.ts`).default[key]
  }
  // Бот настроен: секрет в конфиге и файл Telegram с токеном и чатом.
  runtime = { tgbotSecret: 'test', ...runtime }
  const { readJsonFile } = load('app/server/utils/api.ts')
  patch('app/server/utils/api.ts', {
    readJsonFile: file => file.endsWith('telegram-runtime.json') ? { bot_token: 'test', chat_id: 1 } : readJsonFile(file),
  })
  return { load, overrides, patch, route }
}
