import type { Serialize, Simplify } from 'nitropack/types'
import type { api } from '../server/plugins/api'
type Table = typeof api
type Routes = {
  [P in keyof Table as P extends `${string} ${infer Path}` ? `/api${Path}` : never]: {
    [K in keyof Table as K extends `${infer M} ${infer Q}` ? (P extends `${string} ${Q}` ? Lowercase<M> : never) : never]: Simplify<Serialize<Awaited<ReturnType<Table[K]>>>>
  }
}
declare module 'nitropack/types' {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface InternalApi extends Routes {}
}
declare module '#app' {
  /** Заголовок и постоянный подзаголовок раздела для шапки телефона. */
  interface PageMeta { title?: string, sub?: string }
}
