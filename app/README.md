# anysda-vpn2 — панель

Nuxt 4 веб-панель управления VPN-стеком (WireGuard + OpenVPN). Часть
проекта `anysda-vpn2` — общий README и архитектура в корне репозитория.

## Разработка

```bash
pnpm install        # зависимости
pnpm dev            # dev-сервер на http://localhost:3000
pnpm build          # прод-сборка (.output/)
pnpm typecheck      # проверка типов
pnpm lint           # eslint
pnpm db:generate    # сгенерировать drizzle-миграцию из schema.ts
```

## Стек

- Nuxt 4 + Vue 3 (TS strict), Nuxt UI 3
- Nitro server routes, nuxt-auth-utils (session + Argon2id), свой TOTP
- Drizzle ORM + libSQL (SQLite), миграции в `server/database/migrations/`

## Деплой

Образ собирает GitHub Actions (`.github/workflows/images.yml`) на каждый пуш в `dev`/`main`
и кладёт в `ghcr.io/anysda/vpn2/panel` с тегом ветки; нода тянет его (стадия `30-frontend`).
Собрать руками:

```bash
docker buildx build --platform linux/amd64 --push \
  -t ghcr.io/anysda/vpn2/panel:dev .
```
