// Связь страницы мастера с его сервером (infra/vpn2/src/vpn2/wizard.py, договор - в его docstring): весь HTTP
// мастера здесь. Обрыв связи и 502-504 (перезапуск Caddy и менеджера посреди установки) - тихий повтор раз в секунду.
import { ref } from 'vue'
import { Refused, type ExitJob, type ExitTransport } from '../ui/transport'

export interface DraftExit { tag: string, host: string, skipped: boolean, problems: string[] | null }
interface Draft {
  step: number
  admin_ssh_key: string
  exits: DraftExit[]
  panel: { domain: string, admin_user: string }
  telegram: { enabled: boolean, chat_id: string }
  backup: { target: 'local' | 's3', s3: { endpoint: string, bucket: string, region: string } }
  youtube: boolean
}
type SecretName = 'admin_password' | 'telegram_bot_token' | 's3_access_key' | 's3_secret_key'
export interface State {
  phase: 'filling' | 'installing' | 'done'
  draft: Draft
  secrets: Record<SecretName, boolean>
  backup_key: 'none' | 'shown' | 'seen'
  orchestrator_key: string
  job: ExitJob | null
}
export interface Status { panel_url: string, systems: Array<{ name: string, state: 'ok' | 'warning' | 'failed', detail: string }> }
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
      if (!response.ok) throw new Refused(data.refused || `Мастер установки ответил ошибкой ${response.status}`)
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

/** Экзит годен: проверка прошла и он не пропущен. Установка - при годном и без строк в подвешенном виде. */
/** Сервер хранит голый адрес (вход на экзит всегда root): человеку - root@адрес. */
export const login = (host: string) => host.includes('@') ? host : `root@${host}`
export const exitGood = (e: DraftExit) => !e.skipped && e.problems?.length === 0
export const exitsReady = (exits: DraftExit[]) => exits.some(exitGood) && exits.every(e => e.skipped || exitGood(e))

export const putDraft = (patch: Partial<Draft>) => call<{ draft: Draft }>('PUT', '/api/draft', patch).then(result => result.draft)
const jobCall = (method: string, path: string, body?: unknown) => call<{ job: ExitJob }>(method, path, body).then(result => job(result.job))

/** Транспорт общих экранов экзитов (app/ui). check - адрес из строки черновика с этим тегом. */
export const transport: ExitTransport = {
  key: () => readState().then(state => state.orchestrator_key),
  check: tag => call<{ problems: string[] }>('POST', '/api/exit/check', { tag }).then(result => result.problems),
  add: () => Promise.reject(new Refused('Экзит в мастере добавляется в черновик, не заданием')),
  job: () => jobCall('GET', '/api/job'),
  act: (_id, action) => action === 'retry' ? jobCall('POST', '/api/job/retry') : Promise.reject(new Refused('Мастер не забывает установку')),
  skip: (_id, tag) => jobCall('POST', '/api/job/skip', { tag }),
}
