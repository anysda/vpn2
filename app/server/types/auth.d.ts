declare module '#auth-utils' {
  interface User {
    id: number
    username: string
    totpEnabled: boolean
  }

  interface UserSession {
    pendingTotp?: {
      userId: number
      issuedAt: number
    }
  }
}

export {}
