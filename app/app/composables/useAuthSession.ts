/** Кто вошёл: сессия better-auth (/api/auth/get-session). */
interface PanelUser {
  id: string
  username: string
  twoFactorEnabled: boolean
}

interface SessionData {
  user: PanelUser
  session: { loginMethod?: 'password' | 'sso' | null }
}

export function useAuthSession() {
  const data = useState<SessionData | null>('auth-session', () => null)
  const loaded = useState('auth-session-loaded', () => false)
  // При SSR запрос должен уйти с cookie браузера — их пробрасывает useRequestFetch.
  const request = useRequestFetch()

  async function fetch() {
    data.value = await request<SessionData | null>('/api/auth/get-session').catch(() => null)
    loaded.value = true
  }

  function clear() {
    data.value = null
  }

  return {
    loaded,
    loggedIn: computed(() => !!data.value),
    user: computed(() => data.value?.user ?? null),
    loginMethod: computed(() => data.value?.session.loginMethod ?? 'password'),
    fetch,
    clear,
  }
}
