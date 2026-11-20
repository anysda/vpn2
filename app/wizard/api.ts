// Связь страницы мастера с его сервером (infra/vpn2/src/vpn2/wizard.py): весь HTTP мастера здесь.
// Обрыв связи и 502-504 (перезапуск Caddy и менеджера посреди установки) - тихий повтор раз в секунду.
import { ref } from 'vue'
import { Refused, type ExitJob, type ExitTransport } from '../ui/transport'

export interface DraftExit { tag: string, host: string, skipped: boolean, problems: string[] | null }
interface Draft {
  admin_ssh_key: string
  exits: DraftExit[]
  panel: { domain: string | null, admin_user: string }
  telegram: { enabled: boolean, chat_id: string }
  backup: { target: 'local' | 's3', s3: { endpoint: string, bucket: string, region: string } }
  youtube: boolean
}
type SecretName = 'admin_password' | 'telegram_bot_token' | 's3_access_key' | 's3_secret_key'
export interface State {
  phase: 'filling' | 'installing' | 'done'
  screen: number
  draft: Draft
  secrets: Record<SecretName, boolean>
  backup_key: 'none' | 'unseen' | 'seen'
  orchestrator_key: string
  job: ExitJob | null
}
export interface Status { panel_url: string, systems: Array<{ name: string, state: 'ok' | 'warn' | 'fail', detail: string }> }
/** Что экран отдаёт по «Далее»: секреты пишутся первыми, then - после черновика. */
export interface Patch { patch?: Partial<Draft>, secrets?: Partial<Record<SecretName, string>>, then?: () => Promise<unknown> }

/** Сессии нет: ссылка без токена, чужая, просроченная или её сменил новый ./deploy.sh. */
class Expired extends Error {}
export const expired = ref(false)
/** С какого момента нет связи с сервером мастера; null - связь есть. */
export const offlineSince = ref<number | null>(null)

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = body === undefined ? { method } : { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
  for (;;) {
    const response = await fetch(path, init).catch(() => null)
    if (response && ![502, 503, 504].includes(response.status)) {
      offlineSince.value = null
      if (response.status === 401) {
        expired.value = true
        throw new Expired()
      }
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Refused(data.message || `Мастер установки ответил ошибкой ${response.status}`)
      return data as T
    }
    offlineSince.value ??= Date.now()
    await wait(1000)
  }
}

/** Текст отказа для экрана; сессия кончилась - пусто, страница уже показывает «ссылка устарела». */
export function refusal(e: unknown): string {
  if (e instanceof Refused) return e.message
  if (e instanceof Expired) return ''
  throw e
}

// «Забыть» установку мастер не умеет: выйти из неё можно только повтором или пропуском экзита.
const job = (next: ExitJob): ExitJob => ({ ...next, actions: next.actions.filter(action => action !== 'forget') })

export const readState = () => call<State>('GET', '/api/state').then(state => ({ ...state, job: state.job && job(state.job) }))

/** Токен из фрагмента ссылки - один раз в обмен на cookie, затем фрагмент долой из адресной строки. */
export async function start() {
  const token = location.hash.slice(1)
  if (!token) return
  try {
    await call('POST', '/api/session', { token })
  }
  finally {
    history.replaceState(history.state, '', location.pathname + location.search)
  }
}

/** Транспорт общих экранов экзитов (app/ui): проверка экзита и ход установки. */
export const transport: ExitTransport = {
  key: () => readState().then(state => state.orchestrator_key),
  check: (tag, host) => call<{ problems: string[] }>('POST', '/api/exit/check', { tag, host }).then(result => result.problems),
  add: () => Promise.reject(new Refused('Экзит в мастере добавляется в черновик, не заданием')),
  job: () => call<ExitJob>('GET', '/api/job').then(job),
  act: (_id, action) => action === 'retry' ? call<ExitJob>('POST', '/api/job/retry').then(job) : Promise.reject(new Refused('Мастер не забывает установку')),
  skip: (_id, tag) => call<ExitJob>('POST', '/api/job/skip', { tag }).then(job),
}
