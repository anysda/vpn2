export default defineNuxtRouteMiddleware((to) => {
  if (to.path === '/login') return

  const { loggedIn } = useUserSession()
  if (loggedIn.value) return

  // Бесшовность: неавторизованного уносим прямо в Authentik, формы он не видит
  // вообще. external — потому что /auth/authentik это серверный маршрут (там
  // 302 в IdP), а не страница Nuxt.
  const { sso } = useRuntimeConfig().public
  if (sso.enabled && sso.autoRedirect) {
    return navigateTo('/auth/authentik', { external: true })
  }

  return navigateTo('/login')
})
