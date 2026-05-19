# anysda-vpn2

Веб-панель для управления многонодным Shadowsocks-стеком с автоматическим
geoip-роутингом, drag-n-drop ручными правилами и реал-тайм мониторингом.

> **Статус:** в разработке (Phase D ops-features завершены).
> **Лицензия:** MIT.
> **Clean-room:** проект написан с нуля, не использует AGPL-код wg-easy.

## Стек

- **Web:** Nuxt 4 + Vue 3 + TS strict, Nuxt UI 3 (Tailwind v4)
- **API:** Nitro server routes, nuxt-auth-utils (session cookies + Argon2id)
- **DB:** Drizzle ORM + libSQL (SQLite). Single-row admin user table.
- **TOTP:** свой RFC 6238 в `server/utils/totp.ts` (40 строк, без внешних зависимостей).
- **SS-сервер:** outline-ss-server v1.7 (отдельный systemd unit на entry-ноде).
- **Транспорт между нодами:** sing-box + Hysteria2.
- **Логи:** pino → JSON в проде, pino-pretty в dev.
- **Контейнер:** `node:22-slim` (нужен glibc для @node-rs/argon2 prebuilts).

## Архитектура

```
SS client (Outline / shadowsocks-libev / sing-box-client)
   │ ss://chacha20-ietf-poly1305:<secret>@entry-host:443
   ▼
[entry-нода: vpn-stand-ru (или прод)]
 ├─ outline-ss-server :443 (user `outline`)
 │    iptables --uid-owner outline → REDIRECT(tcp→7895) / TPROXY(udp→7896)
 │    ▼
 ├─ sing-box роутер
 │    geoip:ru/.ru/.рф → direct-ru (WAN entry)
 │    остальное → foreign-best (urltest hy2-*-direct)
 │    ручные правила → /etc/anysda/manual-routes.json (watched by systemd.path)
 │    ▼
 ├─ hy2-{tag}-direct → exit-нода (US/GB/NL/DE/...) → internet
 │
 ├─ anysda-vpn2 panel container :51821 (Caddy на :80 / :443)
 │    пишет /etc/outline-ss-server/config.yml + SIGHUP
 │    пишет /etc/anysda/manual-routes.json
 │    читает VictoriaMetrics + sing-box Clash API + AdGuard /control/stats
 │
 ├─ VictoriaMetrics (node_exporter scraping по mgmt-mesh 10.99.0.0/24)
 ├─ AdGuard Home (DNS)
 ├─ Caddy (reverse-proxy + опционально HTTPS через LE)
 └─ Telegram bot (отдельный python-контейнер)
```

## Структура

```
vpn2/
├── app/                      # Nuxt 4 панель
│   ├── Dockerfile            # multi-stage build + sed-патч h3 cookie.secure
│   ├── app/                  # srcDir: pages/components/layouts/composables
│   ├── server/               # Nitro: api/* routes/* utils/* plugins/* database/*
│   └── nuxt.config.ts
├── infra/                    # bash + python оркестратор деплоя
│   ├── deploy.sh             # главный pipeline (SKIP_IMAGE_BUILD=1 для предсобранных tar.gz)
│   ├── lib/                  # config2env.py, gen-router-config.py, ssh.sh
│   ├── configs/              # шаблоны для envsubst
│   └── scripts/              # 9 стадий: 00→05→10→20→22→25→27→30→35→99
├── telegram/                 # Python бот (опциональный)
├── deploy.sh                 # entry-point, делегирует infra/deploy.sh
├── setup.sh                  # интерактивный мастер config.yaml
└── config.example.yaml       # шаблон конфигурации
```

## Quickstart (с нуля на свежих Ubuntu 24.04 нодах)

1. **Подготовь ноды.** 1 entry (4 ГБ RAM, 20 ГБ диск) + N exit (1 ГБ, 5 ГБ).
   Root SSH с парольной авторизацией.

2. **Клонируй репу** на свою dev-машину (с Docker Desktop и rsync):
   ```bash
   git clone https://gitlab.anysda.space/anysda/vpn2 && cd vpn2
   ```

3. **Создай `config.yaml`** через мастер:
   ```bash
   ./setup.sh
   ```

4. **Собери Docker-образ панели локально** (apparmor в LXC не пускает билды):
   ```bash
   docker build -t anysda-vpn2:local app/
   docker save anysda-vpn2:local | gzip > infra/secrets/rendered/anysda-vpn2.tar.gz
   ```

5. **Разверни:**
   ```bash
   SKIP_IMAGE_BUILD=1 ./deploy.sh
   ```

6. **Открой панель** на `http://<entry-ip>/` — логин `admin` с паролем из `config.yaml`
   (или `/etc/anysda/admin-password.txt` если был сгенерирован).

7. **Создай клиента** → получи `ss://` URL или QR → импортируй в SS-клиент.

## Перезапуск отдельных стадий

```bash
./deploy.sh 30-frontend ru       # пересобрать панель
./deploy.sh 10-foreign foreign   # exits
./deploy.sh 99-verify all        # smoke-тест всех нод
```

## Что есть в API

```
POST   /api/auth/{login,logout}             session cookies
POST   /api/auth/password                   change password
POST   /api/auth/totp/{setup,confirm,disable}
GET    /api/auth/me

GET    /api/clients                         list
POST   /api/clients                         create
PATCH  /api/clients/:id                     enable/expiry/rename
DELETE /api/clients/:id
GET    /api/clients/:id/ss-url              text/plain ss://...
GET    /api/clients/:id/qrcode.svg
POST   /api/clients/:id/one-time-link       5min TTL OTL

GET    /api/routes                          manual rules
POST/PATCH/DELETE /api/routes               + outbounds from clash
GET    /api/routes/outbounds

GET    /api/ops/nodes                       cpu/ram/net/uptime via VM
GET    /api/ops/outbounds                   rtt + kbps via clash API
GET    /api/ops/adguard                     queries/blocked stats

GET    /api/admin/telegram                  bot config + status
PUT    /api/admin/telegram
GET    /api/ops/bot-snapshot                Bearer auth, for bot

GET    /ott/:token                          public OTL retrieve
GET    /metrics                             Prometheus exposition
GET    /api/version
```

## Лицензия

MIT — см. [LICENSE](LICENSE). Этот проект НЕ является форком wg-easy и не содержит AGPL-кода.
Инфраструктурные скрипты (`infra/`, `telegram/`, `setup.sh`, `deploy.sh`) перенесены из
предыдущей версии (anysda-vpn v1), где они написаны с нуля автором проекта.
