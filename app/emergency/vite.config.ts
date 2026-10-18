import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [vue()],
  resolve: { alias: { '#shared': fileURLToPath(new URL('../shared', import.meta.url)) } },
  build: {
    outDir: fileURLToPath(new URL('../../infra/emergency/vpn2_emergency/static', import.meta.url)),
    emptyOutDir: true,
    rollupOptions: { input: {
      panel: fileURLToPath(new URL('./index.html', import.meta.url)),
      instructions: fileURLToPath(new URL('./instructions.html', import.meta.url)),
    } },
  },
})
