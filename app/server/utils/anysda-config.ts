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

  // Файл есть, но не разбирается (пароль `12345678` стал числом, `!x` — тегом):
  // null здесь означал бы «конфига нет», и init молча завёл бы admin со
  // случайным паролем. Падаем громко — пусть деплой увидит, что шаблон битый.
  try {
    _cached = ConfigSchema.parse(parseYaml(readFileSync(path, 'utf-8')))
    return _cached
  }
  catch (err) {
    useLogger().error({ err, path }, 'anysda config is invalid')
    throw new Error(`anysda config ${path} is invalid: ${(err as Error).message}`, { cause: err })
  }
}
