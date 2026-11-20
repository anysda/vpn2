// Задание менеджера установки (infra/vpn2/src/vpn2/manager.py, job.read) и путь до него. Экраны
// shared/exits/*.vue - обычный Vue без Nuxt: панель и мастер установки передают свой транспорт.

interface ExitStage { name: string, state: 'pending' | 'running' | 'done' | 'failed', nodes: string[], failed: string[] }

interface RemoveResult { exit: string, server: 'cleaned' | 'partial' | 'forgotten', left: string[] }

export interface ExitJob {
  id: number
  kind: 'install' | 'add' | 'remove'
  tag: string
  host: string
  state: 'running' | 'done' | 'failed' | 'interrupted' | 'forgotten'
  stages: ExitStage[]
  log: string
  error: string
  actions: Array<'retry' | 'skip' | 'forget'>
  skipped: string[]
  result: RemoveResult | null
}

/** Отказ менеджера: текст - готовая фраза для человека. Прочие ошибки транспорта - нет связи. */
export class Refused extends Error {}

/** Как экран зовёт менеджер; отказ - исключение Refused. skip есть только у хоста, что ведёт установку. */
export interface ExitTransport {
  key: () => Promise<string>
  check: (tag: string, host: string) => Promise<string[]>
  add: (tag: string, host: string) => Promise<ExitJob>
  job: (id: number) => Promise<ExitJob>
  act: (id: number, action: 'retry' | 'forget') => Promise<ExitJob>
  skip?: (id: number, tag: string) => Promise<ExitJob>
}

export const OPEN_STATES: ReadonlyArray<ExitJob['state']> = ['running', 'failed', 'interrupted']
