/** Пользовательские выходы из gen-router-config; остальные теги — служебные. */
export function parseOutbound(name: string): { tag: string, variant: 'direct' | 'warp' } | null {
  if (name === 'direct-ru') return { tag: 'ru', variant: 'direct' }
  const match = name.match(/^hy2-([a-z](?:[a-z0-9-]*[a-z0-9])?)-(direct|warp)$/)
  if (!match) return null
  return { tag: match[1]!, variant: match[2] as 'direct' | 'warp' }
}
