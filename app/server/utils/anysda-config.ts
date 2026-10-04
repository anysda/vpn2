import { readFileSync, existsSync } from 'node:fs'
import { load as parseYaml } from 'js-yaml'
import { z } from 'zod'

// Стадия 30-frontend рендерит сюда только блок `admin:` (см.
// infra/configs/anysda-config.yaml.tpl); остальное панель берёт из NUXT_* env.
// Лишние ключи zod отбрасывает, поэтому схема описывает только то, что читают.
const ConfigSchema = z.object({
  admin: z
    .object({
      user: z.string().default('admin'),
      password: z.string().optional(),
    })
    .default({ user: 'admin' }),
})

export type AnysdaConfig = z.infer<typeof ConfigSchema>

let _cached: AnysdaConfig | null = null

export function loadAnysdaConfig(): AnysdaConfig | null {
  if (_cached) return _cached

  const path = useRuntimeConfig().anysdaConfigPath
  if (!existsSync(path)) {
    useLogger().warn({ path }, 'anysda config not found; using defaults')
    return null
  }

  try {
    const raw = readFileSync(path, 'utf-8')
    const parsed = parseYaml(raw)
    _cached = ConfigSchema.parse(parsed)
    return _cached
  }
  catch (err) {
    useLogger().error({ err, path }, 'failed to load anysda config')
    return null
  }
}
