/** Повтор `fn` каждые `ms`, пока компонент на экране, и сразу при возврате на вкладку. */
export function usePoll(fn: () => unknown, ms: number) {
  let timer: ReturnType<typeof setInterval> | undefined
  const onShow = () => document.visibilityState === 'visible' && void fn()
  onMounted(() => {
    timer = setInterval(() => void fn(), ms)
    document.addEventListener('visibilitychange', onShow)
  })
  onUnmounted(() => {
    clearInterval(timer)
    document.removeEventListener('visibilitychange', onShow)
  })
}

/** Окно до 720px - мобильный каркас (panel.css); до монтирования - десктоп. */
export function useMobile() {
  const on = useState('mobile', () => false)
  onMounted(() => {
    const mq = matchMedia('(max-width: 720px)')
    on.value = mq.matches
    mq.onchange = () => { on.value = mq.matches }
  })
  return on
}

/** Подзаголовок раздела для шапки телефона; ставится только в браузере. */
export function usePageSub(text?: () => string) {
  const sub = useState('page-sub', () => '')
  if (text && import.meta.client) {
    watchEffect(() => { sub.value = text() })
    onUnmounted(() => { sub.value = '' })
  }
  return sub
}

export function errText(e: unknown, fallback = 'Ошибка'): string {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string } | null
  return x?.data?.message ?? x?.statusMessage ?? x?.message ?? fallback
}

/** Тост завершённой операции; `warning` сервера (Telegram не ушёл) - жёлтым со своим текстом. */
export function doneToast(title: string, warning?: string) {
  return warning ? { title, description: warning, color: 'warning' as const } : { title, color: 'success' as const }
}

type Note = string | { title: string, description?: string, color?: 'success' | 'info' | 'warning' }

/** Действие: ключ занят, пока идёт запрос; ошибка - красным тостом (`fail` объектом - свой заголовок, причина ниже), успех - тостом `done`. */
export function useAct() {
  const toast = useToast()
  const busy = ref(new Set<string>())
  async function act<R>(key: string, req: () => Promise<R>, done?: Note | ((r: R) => Note | undefined), fail?: string | { title: string }) {
    busy.value.add(key)
    try {
      const r = await req()
      const note = typeof done === 'function' ? done(r) : done
      if (note) toast.add(typeof note === 'string' ? { title: note } : note)
      return true
    }
    catch (e) {
      if (typeof fail === 'object') toast.add({ title: fail.title, description: errText(e, '') || undefined, color: 'error' })
      else toast.add({ title: errText(e, fail), color: 'error' })
      return false
    }
    finally {
      busy.value.delete(key)
    }
  }
  return { toast, busy, act }
}

/** Вопрос перед необратимым действием: окно одно на панель (components/AskDialog.vue). */
export interface Ask { title: string, text: string, ok: string, danger?: boolean, fail?: string, run: () => Promise<unknown> }
export const useAsk = () => useState<Ask | null>('ask', () => null)

/** Копирование в буфер; без Clipboard API (HTTP) - execCommand из поля внутри открытого окна. */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    if (await navigator.clipboard.writeText(text).then(() => true, () => false)) return true
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed;bottom:0;left:0;width:1px;height:1px;opacity:0'
  ;(document.querySelector('[role="dialog"]') ?? document.body).appendChild(ta)
  ta.focus()
  ta.select()
  const ok = document.activeElement === ta && document.execCommand('copy')
  ta.remove()
  return ok
}
