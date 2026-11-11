// Подраздел DNS: один снимок на сервере, каждое изменение возвращает новый
// снимок, применяется ответ последнего запроса (latestWins).
export function useDnsSection<T>(url: string) {
  const { toast, busy, act } = useAct()
  const load = useFetch<T>(url, { server: false, lazy: true })
  const data = load.data as Ref<T | undefined>
  const latest = latestWins<T>(v => (data.value = v), () => $fetch<T>(url) as Promise<T>)

  const api = <R extends T = T>(method: 'POST' | 'PATCH' | 'PUT' | 'DELETE', body?: object, path = '') =>
    () => $fetch<R>(url + path, { method, body }) as Promise<R>
  const change = <R extends T>(key: string, req: () => Promise<R>, done?: string | ((r: R) => string)) =>
    act(key, () => latest(req), done)

  /** Удаление строки: после ответа строка гаснет и только потом уходит из списка. */
  const removeRow = (row: HTMLElement | null, body: object, done: string) => change('del', async () => {
    const v = await api('DELETE', body)()
    await anim(row, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], { duration: 160, easing: EO, fill: 'forwards' })?.finished
    return v
  }, done)

  return { data, load, toast, busy, act, api, change, removeRow }
}
