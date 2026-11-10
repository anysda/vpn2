"""
anysda-vpn Telegram bot.

Мультипользовательский: обслуживает админа (чат TELEGRAM_CHAT_ID) и
конечных клиентов VPN (любой другой чат — самообслуживание).

Админ-команды (только из админского чата):
  /status   — system overview (nodes CPU/RAM + outbounds RTT)
  /nodes    — per-exit-node RTT
  /clients  — client list

Клиент (любой другой чат):
  /start <пароль>  — привязать чат к клиенту по диплинку-приглашению
  /start           — открыть меню (если чат уже привязан)
  инлайн-меню — устройства, выдача WG/OVPN конфигов, перевыпуск, удаление

Background monitor (every ALERT_INTERVAL_SEC, default 30 s) — только админу:
  - alerts when a node's metrics go stale for > ALERT_NODE_DOWN_SEC
    (default 5 min) — i.e. the node/VM is down;
  - alerts on high CPU / RAM.
  Each alert is cleared when the node recovers.

Listens on TGBOT_EVENT_PORT for events from Nuxt:
  POST /event  {"type": "client_created", "name": "..."}

Telegram API traffic egresses through an exit node: the RU entry node
in Moscow can't reach api.telegram.org directly, so the bot's
Telegram-facing httpx clients route through the local HTTP proxy that
sing-box exposes (→ foreign-best → exit). Only calls to the Nuxt API on
127.0.0.1 stay direct.

Env vars:
  TELEGRAM_BOT_TOKEN  — required
  TELEGRAM_CHAT_ID    — required (integer; у группы отрицательный, -100…)
  TGBOT_SECRET        — shared secret with Nuxt (Bearer token)
  TGBOT_EVENT_PORT    — HTTP port for Nuxt→bot events (default 8877)
  TGBOT_OUTBOX_PATH   — durable notification DB (default /var/lib/anysda-tgbot/outbox.sqlite)
  ANYSDA_URL          — Nuxt API base (default http://127.0.0.1:51821)
  TG_PROXY            — HTTP proxy for Telegram API (default http://127.0.0.1:7897)
  TGBOT_POLL_STALL_SEC — poller watchdog: exit if no successful getUpdates for
                         this long (default 300, 0 = off)
"""
