// Одно действие оператора: пока запрос идёт, его ключ занят (кнопка выключена);
// ошибка — красным тостом, успех — тостом done. Возвращает, удалось ли.
export function useAct() {
  const toast = useToast()
  const busy = ref(new Set<string>())

  async function act<R>(key: string, req: () => Promise<R>, done?: string | ((r: R) => string)) {
    busy.value.add(key)
    try {
      const r = await req()
      if (done) toast.add({ title: typeof done === 'string' ? done : done(r) })
      return true
    }
    catch (e) {
      toast.add({ title: errText(e), color: 'error' })
      return false
    }
    finally {
      busy.value.delete(key)
    }
  }

  return { toast, busy, act }
}
