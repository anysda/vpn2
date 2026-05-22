declare module '#auth-utils' {
  interface User {
    id: number
    username: string
    totpEnabled: boolean
  }
}

export {}
