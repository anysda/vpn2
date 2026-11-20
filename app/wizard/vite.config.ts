import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { colorModeScript } from '../emergency/vite.config'

// Мастер установки: та же тема и CSS панели, что у аварийки. Статику отдаёт сервер мастера
// (vpn2.wizard), на entry она приходит в образе wizard (infra/emergency/Dockerfile).
const here = (path: string) => fileURLToPath(new URL(path, import.meta.url))

export default defineConfig({
  root: here('.'),
  plugins: [vue(), colorModeScript()],
  build: { outDir: here('dist'), emptyOutDir: true },
})
