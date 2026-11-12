import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, relative, resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { utilsImports } from '../../server/test/auto-imports.cjs'

/**
 * Исполняем настоящий серверный модуль с подменой только его окружения, без Nitro.
 * Строковый путь берётся от E2E_SERVER_ROOT (иначе от cwd); SERVER_REVISION
 * читает файл из указанной ревизии git.
 */
export function serverModule<T = Record<string, unknown>>(
  file: URL | string,
  mocks: Record<string, unknown>,
  globals: Record<string, unknown> = {},
  lazy = false,
  cache = new Map<string, object>(),
): T {
  const filename = typeof file === 'string' ? resolve(process.env.E2E_SERVER_ROOT || '.', file) : fileURLToPath(file)
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const raw = process.env.SERVER_REVISION
    ? execFileSync('git', ['show', `${process.env.SERVER_REVISION}:${relative(root, filename)}`], { cwd: root, encoding: 'utf8' })
    : readFileSync(filename, 'utf8')
  const source = (filename.includes('/server/database/') ? '' : utilsImports(raw, filename, Object.keys(globals))) + raw
  // esModuleInterop выключен явно (в TypeScript 6 он включён по умолчанию):
  // `import * as x` остаётся простым require, как пространство имён в сборке
  // Nitro, без лишнего ключа default, на котором спотыкается drizzle.
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: false },
    fileName: filename,
  })
  // Модуль виден соседям до исполнения, как в CommonJS: круговые импорты утилит не зацикливаются.
  const exports = {}
  cache.set(filename, exports)
  const require = createRequire(import.meta.url)
  runInNewContext(outputText, {
    exports,
    require: (name: string) => {
      if (name in mocks) return mocks[name]
      // Относительный импорт без подмены (например, общий list-order) исполняем
      // настоящим модулем и теми же подменами, а не ищем от каталога загрузчика.
      const sibling = name.startsWith('.') ? `${resolve(dirname(filename), name)}.ts` : ''
      if (sibling && (process.env.SERVER_REVISION || existsSync(sibling))) {
        const load = () => cache.get(sibling) ?? serverModule<object>(sibling, mocks, globals, lazy, cache)
        if (!lazy && !sibling.includes('/server/utils/')) return load()
        let loaded: object | undefined
        return new Proxy({}, { get: (_, key) => Reflect.get(loaded ??= load(), key) })
      }
      return require(name)
    },
    ...globals,
  }, { filename })
  return exports as T
}

/**
 * Обработчик из таблицы маршрутов `server/resources/<resource>.ts` по ключу
 * `МЕТОД /путь`. Подмена `../utils/x` действует и на `./x` внутри utils;
 * модуль без подмены исполняется настоящим при первом обращении к нему.
 */
export function apiRoute<T>(resource: string, key: string, mocks: Record<string, unknown>, globals: Record<string, unknown> = {}): T {
  const all = { ...mocks }
  for (const [name, mock] of Object.entries(mocks)) {
    if (name.startsWith('../utils/')) all[name.replace('../utils/', './')] = mock
  }
  const file = new URL(`../../server/resources/${resource}.ts`, import.meta.url)
  return serverModule<{ default: Record<string, T> }>(file, all, globals, true).default[key]!
}

/** Ключи `МЕТОД /путь` всех таблиц маршрутов `server/resources`. */
export function apiRouteKeys(): string[] {
  const dir = fileURLToPath(new URL('../../server/resources/', import.meta.url))
  return readdirSync(dir).flatMap(file =>
    [...readFileSync(resolve(dir, file), 'utf8').matchAll(/^ {2}'([A-Z]+ \/[^']*)': /gm)].map(match => match[1]!))
}

export class FakeClock {
  now = 0
  private serial = 0
  private timers = new Map<number, { at: number, repeat: number, callback: () => void }>()

  setTimeout = (callback: () => void, delay: number) => this.add(callback, delay, 0)
  setInterval = (callback: () => void, delay: number) => this.add(callback, delay, delay)
  clearTimeout = (id: number) => { this.timers.delete(id) }
  clearInterval = this.clearTimeout

  private add(callback: () => void, delay: number, repeat: number) {
    const id = ++this.serial
    this.timers.set(id, { at: this.now + delay, repeat, callback })
    return id
  }

  async advance(ms: number) {
    const end = this.now + ms
    for (;;) {
      const next = [...this.timers].sort((a, b) => a[1].at - b[1].at)[0]
      if (!next || next[1].at > end) break
      const [id, timer] = next
      this.now = timer.at
      if (timer.repeat) timer.at += timer.repeat
      else this.timers.delete(id)
      timer.callback()
      await this.flush()
    }
    this.now = end
    await this.flush()
  }

  async flush() {
    // Даём цепочке async-шагов тика завершиться; таймеры остаются под контролем теста.
    for (let i = 0; i < 50; i++) await Promise.resolve()
  }
}
