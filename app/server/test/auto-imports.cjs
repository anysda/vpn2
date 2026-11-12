const fs = require('node:fs')
const path = require('node:path')

const utilsDir = path.join(__dirname, '../utils')
const utilsExports = new Map()
for (const file of fs.readdirSync(utilsDir).filter(f => f.endsWith('.ts') && !f.endsWith('.test.ts'))) {
  for (const [, name] of fs.readFileSync(path.join(utilsDir, file), 'utf8').matchAll(/^export (?:async )?(?:function|const|class) (\w+)/gm)) {
    utilsExports.set(name, path.join(utilsDir, file.slice(0, -3)))
  }
}

// Автоимпорты Nitro из server/utils, которыми пользуется исходник, строками `import` с
// относительным путём: подмена модуля утилит в тесте действует на них, как на обычный импорт.
exports.utilsImports = function utilsImports(source, filename, globals = []) {
  const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')
  const lines = []
  for (const [name, file] of utilsExports) {
    if (file === filename.replace(/\.ts$/, '') || globals.includes(name)) continue
    if (!new RegExp(`(?<![\\w$])(?<![^.]\\.)${name}\\b`).test(code)) continue
    if (new RegExp(`(?:function|const|let|class)\\s+${name}\\b|import[^;'"]*\\b${name}\\b[^;'"]*from`).test(code)) continue
    const relative = path.relative(path.dirname(filename), file)
    lines.push(`import { ${name} } from '${relative.startsWith('.') ? relative : `./${relative}`}'\n`)
  }
  return lines.join('')
}
