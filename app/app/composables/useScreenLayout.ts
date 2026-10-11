import { defaultScreenLayout } from '#shared/screen-layout'
import type { ScreenLayout } from '#shared/screen-layout'

/** Раскладка главной текущего пользователя: главная читает, /screen правит. */
export function useScreenLayout() {
  const request = useFetch<ScreenLayout>('/api/me/layout', {
    key: 'screen-layout',
    default: defaultScreenLayout,
  })
  const layout = request.data

  async function save(next: ScreenLayout) {
    layout.value = await $fetch<ScreenLayout>('/api/me/layout', { method: 'PUT', body: next })
  }

  return { layout, save, loaded: request }
}
