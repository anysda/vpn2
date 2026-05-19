# anysda-vpn2

Веб-панель для управления многонодным Shadowsocks-стеком: один entry-узел + N exit-узлов, sing-box роутер с geoip/geosite разделением трафика, Hysteria2 туннели между нодами.

> **Status:** в разработке. Спецификация и план развития — внутренний документ.
> **License:** MIT
> **Clean-room rewrite:** проект написан с нуля, без переиспользования AGPL-кода wg-easy/anysda-vpn v1.

## Стек

- Nuxt 4 + Nuxt UI 3 (Vue 3, TS strict)
- Drizzle ORM + libSQL (SQLite)
- nuxt-auth-utils (Argon2id, TOTP)
- pino logging
- Docker (alpine, multi-stage)

## Структура (планируется)

```
anysda-vpn2/
├── app/             # Nuxt 4 приложение (панель)
├── infra/           # bash-скрипты деплоя на entry+exits (планируется вытащить из ../vpn/infra/)
├── telegram/        # Python-бот (планируется вытащить из ../vpn/telegram/)
├── docs/            # пользовательская документация
└── docker-compose.yml
```

## Quickstart

_Скоро. Сначала допишем MVP, потом инструкция._

## Связанные репозитории

- [vpn](https://gitlab.anysda.space/anysda/vpn) — предыдущая версия (AGPL, форк wg-easy). Заморожена.
