# Security Audit — anysda-vpn2

- **Дата:** 2026-06-01
- **Scope:** весь репозиторий `vpn2` на `dev`-ветке (не только pending changes — это первое security-ревью)
- **Методология:** 6 параллельных read-only агентов по направлениям:
  1. Backend API (`app/server/`) — auth, IDOR, injection, SSRF, secrets exposure
  2. Frontend Nuxt (`app/app/`) — XSS, CSRF, secrets-in-bundle
  3. DB schema + migrations (`app/server/database/`) — at-rest secrets, constraints
  4. Infra deploy/scripts (`deploy.sh`, `setup.sh`, `infra/`) — RCE, MITM, supply chain
  5. Telegram bot (`telegram/bot.py`)
  6. Secrets handling end-to-end + trust model

Все агенты работали read-only, без правок кода. Сводный документ дедуплицирован — одна и та же корневая проблема не дублируется между секциями.

## Severity overview

| Severity | Count |
|---|---|
| **Critical** | 2 |
| **High** | 11 |
| **Medium** | 16 |
| **Low** | 13 |
| Info | ~14 |

## TL;DR

Проект **аккуратен в локальной гигиене** (chmod 600 повсюду, age-шифр бэкапов, sshpass через env, `.gitignore` чистый, истории секретов в git нет, frontend не пускает секреты в bundle), но **архитектурно концентрирует риск**:

1. **`config.yaml` — single point of compromise.** В нём root-пароли всех нод, orchestrator-ed25519-private-key (base64), admin-пароль, telegram-токен, S3 access+secret, age-passphrase бэкапов. Чтение этого файла = полный контроль над флотом + возможность расшифровать любой бэкап. Ротации orchestrator-ключа нет.
2. **SSH с `StrictHostKeyChecking=no` + `/dev/null`** на каждый deploy/redeploy → MITM на любом первом подключении сразу даёт root-пароль и orchestrator-pubkey.
3. **Все приватные ключи клиентов (WG, OpenVPN, IKEv2-пароли) хранятся в SQLite в plaintext** — компрометация бэкапа = identity-takeover каждого клиента.
4. **`tgbotSecret` — единственный гейт для `/api/bot/admin/**`** с правами полного админа (create/delete/dump клиентов). Если Caddy не ограничивает эти роуты на loopback (агент не смог проверить), любой с секретом = full admin без 2FA и без rate-limit.
5. **Сессионная кука `Secure=false` зашита в Docker-образе** через `sed`-патч — кука уезжает по HTTP даже когда Caddy фронтит HTTPS.

Прежде чем ставить это в широкий прод — закрыть **2 critical + 5 high "quick wins"** (отдельная секция в конце).

## Trust model (из агента «secrets»)

```
                          [ orchestrator (dev laptop / LXC 120) ]
                          owns:  config.yaml + ~/.ssh/id_ed25519 + secrets/rendered/*
                          blast: ТОТАЛЬНО — root на RU + каждом exit;
                                 расшифровывает все S3-бэкапы (passphrase в том же config.yaml);
                                 SSH-имперсонация навечно (нет ротации).
                                /                            \
                               v                              v
                  [ entry / RU ]                       [ exit (us / gb / se / de) ]
                  /etc/anysda/*, db.sqlite,           /etc/anysda/hy2-*.pwd
                  /etc/wireguard/*, IKEv2 PKI,        wgmgmt.privkey
                  backup.env (passphrase + S3)
                  blast: каждый клиентский WG-privkey,  blast: hy2 этого exit + mgmt-mesh peer impersonation;
                         каждый IKEv2 password,                НЕ имеет orchestrator-key,
                         MITM всех клиентов через DNS,         НЕ имеет RU-секретов.
                         все S3-бэкапы расшифровываются.
```

Транзитивность: компромет dev-ноутбука → orchestrator → entry → все exit'ы. Компромет одной exit → только этот exit, перейти выше нельзя (good).

---

# Critical

## C1. SSH `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` на каждом подключении

[infra/lib/ssh.sh:66-67](../../infra/lib/ssh.sh#L66-L67)

`_ssh_opts` хардкодит `StrictHostKeyChecking=no` и `UserKnownHostsFile=/dev/null` для всех `ssh_exec`/`scp`. Каждый `./deploy.sh` принимает любой host-key. Сразу после подключения по паролю `00-bootstrap` льёт orchestrator-pubkey в `/root/.ssh/authorized_keys` на ещё MITM-нутом хосте.

**Attacker:** ISP / hijacked BGP / hostile transit / гипервизор хостера. Подменяет host-key → ловит root-password (через `sshpass`) → получает orchestrator-pubkey → root forever.

**Fix:** `StrictHostKeyChecking=accept-new` + `UserKnownHostsFile=$DEPLOY_ROOT/.known_hosts`, коммитить known_hosts в репу (или хранить SHA host-key в config.yaml). При несоответствии — отказ деплоя.

## C2. `ExecStart=/bin/sh -c` в systemd unit с интерполяцией путей из config.yaml

[infra/scripts/27-ikev2.sh:95-103](../../infra/scripts/27-ikev2.sh#L95-L103)

Heredoc раскрывает `$LE_CERT`/`$LE_KEY` в момент записи unit-файла. Эти пути собираются из `PANEL_DOMAIN`. `setup.sh` валидирует домен regex'ом, но **`config.yaml` пользователь правит руками** — комментарий в скрипте это подтверждает.

**Attacker:** оператор / компромет config.yaml. Кладёт `PANEL_DOMAIN` с `'`/`;` → ломает single-quoted `sh -c '...'` → RCE как root в systemd-unit на каждом deploy.

**Fix:** передавать пути через `Environment=LE_CERT=...` в unit-файле или вынести в отдельный `/usr/local/sbin/anysda-ikev2-cert-sync.sh`, читающий 600-env-файл. Не интерполировать config-данные в `sh -c`-строки.

---

# High

## H1. `config.yaml` = «skeleton key» всей инфры + orchestrator-private-key персистится туда же

[infra/deploy.sh:282-293](../../infra/deploy.sh#L282-L293), [infra/lib/config2env.py:98-119](../../infra/lib/config2env.py#L98-L119)

`prep_orchestrator_key()` base64-кодирует `~/.ssh/id_ed25519` и аппендит в `config.yaml`. В этом же файле — root-пароли всех нод, admin-password, telegram-token, S3-creds, age-passphrase бэкапов. Ротации ключа нет.

**Fix:** отдельный `secrets/orchestrator.key` (600), `./deploy.sh rotate-orchestrator-key` (генерит новый ключ → пушит pubkey по старому → удаляет старый из authorized_keys на всех нодах), `keyId`-маркер в authorized_keys для адресной деинсталляции старых ключей. Опционально — age-шифровать `config.yaml` целиком, расшифровывать на лету.

## H2. WireGuard/OpenVPN/IKEv2 client private keys + клиентские пароли — plaintext в SQLite

[app/server/database/schema.ts:58-68](../../app/server/database/schema.ts#L58-L68), миграции `0001`, `0002`, `0008`

Колонки `devices.wg_private_key`, `wg_preshared_key`, `ovpn_key`, `ikev2_password`, `clients.password` — все TEXT plaintext. SQLite файл попадает в бэкапы.

**Attacker:** утечка бэкапа (S3-creds в том же config.yaml — см. H8), bug-уровневое DB-чтение, кража диска — даёт identity всех клиентов VPN: можно подменять, дешифровать прошлый трафик при отсутствии PFS.

**Fix:**
- WG: генерировать keypair на устройстве клиента (paste pubkey в panel или сгенерить в браузере через WebCrypto), убрать колонку `wg_private_key`.
- OpenVPN: то же или envelope-шифрование с KEK в `/etc/anysda/kek` (600).
- IKEv2 EAP-MSCHAPv2 требует cleartext server-side — минимум зашифровать колонку KEK'ом, расшифровывать только при материализации `ipsec.secrets`.
- `clients.password` — хранить hash (argon2id), а сам deeplink `?start=<token>` — отдельный one-shot signed token (HMAC + nonce + TTL) в новой таблице.

## H3. `tgbotSecret` — единственный gate для `/api/bot/admin/**` с full-admin правами

[app/server/utils/bot-api.ts:8-13](../../app/server/utils/bot-api.ts#L8-L13), [app/server/api/ops/bot-snapshot.get.ts:10-19](../../app/server/api/ops/bot-snapshot.get.ts#L10-L19)

`requireBotAuth()` делает `provided !== expected` (non-constant-time). Тот же секрет открывает: `POST /api/bot/admin/clients` (создать), `PATCH/DELETE /api/bot/admin/clients/:id`, `GET /api/bot/admin/clients/:id` (отдаёт plaintext bot-link password), `POST /api/bot/devices`, `DELETE /api/bot/devices/:deviceId` + snapshot всех клиентов.

**Не покрыто аудитом:** ограничивает ли Caddy эти роуты на loopback. Если нет — любой со знанием секрета = full admin без 2FA и rate-limit.

**Fix:** (a) Биндить `/api/bot/**` и `/api/ops/bot-snapshot` на 127.0.0.1 в Caddy / H3-middleware по `event.node.req.socket.remoteAddress`. (b) `crypto.timingSafeEqual` на equal-length буферах. (c) Разделить секреты: один для client-facing bot-ops, другой для admin-bot-ops.

## H4. `client_notify` event позволяет вызывающему задавать `chatId` → arbitrary message от лица бота

[telegram/bot.py:1198,1250-1257](../../telegram/bot.py#L1250-L1257)

`/event` слушает на 127.0.0.1, auth-чек: `if SECRET and request.headers.get(...) != SECRET`. Если `TGBOT_SECRET == ''` (свежая установка, до настройки) — auth отключён. Внутри `client_notify` `chat_id = data.get('chatId')` — берётся прямо из тела, без проверки принадлежности клиенту.

**Attacker:** любой соседний процесс на хосте (контейнер, локальный пользователь) → шлёт произвольный текст в **любой** Telegram-чат от имени бота → фишинг, импersonation.

**Fix:** hard-fail при `SECRET == ''` на старте бота. Для `client_notify` — либо доставать chatId из таблицы `clients.tgChatId` (требует DB-доступа боту), либо роутить через `POST /api/bot/notify/<clientId>` где Nuxt сам выбирает chatId.

## H5. `tls.insecure: true` для Hysteria2 outbound RU→exit (sing-box)

[infra/lib/gen-router-config.py:66-67](../../infra/lib/gen-router-config.py#L66-L67)

RU-роутер принимает любой cert от exit'а. Комментарий объясняет «self-signed на exits, наша инфра». **Pinning отсутствует** (Hysteria2 поддерживает `pinSHA256`, sing-box — `certificate` / SPKI-pinning).

**Attacker:** RU-side state-level adversary или hostile transit MITM-ит RU→exit hop → дешифрует и инъектит трафик. Вся «зарубежная» гарантия VPN рассыпается.

**Fix:** в `10-foreign` собирать SHA-256 fingerprint self-signed cert exit-ноды, протаскивать в `prep_ru_router`, класть в `tls.certificate` или SPKI-pin. Альтернатива — LE-cert на exits (как для panel), `insecure: false`.

## H6. `set -a; source <env>; set +a` экспортирует SSH-пароли в env всех subprocess'ов

[infra/lib/ssh.sh:21-33](../../infra/lib/ssh.sh#L21-L33)

`load_env()` `source`-ит `infra/envs/$host.env` с `set -a` — `SSH_PASS='...'` экспортируется, и **не unset-ится** после `set +a`. Любой дочерний процесс (curl, python-хелперы) видит SSH_PASS всех нод. На LXC 120 с samba-export `/` это особенно чувствительно.

**Fix:** парсить env в локальный assoc-array, не `source`. Или `unset SSH_PASS` после каждого `ssh_exec`. Или вообще убрать пароли из env-файлов после bootstrap (после H10).

## H7. Шелл-инъекция в `ssh_exec` через `$exit_host` / `$base` из user-input

[infra/deploy.sh:655-695](../../infra/deploy.sh#L655-L695) (probe), [infra/deploy.sh:786-797](../../infra/deploy.sh#L786-L797) (restore)

`_probe_udp_one_port` подставляет `$exit_host` (из `config.yaml`) текстом в python-heredoc внутри `ssh_exec`. `do_restore` — `'$base'` (basename из CLI-аргумента) тоже текстом. setup.sh валидирует, но **config.yaml редактируется руками**.

**Attacker:** правит `host: 1.1.1.1"); subprocess.call([...]); #` в config.yaml → RCE root на entry на каждом deploy.

**Fix:** `ssh "$host" -- bash -s <<<"$body"` через stdin, либо `printf %q` для каждого интерполируемого значения. Никогда не клеить config-строки в текст ssh-команды.

## H8. `curl ... | sh` / `curl | tar -xz` без checksum на 7+ зависимостях

[infra/deploy.sh:143](../../infra/deploy.sh#L143) (docker), [infra/scripts/00-bootstrap.sh:183-187](../../infra/scripts/00-bootstrap.sh#L183-L187) (node_exporter), [infra/scripts/10-foreign.sh:30-32](../../infra/scripts/10-foreign.sh#L30-L32) (wgcf), [infra/scripts/10-foreign.sh:87-92](../../infra/scripts/10-foreign.sh#L87-L92) / [20-ru-router.sh:25-30](../../infra/scripts/20-ru-router.sh#L25-L30) (sing-box), [22-adguard.sh:38-43](../../infra/scripts/22-adguard.sh#L38-L43) (AdGuardHome), [26-backup.sh:54-56](../../infra/scripts/26-backup.sh#L54-L56) (aws-cli)

Ни одна загрузка не проверяет SHA-256. `get.docker.com | sh` исполняет код Docker'а от рута. Geoip/geosite — `releases/latest/download/`, без pin'а версии.

**Attacker:** компромет GitHub release CDN / awscli S3 → бэкдор в каждом deploy. RKN-MITM прецеденты на CDN были.

**Fix:** для каждой загрузки — переменная `*_SHA256` рядом с версией, `sha256sum -c` после `curl`. Для Docker — официальное apt-repo (как уже сделано в 25-monitoring).

## H9. Admin-пароль echo-ится в stdout deploy → попадает в `logs/` репозитория

[infra/scripts/30-frontend.sh:57,187](../../infra/scripts/30-frontend.sh#L57), [infra/scripts/22-adguard.sh:65](../../infra/scripts/22-adguard.sh#L65), наблюдается в `logs/deploy-005-rest.log`, `logs/deploy-007-panel-retest.log` («admin pass: …»)

`echo "admin pass: $ADMIN_PASS"` — попадает в stdout стейджа, в `journalctl -u ssh` на entry (т.к. идёт через ssh), в orchestrator tty, в `logs/` если `tee`.

**Attacker:** screen-shot, log-shipper, скопированный в чат для «помоги».

**Fix:** только в финальном summary, только в stderr orchestrator'а, печатать «(сохранено в /etc/anysda/admin-password.txt)» вместо самого пароля. Скрабнуть существующие `logs/*.log` (или удалить — они уже в git'е?).

## H10. Panel-контейнер = `root` + bind-mount `/etc/anysda` rw + `--security-opt apparmor=unconfined`

[infra/scripts/30-frontend.sh:122-132](../../infra/scripts/30-frontend.sh#L122-L132), [app/Dockerfile](../../app/Dockerfile) (нет `USER`)

Контейнер монтирует `/etc/anysda` (rw), `/etc/wireguard` (rw), `/etc/openvpn`, `/etc/swanctl`, бежит как root, host network, `CAP_NET_ADMIN`, apparmor unconfined. Любая RCE в Nuxt (dep-CVE, deserialize в `nuxt-auth-utils`, ssrf-режим в YAML/JSON-парсере) = чтение `admin-password.txt`, `tgbot-secret.txt`, `clash-secret.txt`, всех WG/IKEv2 ключей + `backup.env` (S3 + age-passphrase).

**Fix:** монтировать конкретные файлы как `:ro`. `USER node` в Dockerfile, `--user 1000:1000`. `backup.env` вынести из `/etc/anysda`. Капабилити выдавать точечно.

## H11. Session cookie `secure: false` зашит в Docker-образ через `sed`-патч

[app/Dockerfile:34-36](../../app/Dockerfile#L34-L36), [app/nuxt.config.ts:25](../../app/nuxt.config.ts#L25)

Dockerfile `sed`-патчит сборку nitro, чтобы `secure: false`. Stage `30-frontend` пробует выставить `NUXT_SESSION_COOKIE_SECURE=true` через env — но bake'д значение ниже уровнем (комментарий в Dockerfile это признаёт: «defu drops the extra keys at module init time»).

**Attacker:** HTTP-deployment / любой :80-redirect-fail / mixed-content → session cookie уходит cleartext.

**Fix:** правильно патчить h3 через nitro plugin или runtimeConfig в момент request, а не `sed`. Или две image-tag'а (http/https). На старте — `assert(cookie.secure || !PANEL_DOMAIN)`.

---

# Medium

## M-Auth / Session

| # | Файл | Что |
|---|---|---|
| M1 | [app/server/api/auth/login.post.ts:14-44](../../app/server/api/auth/login.post.ts#L14-L44) | Нет rate-limit/lockout на login. Argon2 — единственный гейт, ~200ms/попытка → тысячи попыток/час реально. |
| M2 | [app/server/api/auth/totp/disable.post.ts:7-29](../../app/server/api/auth/totp/disable.post.ts#L7-L29) | Disable TOTP требует только current password, **не** требует валидный TOTP-код. С украденной session + password = снести 2FA. |
| M3 | [app/server/api/auth/password.post.ts:12-27](../../app/server/api/auth/password.post.ts#L12-L27) | Password change **не** ротирует session/выкидывает другие сессии. |
| M4 | [app/server/api/auth/login.post.ts:23-33](../../app/server/api/auth/login.post.ts#L23-L33) | Разные statusMessage для `invalid_credentials` vs `invalid_totp` → password-oracle. |
| M5 | [app/server/api/bot/link.post.ts:14-31](../../app/server/api/bot/link.post.ts#L14-L31) | Нет rate-limit на `/bot/link` — bruteforce 20-char `clients.password` теоретически реален, особенно если password-gen имеет слабое распределение (нет UNIQUE). |
| M6 | [app/nuxt.config.ts:21-27](../../app/nuxt.config.ts#L21-L27) | Default `cookie.secure: false` (см. также H11). |
| M7 | [app/nuxt.config.ts](../../app/nuxt.config.ts) | Нет CSRF на state-changing endpoints. SameSite=Lax — не серебряная пуля; внутрипанельный XSS = полное использование сессии. |
| M8 | [app/nuxt.config.ts](../../app/nuxt.config.ts) | Нет `X-Frame-Options` / `frame-ancestors 'none'` — clickjacking на админа. |

## M-DB constraints

| # | Файл | Что |
|---|---|---|
| M9 | [app/server/database/schema.ts:13-19](../../app/server/database/schema.ts#L13-L19) | `users` нет `failed_attempts`, `locked_until`, `last_login_ip` → persistent brute-force lockout невозможен. |
| M10 | [schema.ts:17](../../app/server/database/schema.ts#L17) | `users.totp_secret` plaintext — лежит рядом с password_hash, defeating 2FA-in-depth. |
| M11 | [schema.ts:57-69](../../app/server/database/schema.ts#L57-L69) | Нет UNIQUE на `wg_public_key`, `wg_ip`, `ikev2_username`, `ikev2_ip`. Коллизия → silent identity collision у WG/IPsec. |
| M12 | [schema.ts:42](../../app/server/database/schema.ts#L42), [schema.ts:45](../../app/server/database/schema.ts#L45) | Нет UNIQUE на `clients.password` (deeplink lookup) и `clients.tg_chat_id`. Один tg-юзер может биндиться к разным клиентам. |

## M-Telegram

| # | Файл | Что |
|---|---|---|
| M13 | [telegram/bot.py:120](../../telegram/bot.py#L120) | Bot-token в `base_url=f'https://api.telegram.org/bot{TOKEN}'` — любой `httpx`-exception в `log.exception` напечатает URL целиком с токеном. |
| M14 | [telegram/bot.py:562-563](../../telegram/bot.py#L562-L563) | Admin-gate = `chat_id == CHAT_ID`. Нет startup-assert `CHAT_ID > 0`. Если runtime.json пустой → `CHAT_ID = 0`, любые admin-команды залочены, но при подмене CHAT_ID на групповой чат → все в группе админы. |
| M15 | [telegram/bot.py:410,752,1303-1308](../../telegram/bot.py#L1303-L1308) | Markdown-injection: client-controlled `device.name` интерполируется в `parse_mode='Markdown'` — `*`/`_`/`` ` `` ломают Telegram-парсинг, spoofing в admin-чат. |
| M16 | [telegram/bot.py:1198](../../telegram/bot.py#L1198) | Token-compare через `!=` (timing-leaky), и `if SECRET` — если SECRET=='' приём событий открыт (см. H4). |

## M-Infra

| # | Файл | Что |
|---|---|---|
| M17 | [infra/lib/ssh.sh:99-104](../../infra/lib/ssh.sh#L99-L104) | `/tmp/anysda` на remote — predictable shared path для secret-файлов. World-traversable dir, потенциальный symlink-race. |
| M18 | [infra/deploy.sh:319-327](../../infra/deploy.sh#L319-L327) | `secrets/rendered/<host>.env` остаются на orchestrator'е между деплоями. Нет cleanup-trap. На LXC 120 эта папка попадает в samba `\\.142\root`. |
| M19 | [infra/lib/config2env.py:230](../../infra/lib/config2env.py#L230) | `all.env` создаётся с 0664 (default umask) — содержит топологию мешa и endpoints. |
| M20 | [infra/scripts/05-mgmt-mesh.sh:18-19](../../infra/scripts/05-mgmt-mesh.sh#L18-L19), [20-ru-router.sh:61-63](../../infra/scripts/20-ru-router.sh#L61-L63) | `eval ": \"\${PUBKEY_${_T}:?}\""` — `_T` из config-tag. `${!var}` уже используется в том же файле — `eval` тут лишний и опаснее. |
| M21 | [infra/lib/anysda-restore.sh:130](../../infra/lib/anysda-restore.sh#L130) | `tar -xzf` без `--no-same-owner` / `--no-overwrite-dir`. Tar-archive может содержать symlink → `cp -a` после распаковки следует ему → перезапись `/root/.ssh/authorized_keys`. |
| M22 | [infra/scripts/30-frontend.sh:126-132](../../infra/scripts/30-frontend.sh#L126-L132) | Аналогично H10 (тот же mount-pattern в medium-разрезе). |

## M-API surface

| # | Файл | Что |
|---|---|---|
| M23 | [app/server/routes/metrics.get.ts](../../app/server/routes/metrics.get.ts) | `/metrics` без auth — раскрывает счётчики клиентов, версию (fingerprinting). Полагается на upstream Caddy. |
| M24 | [app/server/plugins/init.ts:53-72](../../app/server/plugins/init.ts#L53-L72) | Auto-генерированный admin-password пишется в `/etc/anysda/admin-password.txt` (600) **и** дополнительно `log.warn({ password })` при ошибке записи → cleartext в journald. |

---

# Low (одной строкой каждое)

- [app/app/pages/me.vue:210](../../app/app/pages/me.vue#L210) — `v-html="totpQrSvg"` ОК сегодня (qrcode-svg sanitizes), но footgun: добавить DOMPurify или canvas-рендер.
- [app/app/components/Monitoring/Dns.vue:81](../../app/app/components/Monitoring/Dns.vue#L81) — `<a>` к AdGuard без `rel=noreferrer` (Referer leak).
- [app/server/api/ops/bot-snapshot.get.ts:10-19](../../app/server/api/ops/bot-snapshot.get.ts#L10-L19) — inline-копия bot-secret-check вместо `requireBotAuth()`, drift-риск.
- [app/server/api/clients/[id]/send-*-to-tg.post.ts](../../app/server/api/clients/) — `cause: e` в `createError` может утечь stack в dev.
- [app/server/api/auth/password.post.ts:8-10](../../app/server/api/auth/password.post.ts#L8-L10) — min 8 без complexity. С отключённым TOTP — слабый пароль реально гадается.
- [telegram/bot.py:754,769,1222](../../telegram/bot.py#L1312) — путь-traversal в filename attached document (Telegram сами sanitize, но pattern footgun).
- [telegram/bot.py:660,1050](../../telegram/bot.py#L1050) — нет ограничения длины inputs из TG → 4KB device.name'ы.
- [infra/lib/anysda-backup.sh:188-204](../../infra/lib/anysda-backup.sh#L188-L204) — `expect` + env var passphrase. Лучше через stdin pipe.
- [infra/deploy.sh:278-281](../../infra/deploy.sh#L278-L281) — merge orchestrator authorized_keys в push'имый блоб → не-orchestrator-keys уезжают на все ноды.
- [infra/scripts/35-telegram.sh:87](../../infra/scripts/35-telegram.sh#L87) — `-e TELEGRAM_BOT_TOKEN=$T` в `docker run` → `docker inspect` показывает токен. Использовать `--env-file`.
- [infra/scripts/00-bootstrap.sh:115-120](../../infra/scripts/00-bootstrap.sh#L115-L120) — `PerSourcePenalties` выключен. Trade-off ради retry'ев, осталось fail2ban.
- [infra/lib/ssh.sh:85,111](../../infra/lib/ssh.sh#L85) — `SSHPASS=$X sshpass -e` — пароль виден через `/proc/N/environ` root'у. Перейти на process substitution / fd.
- [infra/scripts/22-adguard.sh:60](../../infra/scripts/22-adguard.sh#L60) — 18-char password из `openssl rand -base64 24 | tr -d ...` ≈ 107 бит, ОК, но pattern неровный (в разных скриптах разные).

---

# Quick wins (что починить в первый присест)

В порядке убывания соотношения «снижение риска / усилие»:

1. **Включить `StrictHostKeyChecking=accept-new`** в `infra/lib/ssh.sh` + добавить `.known_hosts` в репу. Закрывает C1. Часы работы.
2. **Hard-fail бота при `TGBOT_SECRET == ''`** + перевести `/api/bot/admin/**` на bind 127.0.0.1 в Caddy. Закрывает H3 + H4 разом.
3. **Перестать echo-ить admin-password** в `30-frontend.sh:57,187` и `22-adguard.sh:65`. Скрабнуть `logs/*.log`. Закрывает H9.
4. **`USER node` в `app/Dockerfile`** + точечный read-only mount файлов из `/etc/anysda` вместо всей папки. Сильно сокращает H10.
5. **Pin SHA-256 для `curl | tar -xz`** загрузок (хотя бы для Docker и AdGuard) + переход на apt-repo Docker'а как в `25-monitoring`. Закрывает большую часть H8.
6. **Rate-limit на `/api/auth/login`** (in-memory counter по IP) + lockout на TOTP-disable без TOTP-кода. Закрывает M1+M2.
7. **TLS-pinning Hysteria2 outbound** (SHA-256 cert exit-ноды → в sing-box config через `tls.certificate`). Закрывает H5.

---

# Что НЕ покрыто этим аудитом

1. **Caddy / firewall config** — bind ли `/api/bot/**` и `/metrics` на loopback. От этого зависит весь severity H3.
2. **nuxt-auth-utils session rotation semantics** — sealed cookie, нет server-side store, «инвалидация» полагается на token-freshness внутри seal'а.
3. **Дerзависимости** (`package.json`, `requirements.txt`) — не аудированы на CVE.
4. **WG-mesh / Hysteria2 wire-level** — не проверялись на отдельные CVE sing-box / wg-go.
5. **Behaviour `nuxt-auth-utils` под нагрузкой / race** — не fuzz-тестировались.

---

# Файлы, реально прочитанные агентами

- `app/server/api/**/*.ts` (~25 endpoint'ов), `app/server/utils/**/*.ts`, `app/server/plugins/**`, `app/server/database/schema.ts`, `app/server/database/migrations/0000…0008*.sql`
- `app/app/**/*.vue` (~30 файлов), `app/app/composables/**`, `app/app/middleware/**`, `app/nuxt.config.ts`, `app/Dockerfile`
- `deploy.sh`, `setup.sh`, `infra/deploy.sh`, `infra/lib/{ssh.sh,config2env.py,anysda-backup.sh,anysda-restore.sh,gen-router-config.py}`, `infra/scripts/{00-bootstrap,05-mgmt-mesh,10-foreign,20-ru-router,22-adguard,25-monitoring,26-backup,27-ikev2,29-openvpn,30-frontend,35-telegram,99-verify}.sh`
- `telegram/bot.py`, `telegram/Dockerfile`, `telegram/requirements.txt`
- `config.example.yaml`, `.gitignore`
- `git log --all --full-history -p` (выборочно — секретов в истории не найдено)
