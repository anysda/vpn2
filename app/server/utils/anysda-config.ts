import { readFileSync, existsSync } from 'node:fs'
import { load as parseYaml } from 'js-yaml'
import { z } from 'zod'

// The panel only reads `admin:` from this YAML; all other sections live in
// the orchestrator's config.yaml (which has entry/exits/ports), but are
// stripped out before mounting into the container. So make everything else
// optional — the panel doesn't need it.
const ConfigSchema = z.object({
  entry: z
    .object({
      host: z.string(),
      password: z.string().optional(),
    })
    .optional(),
  exits: z
    .array(
      z.object({
        tag: z.string(),
        host: z.string(),
        password: z.string().optional(),
        hy2_direct_port: z.number().optional(),
      }),
    )
    .optional(),
  admin: z
    .object({
      user: z.string().default('admin'),
      password: z.string().optional(),
    })
    .default({ user: 'admin' }),
  panel: z
    .object({
      domain: z.string().optional(),
    })
    .optional(),
  telegram: z
    .object({
      bot_token: z.string().optional(),
      chat_id: z.union([z.string(), z.number()]).optional(),
    })
    .optional(),
  ports: z
    .object({
      hy2_direct: z.number().optional(),
      hy2_warp: z.number().optional(),
      mgmt: z.number().optional(),
    })
    .optional(),
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

export function clearConfigCache() {
  _cached = null
}
