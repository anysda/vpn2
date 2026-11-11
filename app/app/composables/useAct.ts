type Note = string | { title: string, description?: string, color?: 'success' | 'info' | 'warning' }

// Одно действие оператора: пока запрос идёт, его ключ занят (кнопка выключена);
// ошибка — красным тостом (fail — текст, если у ошибки нет своего), успех —
// тостом done. Возвращает, удалось ли.
export function useAct() {
  const toast = useToast()
  const busy = ref(new Set<string>())

  async function act<R>(key: string, req: () => Promise<R>, done?: Note | ((r: R) => Note | undefined), fail?: string) {
    busy.value.add(key)
    try {
      const r = await req()
      const note = typeof done === 'function' ? done(r) : done
      if (note) toast.add(typeof note === 'string' ? { title: note } : note)
      return true
    }
    catch (e) {
      toast.add({ title: errText(e, fail), color: 'error' })
      return false
    }
    finally {
      busy.value.delete(key)
    }
  }

  return { toast, busy, act }
}
