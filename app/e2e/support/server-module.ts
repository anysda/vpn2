import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { relative } from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

/** Run the server source with controlled OS/config dependencies, without Nitro. */
export function serverModule<T>(file: URL, mocks: Record<string, unknown>, globals: Record<string, unknown> = {}): T {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const source = process.env.SERVER_REVISION
    ? execFileSync('git', ['show', `${process.env.SERVER_REVISION}:${relative(root, fileURLToPath(file))}`], { cwd: root, encoding: 'utf8' })
    : readFileSync(file, 'utf8')
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
