declare module '#app' {
  interface PageMeta {
    /** Заголовок и постоянный подзаголовок раздела для мобильной шапки. */
    title?: string
    sub?: string
  }
}

/**
 * Подзаголовок раздела из данных страницы (счётчики) для мобильной шапки.
 * Пишется только в браузере: на сервере шапка layout рисуется раньше страницы,
 * и записанное значение разошлось бы с HTML при гидратации. Без аргумента — чтение.
 */
export function usePageSub(sub?: MaybeRefOrGetter<string>) {
  const state = useState('page-sub', () => ({ path: '', text: '' }))
  if (sub !== undefined && import.meta.client) {
    const path = useRoute().path
    // Только при смене текста: опрос данных не должен перерисовывать layout.
    watch(() => toValue(sub), (text) => { state.value = { path, text } }, { immediate: true })
  }
  return state
}
