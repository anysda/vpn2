// @ts-check
import withNuxt from './.nuxt/eslint.config.mjs'

export default withNuxt({
  // Файл клиентской части панели - не длиннее 400 строк: длиннее режется на части.
  files: ['app/**/*.vue', 'app/**/*.ts'],
  rules: { 'max-lines': ['error', 400] },
})
