import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { relative, resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

/**
 * Исполняем настоящий серверный модуль с подменой только его окружения, без Nitro.
 * Строковый путь берётся от E2E_SERVER_ROOT (иначе от cwd); SERVER_REVISION
 * читает файл из указанной ревизии git.
 */
export function serverModule<T = Record<string, unknown>>(
  file: URL | string,
  mocks: Record<string, unknown>,
  globals: Record<string, unknown> = {},
): T {
  const filename = typeof file === 'string' ? resolve(process.env.E2E_SERVER_ROOT || '.', file) : fileURLToPath(file)
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const source = process.env.SERVER_REVISION
    ? execFileSync('git', ['show', `${process.env.SERVER_REVISION}:${relative(root, filename)}`], { cwd: root, encoding: 'utf8' })
    : readFileSync(filename, 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  })
  const exports = {}
  const require = createRequire(import.meta.url)
  runInNewContext(outputText, {
    exports,
    require: (name: string) => name in mocks ? mocks[name] : require(name),
    ...globals,
  }, { filename })
  return exports as T
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
