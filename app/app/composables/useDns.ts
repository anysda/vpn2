type Method = 'POST' | 'PATCH' | 'PUT' | 'DELETE'

/** Снимок подраздела DNS: применяется ответ последнего запроса, после отказа снимок перечитывается. */
export function useDns<T>(url: string) {
  const { toast, busy, act } = useAct()
  const load = useFetch<T>(url, { server: false, lazy: true })
  const data = load.data as Ref<T | undefined>
  let sent = 0

  async function change<R extends T = T>(key: string, method: Method, body?: object, done?: string | ((r: R) => string), path = '') {
    const my = ++sent
    const ok = await act(key, async () => {
      const v = await $fetch<R>(url + path, { method, body }) as R
      if (my === sent) data.value = v
      return v
    }, done)
    if (!ok) void load.refresh()
    return ok
  }
  return { data, load, toast, busy, act, change }
}
