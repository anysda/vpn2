/** Период графиков Обзора: сутки по часам или неделя по суткам. Кнопка — на странице, на телефоне — в шапке. */
export function useOverviewPeriod() {
  return useState<'day' | 'week'>('overview-period', () => 'day')
}
