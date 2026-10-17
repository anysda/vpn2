import type { Router } from 'vue-router'

// Слои телефона поверх раздела — карточка клиента и нижние шторки — записи в
// истории браузера: системный «назад» (Android, жест iOS) закрывает верхний
// слой, а не уводит со страницы. Шторка помечает свою запись state.layer.
// Все шаги по истории идут одной очередью: закрытие шторки и возврат к списку
// в один такт не перебивают друг другу навигацию роутера.

let queue: Promise<unknown> = Promise.resolve()

function enqueue<T>(op: () => Promise<T>): Promise<T> {
  const next = queue.then(op, op)
  queue = next.catch(() => {})
  return next
}

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

const layerOnTop = () => typeof history.state?.layer === 'string'

export function useBackStack() {
  const router = useRouter()
  return {
    /** Открыть слой: запись с тем же адресом и меткой; вернуть её позицию в истории. */
    open: (layer: string) => enqueue(async () => {
      const { path, query, hash } = router.currentRoute.value
      await router.push({ path, query, hash, state: { layer }, force: true })
      return Number(history.state?.position)
    }),
    /** Закрыть слой кнопкой: шаг назад, если его запись ещё сверху. */
    close: (layer: string) => enqueue(async () => {
      if (history.state?.layer === layer) await back(router)
    }),
    /** Новый экран поверх (карточка клиента): обычная запись истории. Из шторки — вместо её записи. */
    push: (to: string, from?: string) => enqueue(async () => {
      if (from && history.state?.layer === from) await back(router)
      await router.push(to)
    }),
    /** Дождаться начатых шагов (закрытие шторки) перед своим переходом, иначе их «назад» его перебьёт. */
    idle: () => enqueue(async () => {}),
    /**
     * Вернуться к экрану `to` под слоями: снять шторки, затем шаг назад, если
     * `to` — предыдущая запись, иначе замена (зашли по прямой ссылке).
     */
    leave: (to: string) => enqueue(async () => {
      for (let i = 0; i < 8 && layerOnTop(); i++) await back(router)
      if (history.state?.back === to) await back(router)
      else await router.replace(to)
    }),
  }
}

/** Куда ведёт «Назад» в шапке телефона; ставит страница своего пути, читает layout. */
export function useHeaderBack() {
  return useState('header-back', () => ({ path: '', to: '' }))
}
