/** Повтор `fn` каждые `ms`, пока компонент на экране. */
export function useEvery(fn: () => unknown, ms: number) {
  let timer: ReturnType<typeof setInterval> | undefined
  onMounted(() => { timer = setInterval(() => void fn(), ms) })
  onUnmounted(() => clearInterval(timer))
}
