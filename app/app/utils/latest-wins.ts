// Изменения одного раздела уходят подряд, ответы приходят в любом порядке, и
// каждый ответ — снимок всего раздела на момент своего запроса. Применяем
// только ответ последнего отправленного запроса. Если запросы перекрывались
// или один упал, после последнего ответа раздел перечитываем: сервер мог
// посчитать последний ответ раньше, чем применил предыдущее изменение.
export function latestWins<T>(apply: (v: T) => void, reload: () => Promise<T>) {
  let sent = 0
  let open = 0
  let stale = false

  async function sync() {
    const my = ++sent
    try {
      const v = await reload()
      if (my === sent) apply(v)
    }
    catch {
      // Раздел остаётся как есть: его поправит ответ следующего изменения.
    }
  }

  return async function run<R extends T>(req: () => Promise<R>): Promise<R> {
    const my = ++sent
    if (open++) stale = true
    try {
      const v = await req()
      if (my === sent) apply(v)
      return v
    }
    catch (e) {
      stale = true
      throw e
    }
    finally {
      if (!--open && stale) {
        stale = false
        void sync()
      }
    }
  }
}
