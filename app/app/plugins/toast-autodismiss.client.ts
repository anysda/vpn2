/**
 * Тосты Nuxt UI (reka-ui) ставят таймер автозакрытия на паузу, когда окно
 * теряет фокус — полоса прогресса «зависает» и тост не закрывается.
 *
 * Плагин дублирует закрытие фокус-независимым setTimeout: для каждого нового
 * тоста через его длительность вызывается remove(), даже если окно неактивно.
 */
export default defineNuxtPlugin(() => {
  const { toasts, remove } = useToast()
  const scheduled = new Set<string>()

  watch(
    toasts,
    (list) => {
      for (const t of list) {
        if (!t.id || scheduled.has(t.id)) continue
        scheduled.add(t.id)
        const duration = typeof t.duration === 'number' ? t.duration : 5000
        if (duration <= 0) continue
        setTimeout(() => {
          remove(t.id)
          scheduled.delete(t.id)
        }, duration)
      }
    },
    { deep: true, immediate: true },
  )
})
