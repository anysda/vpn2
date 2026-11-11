import { tabDirection } from '~/utils/tab-pages'

// Переход между разделами шапки как в макете: старая страница уходит, новая
// въезжает со стороны пункта меню (View Transitions). Без поддержки, на телефоне,
// при «уменьшить движение» и при жесте «назад» самого браузера — обычная смена.
export default defineNuxtPlugin((nuxtApp) => {
  const html = document.documentElement
  // На телефоне вкладка только проявляется (main.css), без въезда сбоку.
  const phone = matchMedia('(max-width: 720px)')
  const router = useRouter()
  let finishPage: (() => void) | undefined
  let uaTransition = false

  addEventListener('popstate', (e) => {
    uaTransition = (e as PopStateEvent & { hasUAVisualTransition?: boolean }).hasUAVisualTransition ?? false
  })

  router.beforeResolve(async (to, from) => {
    const dir = tabDirection(from.path, to.path)
    const skip = !dir || reducedMotion() || phone.matches || uaTransition || !document.startViewTransition
    uaTransition = false
    if (skip) return

    const pageShown = new Promise<void>((resolve) => { finishPage = resolve })
    let changeRoute!: () => void
    const snapshotTaken = new Promise<void>((resolve) => { changeRoute = resolve })
    html.dataset.vt = 'page'
    html.dataset.dir = dir
    const transition = document.startViewTransition(() => {
      changeRoute()
      return pageShown
    })
    // Следующий переход отменяет незаконченный: отказ ready — штатный, не ошибка.
    transition.ready.catch(() => {})
    void transition.finished.finally(() => {
      delete html.dataset.vt
      delete html.dataset.dir
      // Снятый data-vt заново запускает проявление вкладки — без этого она
      // мигнула бы после въезда.
      document.querySelectorAll('.tab.on').forEach(el => el.getAnimations().forEach((a) => {
        if ((a as CSSAnimation).animationName === 'tfade') a.finish()
      }))
    })
    await snapshotTaken
  })

  const release = () => {
    finishPage?.()
    finishPage = undefined
  }
  router.afterEach((_to, _from, failure) => {
    if (failure) release()
  })
  router.onError(release)
  nuxtApp.hook('vue:error', release)
  nuxtApp.hook('app:error', release)
  nuxtApp.hook('page:finish', () => {
    release()
    // Вход и первая сцена Обзора выводят карточки сами.
    const tab = document.querySelector<HTMLElement>('.tab.on')
    if (!tab?.classList.contains('login') && !tab?.hasAttribute('data-wake')) cascade(tab)
  })
})
