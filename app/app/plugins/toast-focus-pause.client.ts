/**
 * reka-ui Toast (ToastViewport) вешает `window` слушатели 'blur'/'focus' и по
 * ним ставит таймер автозакрытия тоста на паузу/снимает. Из-за этого полоса
 * прогресса «зависает», когда окно теряет фокус, и тост закрывается
 * непредсказуемо.
 *
 * Глушим именно эти два window-события: слушатель регистрируется при
 * инициализации приложения — то есть РАНЬШЕ, чем монтируется ToastViewport, —
 * поэтому в фазе bubble он срабатывает первым и `stopImmediatePropagation`
 * не даёт хендлерам reka выполниться.
 *
 * Не-capture слушатель на window для 'blur'/'focus' ловит только фактическую
 * (рас)фокусировку ОКНА — события фокуса элементов сюда в bubble-фазе не
 * доходят, так что фокус-менеджмент приложения не затрагивается. Пауза тоста
 * по наведению мыши (pointermove) у reka продолжает работать.
 */
export default defineNuxtPlugin(() => {
  const swallow = (e: Event) => e.stopImmediatePropagation()
  window.addEventListener('blur', swallow)
  window.addEventListener('focus', swallow)
})
