/**
 * Профиль переехал из /me в /settings; старые закладки и ссылки ведут туда же.
 * Запрос сохраняется, якорь браузер переносит сам: в Location его нет.
 */
export default defineEventHandler((event) => {
  if (event.method !== 'GET' && event.method !== 'HEAD') return
  const [path, query] = event.path.split(/\?(.*)/s)
  if (path !== '/me' && path !== '/me/') return
  return sendRedirect(event, query ? `/settings?${query}` : '/settings', 301)
})
