import { readFileSync, statSync } from 'node:fs'

export interface PanelNode { tag: string, host: string, mgmt: string }

let cache: { stamp: string, nodes: PanelNode[] } | null = null
let badStamp = ''

const isNode = (n: unknown): n is PanelNode => !!n && typeof n === 'object'
  && ['tag', 'host', 'mgmt'].every(k => typeof (n as Record<string, unknown>)[k] === 'string')

/**
 * Узлы установки, entry первой: runtime/nodes.json пишет стадия 30-frontend при каждой смене состава.
 * Читается на ходу: добавленный или удалённый экзит виден без перезапуска панели. Нет файла - пусто;
 * битый файл - прежний удачный снимок, а не пустой список (пустой снял бы закрепления клиентов).
 */
export function panelNodes(): PanelNode[] {
  const path = String(useRuntimeConfig().nodesFilePath)
  let stamp: string
  try {
    const s = statSync(path)
    stamp = `${s.ino}:${s.mtimeMs}:${s.size}`
  }
  catch {
    return []
  }
  if (cache?.stamp === stamp || badStamp === stamp) return cache?.nodes ?? []
  try {
    const nodes: unknown = JSON.parse(readFileSync(path, 'utf-8'))
    if (!Array.isArray(nodes) || !nodes.every(isNode)) throw new Error('not a node list')
    cache = { stamp, nodes }
  }
  catch (err) {
    badStamp = stamp
    useLogger().warn({ err, path }, 'nodes file unreadable')
  }
  return cache?.nodes ?? []
}

export const exitNodes = () => panelNodes().filter(n => n.tag !== 'ru')
