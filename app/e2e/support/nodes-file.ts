import { mkdtempSync, renameSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

type Node = { tag: string, mgmt?: string, host?: string }

/** runtime/nodes.json, как его пишет стадия 30-frontend (новый файл переименованием); путь - в nodesFilePath. */
export function nodesFile(nodes: Node[], path = join(mkdtempSync(join(tmpdir(), 'nodes-')), 'nodes.json')): string {
  writeFileSync(`${path}.new`, JSON.stringify(nodes.map(n => ({ tag: n.tag, host: n.host ?? '', mgmt: n.mgmt ?? '' }))))
  renameSync(`${path}.new`, path)
  return path
}
