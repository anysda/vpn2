// Сессия и SSO грузятся до первого маршрута: по ним решает auth.global.
export default defineNuxtPlugin(async () => {
  const session = useAuthSession()
  if (!session.loaded.value) await Promise.all([session.fetch(), useSsoPublic().load()])
})
