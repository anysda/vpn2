/** Какой поповер шапки открыт: колокольчик или меню. Открыт всегда один. */
export function useHeaderPop() {
  return useState<'bell' | 'me' | null>('header-pop', () => null)
}
