declare module '#auth-utils' {
  interface User {
    id: number
    username: string
    totpEnabled: boolean
    // Чем человек вошёл в этот раз. Отсутствует у сессий, выданных до появления
    // SSO, — считать такие парольными.
    via?: 'password' | 'sso'
    // users.session_version на момент входа. Расхождение с БД = сессию
    // отозвали (смена пароля, вкл/выкл TOTP). Нет у сессий, выданных до
    // появления поля, — считать нулём.
    sv?: number
  }
  // Server-side session data (encrypted, never sent to client) — используем
  // для TOTP-секрета во время setup→confirm флоу, чтобы не писать секрет в
  // БД до подтверждения первым кодом. Иначе незавершённый setup лочит логин.
  interface SecureSessionData {
    pendingTotpSecret?: string
  }
}

export {}
