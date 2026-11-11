# anysda-vpn2 — панель

Nuxt 4 веб-панель управления VPN-стеком (WireGuard, OpenVPN, IKEv2). Часть
проекта `anysda-vpn2` — общий README и архитектура в корне репозитория.

## Разработка

```bash
pnpm install        # зависимости
pnpm dev            # dev-сервер на http://localhost:3000
pnpm build          # прод-сборка (.output/)
pnpm typecheck      # проверка типов
pnpm lint           # eslint
pnpm test:unit      # unit-тесты (vitest)
pnpm e2e:local      # сквозные тесты (Playwright), нужен Docker
pnpm db:generate    # сгенерировать drizzle-миграцию из schema.ts
```

`pnpm e2e:local` сам собирает образ панели и поднимает её с подставными
VPN-службами; один файл — `bash e2e/run-local.sh e2e/routes.spec.ts`.
Данные, общие с ботом и деплоем (тексты ошибок, пулы адресов), лежат в
`shared/`.

## Стек

- Nuxt 4 + Vue 3 (TS strict), Nuxt UI 4
- Nitro server routes, better-auth (сессии, TOTP, OIDC), свой лимит неудачных входов, пароли Argon2id
- Drizzle ORM + libSQL (SQLite), миграции в `server/database/migrations/`

## Деплой

Образ собирает GitHub Actions (`.github/workflows/images.yml`) на каждый пуш в `dev`/`main`
и кладёт в `ghcr.io/anysda/vpn2/panel` с тегом ветки; нода тянет его (стадия `30-frontend`).
Собрать руками (без пуша: в GHCR пушит только Actions):

```bash
docker buildx build --platform linux/amd64 --load \
  -t ghcr.io/anysda/vpn2/panel:dev .
```
