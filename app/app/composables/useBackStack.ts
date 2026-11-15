import type { Ref } from 'vue'
import type { RouteLocationRaw, Router } from 'vue-router'

// Шторка на телефоне - запись в истории браузера с меткой state.layer: системный
// «назад» (Android, жест iOS) закрывает её, а не уводит со страницы. Все шаги по
// истории идут одной очередью, чтобы закрытие шторки и уход с экрана в один такт
// не перебивали друг другу навигацию роутера.

let queue: Promise<unknown> = Promise.resolve()
let layers = 0

function enqueue(op: () => Promise<unknown>) {
  queue = queue.then(op, op).catch(() => {})
  return queue
}

const onTop = (layer?: string) => typeof history.state?.layer === 'string' && (!layer || history.state.layer === layer)

/** Шаг назад; ждёт, пока роутер доедет (секунду, если ехать некуда). */
function back(router: Router) {
  return new Promise<void>((resolve) => {
    const off = router.afterEach(() => done())
    const timer = setTimeout(done, 1000)
    function done() {
      off()
      clearTimeout(timer)
      resolve()
    }
    router.back()
  })
}

/** Открытая шторка - своя запись в истории телефона; «назад» её закрывает. */
export function useLayer(open: Ref<boolean>) {
  const router = useRouter()
  const mobile = useMobile()
  const layer = `layer-${++layers}`
  // Открыта, а своя запись ещё ждёт очереди: «назад» закрытой в тот же такт соседки - не ей.
  let pending = false
  watch(open, (now) => {
    pending = now
    return enqueue(async () => {
      pending = false
      if (now && mobile.value && !onTop(layer)) {
        const { path, query, hash } = router.currentRoute.value
        await router.push({ path, query, hash, state: { layer }, force: true })
      }
      else if (!now && onTop(layer)) {
        await back(router)
      }
    })
  })
  const closed = () => {
    if (open.value && mobile.value && !pending && !onTop(layer)) open.value = false
  }
  onMounted(() => window.addEventListener('popstate', closed))
  onBeforeUnmount(() => window.removeEventListener('popstate', closed))
}

/** Вернуться к экрану `to` под шторками: шаг назад, если он предыдущая запись, иначе замена. */
export function useLeave() {
  const router = useRouter()
  return (to: RouteLocationRaw) => enqueue(async () => {
    for (let i = 0; i < 8 && onTop(); i++) await back(router)
    const target = router.resolve(to).fullPath
    if (history.state?.back === target) await back(router)
    else await router.replace(target)
  })
}
