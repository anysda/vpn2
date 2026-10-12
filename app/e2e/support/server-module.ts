import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

/** Run the server source with controlled OS/config dependencies, without Nitro. */
export function serverModule<T>(file: URL, mocks: Record<string, unknown>, globals: Record<string, unknown> = {}): T {
  const source = readFileSync(file, 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  const require = createRequire(import.meta.url)
  runInNewContext(outputText, {
    exports,
    require: (name: string) => name in mocks ? mocks[name] : require(name),
    ...globals,
  }, { filename: file.pathname })
  return exports as T
}
