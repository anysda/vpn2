// Неавторизованного - на вход; при бесшовном SSO сразу в Authentik
// (серверный маршрут с 302 в IdP, поэтому external).
export default defineNuxtRouteMiddleware((to) => {
  if (to.path === '/login' || useAuthSession().loggedIn.value) return
  const { sso } = useSsoPublic()
  if (sso.value.enabled && sso.value.autoRedirect) return navigateTo('/auth/authentik', { external: true })
  return navigateTo('/login')
})
