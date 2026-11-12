import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import { utilsImports } from './server/test/auto-imports.cjs'

// useLogger тесты подставляют глобалом.
const nitroUtils = {
  name: 'nitro-utils-imports',
  transform(code: string, id: string) {
    if (!/\/server\/(?!test\/|database\/)/.test(id) || id.includes('.test.')) return
    const imports = utilsImports(code, id, ['useLogger'])
    return imports ? imports + code : undefined
  },
}

export default defineConfig({
  plugins: [nitroUtils],
  resolve: {
    alias: { '#shared': fileURLToPath(new URL('./shared', import.meta.url)) }
  },
  test: {
    environment: 'node',
    include: ['shared/**/*.test.ts', 'server/**/*.test.ts', 'app/**/*.test.ts']
  }
})
