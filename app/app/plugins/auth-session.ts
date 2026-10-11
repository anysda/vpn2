// Сессия загружается один раз до первого маршрута: middleware auth.global
// решает по ней, пускать ли на страницу. На клиент она приходит в payload SSR.
export default defineNuxtPlugin(async () => {
  const session = useAuthSession()
  if (!session.loaded.value) await session.fetch()
})
