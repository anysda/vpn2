/**
 * Trigger `refresh` whenever the tab returns to the foreground.
 * Browsers throttle `setInterval` aggressively on hidden tabs, so polling
 * data goes stale; firing on `visibilitychange` gives users instant fresh
 * data the moment they refocus the panel.
 */
export function useVisibleRefresh(refresh: () => unknown): void {
  if (typeof document === 'undefined') return
  const handler = () => {
    if (document.visibilityState === 'visible') void refresh()
  }
  onMounted(() => document.addEventListener('visibilitychange', handler))
  onUnmounted(() => document.removeEventListener('visibilitychange', handler))
}
