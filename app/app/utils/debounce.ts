/** Откладывает вызов, пока события идут чаще, чем раз в ms: поиск по мере набора. */
export function debounce(fn: () => void, ms: number) {
  let timer: ReturnType<typeof setTimeout> | undefined
  return () => {
    clearTimeout(timer)
    timer = setTimeout(fn, ms)
  }
}
