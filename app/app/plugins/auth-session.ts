// Сессия и настройки SSO загружаются один раз до первого маршрута: middleware
// auth.global решает по ним, пускать ли на страницу и куда уводить. На клиент
// они приходят в payload SSR.
export default defineNuxtPlugin(async () => {
  const session = useAuthSession()
  if (!session.loaded.value) await Promise.all([session.fetch(), useSsoPublic().load()])
})
