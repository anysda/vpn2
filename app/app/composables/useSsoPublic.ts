/** Вход через Authentik для страницы входа: настраивается в профиле, без передеплоя. */
interface SsoPublic {
  enabled: boolean
  autoRedirect: boolean
  label: string
}

export function useSsoPublic() {
  const sso = useState<SsoPublic>('sso-public', () => ({ enabled: false, autoRedirect: false, label: 'Authentik' }))
  const request = useRequestFetch()

  async function load() {
    // Не прочиталось — показываем форму: пароль остаётся запасным входом.
    sso.value = await request<SsoPublic>('/api/sso').catch(() => ({ enabled: false, autoRedirect: false, label: 'Authentik' }))
  }

  return { sso, load }
}
