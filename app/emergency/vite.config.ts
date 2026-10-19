import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { colorMode } from '../shared/color-mode'

// Тема как в основной панели: скрипт @nuxtjs/color-mode с её настройками
// (подстановка как в самом модуле) ставит data-theme до отрисовки. Отдельный
// файл в <head>: CSP экзита запрещает встроенные скрипты. Выбор «как в системе»
// следует смене темы системы без перезагрузки, как plugin.client модуля.
function colorModeScript(): Plugin {
  const options: Record<string, string> = colorMode
  const template = readFileSync(new URL('script.min.js', import.meta.resolve('@nuxtjs/color-mode')), 'utf8')
  const script = template.replace(/<%= options\.([^ ]+) %>/g, (_, option: string) => {
    if (!(option in options)) throw new Error(`color-mode: нет настройки ${option} в shared/color-mode.ts`)
    return options[option]!
  }).trim()
  const follow = `;(()=>{const m=window[${JSON.stringify(colorMode.globalName)}];if(m.preference==='system'&&window.matchMedia)`
    + `matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{m.removeColorScheme(m.value);m.value=m.getColorScheme();m.addColorScheme(m.value)})})()`
  const source = `${script}\n${follow}\n`
  const path = 'assets/color-mode.js'
  return {
    name: 'emergency-color-mode',
    transformIndexHtml: (_html, context) => [context.server
      ? { tag: 'script', children: source, injectTo: 'head-prepend' }
      : { tag: 'script', attrs: { src: `/${path}` }, injectTo: 'head-prepend' }],
    generateBundle() { this.emitFile({ type: 'asset', fileName: path, source }) },
  }
}

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [vue(), colorModeScript()],
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
