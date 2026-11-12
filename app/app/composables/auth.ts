interface SessionData {
  user: { id: string, username: string, twoFactorEnabled: boolean }
  session: { loginMethod?: 'password' | 'sso' | null }
}

export function useAuthSession() {
  const data = useState<SessionData | null>('auth-session', () => null)
  const loaded = useState('auth-session-loaded', () => false)
  const request = useRequestFetch()
  async function fetch() {
    data.value = await request<SessionData | null>('/api/auth/get-session').catch(() => null)
    loaded.value = true
  }
  return {
    loaded,
    loggedIn: computed(() => !!data.value),
    user: computed(() => data.value?.user ?? null),
    loginMethod: computed(() => data.value?.session.loginMethod ?? 'password'),
    fetch,
    clear: () => { data.value = null },
  }
}

const SSO_OFF = { enabled: false, autoRedirect: false, label: 'Authentik' }

export function useSsoPublic() {
  const sso = useState('sso-public', () => SSO_OFF)
  const request = useRequestFetch()
  async function load() {
    sso.value = await request<typeof SSO_OFF>('/api/sso').catch(() => SSO_OFF)
  }
  return { sso, load }
}
