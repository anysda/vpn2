const polls = new Map<string, { users: number, timer: ReturnType<typeof setInterval> }>()

/**
 * Опрос данных `useFetch` с ключом `key` одним таймером на всё приложение,
 * сколько бы компонентов ни читали этот ключ: таймер заводит первый
 * смонтированный, снимает последний. Свой таймер у каждого компонента
 * множил запросы, и каждый `refresh` обрывал запрос соседа в пути.
 */
export function useSharedPoll(key: string, ms: number) {
  const nuxtApp = useNuxtApp()
  onMounted(() => {
    const poll = polls.get(key)
    if (poll) poll.users++
    else polls.set(key, { users: 1, timer: setInterval(() => { void nuxtApp.runWithContext(() => refreshNuxtData(key)) }, ms) })
  })
  onUnmounted(() => {
    const poll = polls.get(key)
    if (!poll || --poll.users > 0) return
    clearInterval(poll.timer)
    polls.delete(key)
  })
}
