// Без входа - на /login; при бесшовном SSO - на серверный маршрут Authentik (external).
export default defineNuxtRouteMiddleware((to) => {
  if (to.path === '/login' || useAuthSession().loggedIn.value) return
  const { sso } = useSsoPublic()
  if (sso.value.enabled && sso.value.autoRedirect) return navigateTo('/auth/authentik', { external: true })
  return navigateTo('/login')
})
