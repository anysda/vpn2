import { tabDirection } from '~/utils/tab-pages'

const EASE = 'cubic-bezier(.23,1,.32,1)'

// Переход между разделами шапки как в макете: старая страница уходит, новая
// въезжает со стороны пункта меню (View Transitions). Без поддержки, при
// «уменьшить движение» и при жесте «назад» самого браузера — обычная смена.
export default defineNuxtPlugin((nuxtApp) => {
  const html = document.documentElement
  const reduced = matchMedia('(prefers-reduced-motion: reduce)')
  const router = useRouter()
  let finishPage: (() => void) | undefined
  let uaTransition = false

  addEventListener('popstate', (e) => {
    uaTransition = (e as PopStateEvent & { hasUAVisualTransition?: boolean }).hasUAVisualTransition ?? false
  })

  router.beforeResolve(async (to, from) => {
    const dir = tabDirection(from.path, to.path)
    const skip = !dir || reduced.matches || uaTransition || !document.startViewTransition
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
    cascade(document.querySelector<HTMLElement>('.tab.on'))
  })

  // Карточки раздела поднимаются лесенкой; те, что ниже экрана, — при прокрутке.
  const reveal = 'IntersectionObserver' in window && !reduced.matches
    ? new IntersectionObserver((entries) => {
        let k = 0
        entries.forEach((e) => {
          if (!e.isIntersecting) return
          reveal?.unobserve(e.target)
          e.target.classList.remove('rv')
          e.target.animate([{ opacity: 0, transform: 'translateY(18px)' }, { opacity: 1, transform: 'none' }],
            { duration: 560, delay: k++ * 60, easing: EASE, fill: 'backwards' })
        })
      }, { rootMargin: '0px 0px -6% 0px' })
    : null

  function cascade(tab: HTMLElement | null) {
    if (reduced.matches || !tab || tab.classList.contains('login')) return
    const cards = [...tab.querySelectorAll<HTMLElement>('.card, .card-ui')]
      .filter(c => c.offsetWidth && !c.parentElement?.closest('.card, .card-ui'))
    let k = 0
    cards.forEach((c) => {
      if (c.getBoundingClientRect().top < innerHeight) {
        c.animate([{ opacity: 0, transform: 'translateY(22px)' }, { opacity: 1, transform: 'none' }],
          { duration: 520, delay: 60 + Math.min(k++, 8) * 55, easing: EASE, fill: 'backwards' })
      }
      else if (reveal && !c.dataset.rv) {
        c.dataset.rv = '1'
        c.classList.add('rv')
        reveal.observe(c)
      }
    })
  }
})
