// @ts-check
import withNuxt from './.nuxt/eslint.config.mjs'

// Блочные теги: пробелы вокруг текста внутри них не видны, строку можно собрать.
const BLOCK = new Set(['button', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li', 'dt', 'dd', 'option', 'td', 'th'])

/** Элемент с одним коротким текстом - одной строкой, если она не длиннее 100 знаков. */
const shortTextOneLine = {
  meta: { type: 'layout', fixable: 'whitespace', schema: [] },
  create(context) {
    const code = context.sourceCode
    if (!code.parserServices?.defineTemplateBodyVisitor) return {}
    return code.parserServices.defineTemplateBodyVisitor({
      VElement(node) {
        const { startTag, endTag } = node
        if (!BLOCK.has(node.rawName) || !endTag || startTag.loc.start.line !== startTag.loc.end.line) return
        if (!node.children.every(c => c.type === 'VText' || c.type === 'VExpressionContainer')) return
        const inner = code.text.slice(startTag.range[1], endTag.range[0])
        const text = inner.trim()
        if (!text || !inner.includes('\n') || text.includes('\n')) return
        const before = code.lines[startTag.loc.end.line - 1].slice(0, startTag.loc.end.column)
        const after = code.lines[endTag.loc.start.line - 1].slice(endTag.loc.start.column)
        if (before.length + text.length + after.length > 100) return
        context.report({
          node,
          message: 'Short text element goes on one line',
          fix: fixer => fixer.replaceTextRange([startTag.range[1], endTag.range[0]], text),
        })
      },
    })
  },
}

export default withNuxt(
  {
    // Файл клиентской части панели - не длиннее 400 строк: длиннее режется на части.
    files: ['app/**/*.vue', 'app/**/*.ts'],
    rules: { 'max-lines': ['error', 400] },
  },
  {
    files: ['app/**/*.vue'],
    plugins: { panel: { rules: { 'short-text-one-line': shortTextOneLine } } },
    rules: { 'panel/short-text-one-line': 'error' },
  },
)
