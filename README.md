# anysda-vpn2

Веб-панель для управления многонодным VPN-стеком (WireGuard + OpenVPN +
IKEv2/IPsec) с авто-geoip-роутингом, ручными правилами (drag-and-drop),
мониторингом и Telegram-ботом для админа и для конечных клиентов.

> 🚀 **Сразу деплоить →** [Quickstart](#-quickstart--развернуть-с-нуля)

![Главный экран панели](docs/screenshots/panel-overview.png)

Слева — список клиентов с трафиком и статусами; центр — Маршрутизация
(drag-and-drop правил по колонкам outbound'ов RU/DE/SE/US, авто-роутинг
+ ручные правила); справа в столбце «US» — AI-сервисы, в «DE» — стриминг.
Снизу — Мониторинг нод (CPU/RAM/Mbps) и AdGuard DNS со статистикой
запросов и блокировок. Слева снизу — настройки Telegram-бота.

![Модалка клиента](docs/screenshots/client-modal.png)

Карточка клиента: имя, срок, суммарный трафик, привязка к Telegram-боту,
лимит устройств, заморозка/перевыпуск/удаление. Снизу — список устройств
с per-device трафиком и кнопками: WG (`.conf`/QR), OVPN (`.ovpn`), IKEv2
(server/login/password + `.mobileconfig` для Apple), перевыпустить ключи,
удалить устройство.

## Что внутри

- **Веб-панель (Nuxt 4):** клиенты, устройства, маршрутизация, графики,
  AdGuard, Telegram-настройки. Один admin, опционально TOTP.
- **Двухуровневая модель:** клиент (человек) → его устройства. У каждого
  устройства свои WG-ключи, OpenVPN-сертификат и IKEv2-логин/пароль.
- **Авто-роутинг (sing-box):** RU-трафик через WAN entry, заграница
  через Hysteria2 на лучший exit; быстрый failover (~5–7с).
- **YouTube мимо экзитов (опционально):** выходит с РФ-адреса entry —
  YouTube не крутит рекламу на российских IP, — а DPI обходится десинком
  TLS (nfqws2/zapret2), отбор трафика по SO_MARK от sing-box.
- **Ручные правила:** домен/IP → конкретный outbound, drag-and-drop в
  UI; sing-box подхватывает без рестарта клиентов.
- **Сплит-туннель по локалкам (по умолчанию вкл.):** RFC1918, CGNAT
  (`100.64/10`) и multicast не уезжают в VPN — роутер, NAS, принтер,
  Chromecast и docker-сети остаются доступны при поднятом туннеле.
  В WG это дополнение `0.0.0.0/0` в `AllowedIPs` (с дыркой под endpoint,
  иначе wg-quick ловит петлю), в OpenVPN — `route … net_gateway`.
  Выключается `WG_SPLIT_LOCAL=false`, см. `server/utils/allowed-ips.ts`.
- **ULA fast-fail для IPv6 (WireGuard):** `AllowedIPs` пропускает клиента
  в `2000::/3` (реальный интернет, чтобы v6 не утёк мимо VPN к оператору),
  но v6-адреса на туннельных интерфейсах нет, поэтому раньше пакет уходил
  в туннель и там молча пропадал (клиент ждёт таймаут, у Telegram на
  IPv6-only сотовых сетях так падало каждое третье соединение). Теперь у
  клиента и у хаба есть парный ULA-адрес `fd66:66::N/64` (N тот же, что в
  `10.66.66.N`), а хаб отвечает на любой v6 с `wg0` мгновенным ICMPv6
  `destination unreachable`: приложение получает отказ за один RTT и тут
  же берёт v4. **Старым клиентам нужно перекачать `.conf`/QR**, новый
  ULA-адрес прописывается только в свежем конфиге.
- **AdGuard Home:** DNS + блок-лист для VPN-клиентов с включённой
  фильтрацией.
- **Telegram-бот:** админ управляет клиентами командами `/clients`,
  получает алерты; конечные клиенты сами получают конфиги, добавляют
  устройства и видят уведомления об изменениях.
- **Мониторинг:** карточки нод (CPU/RAM/RX/TX), RTT по выходам, графики;
  `node_exporter` → VictoriaMetrics + clash-api sing-box.
- **Деплой одной командой:** `./setup.sh` → `./deploy.sh`. 13 стадий,
  идемпотентно, без ручных шагов на нодах.

## Стек

Nuxt 4 (Vue 3, TS strict) · Nuxt UI 3 (Tailwind v4) · Nitro · Drizzle
ORM + libSQL (SQLite) · nuxt-auth-utils (cookie-сессии + Argon2id) ·
свой TOTP (RFC 6238) · sing-box (Hysteria2) · WireGuard (kernel) +
OpenVPN + strongSwan (IKEv2/IPsec, EAP-MSCHAPv2) · AdGuard Home ·
VictoriaMetrics + node_exporter · Caddy
(reverse-proxy + Let's Encrypt) · Python (`python-telegram-bot`) для
бота. Образы — `node:22-slim` (панель), `python:3.13-slim` (бот).

## Архитектура

```
WireGuard client  ·  OpenVPN client  ·  IKEv2 client (iOS/macOS/Win/Android)
   │ wg0 udp/51820        tun0 udp/1194    udp/500 IKE + udp/4500 ESP/NAT-T
   ▼
[entry-нода]
 ├─ WireGuard (wg0) + OpenVPN (tun0) + strongSwan (xfrm0)
 │   iptables PREROUTING -i wg0/tun0/xfrm0 → TPROXY → sing-box :7898
 ├─ sing-box роутер
 │   YouTube → youtube-ru (WAN entry, РФ-IP) + метка → nfqws2-десинк
 │   geoip:ru / .ru/.рф → direct-ru (WAN entry)
 │   остальное → foreign-best (hy2-* exit'ы; выбирает failover-watchdog)
 │   ручные правила → /etc/anysda/manual-routes.json
 ├─ nfqws2 (zapret2) — обход DPI для YouTube, только помеченный трафик
 ├─ anysda-vpn2 panel  (Caddy → :51821)
 ├─ AdGuard Home + Caddy
 ├─ VictoriaMetrics (scraping mgmt-mesh 10.99.0.0/24)
 └─ Telegram-бот (контейнер) — егрессит в Telegram через exit
       hy2-{tag}-direct → exit-нода → internet
```

## Структура

```
vpn2/
├── app/                      # Nuxt 4 панель
├── infra/                    # bash + python оркестратор деплоя
│   ├── deploy.sh             # главный pipeline
│   ├── lib/                  # config2env, gen-router-config, failover-watchdog, ssh
│   ├── configs/              # шаблоны конфигов (envsubst)
│   └── scripts/              # стадии 00 / 05 / 10 / 19 / 20 / 21 / 22 / 25 / 28 / 29 / 30 / 35 / 99
├── telegram/                 # Python бот (опциональный)
├── deploy.sh                 # entry-point, делегирует infra/deploy.sh
├── setup.sh                  # интерактивный мастер config.yaml
└── config.example.yaml       # шаблон конфигурации
```

---

# 🚀 Quickstart — развернуть с нуля

Нужны: 1 entry-нода в России (4 ГБ RAM, 20 ГБ диск) и
N exit-нод (минимум 1, рекомендую 2–3 для failover; 1 ГБ RAM, 5 ГБ
диск). Все — **Ubuntu 24.04 LTS, чистая установка**, root по SSH-паролю.
Разворачивается с entry-ноды — она же оркестратор.

### 0. (если нужен HTTPS-домен) — A-запись за 4 часа до деплоя

`vpn.example.com → <entry-ip>`. DNS прогревается до 4 часов; ставь
заранее, иначе Caddy не получит сертификат при первом запуске.

### 1. Подключиться к entry-ноде

```bash
ssh root@<entry-ip>
```

> *(Опционально, до подключения)* `ssh-copy-id root@<entry-ip>` — кладёт
> твой публичный ключ на entry. Деплой подхватит его и при бутстрапе
> раскатает на все exit-ноды — после этого по всему стенду ходишь по
> ключу. Без этого шага деплой тоже работает: оркестратор сам сгенерит
> ключ на entry и распространит уже свой.

### 2. Склонировать репозиторий 

```bash
git clone https://github.com/anysda/vpn2 /opt/anysda-vpn2
cd /opt/anysda-vpn2
```

### 3. Заполнить конфиг мастером

```bash
./setup.sh
```

Записывает ответы в `config.yaml`

### 4. Запустить деплой

```bash
./deploy.sh
```

Сам поставит локальные зависимости, проверит SSH ко всем нодам,
развернёт mgmt-mesh, поднимет Hysteria2 на exit'ах, WG + OVPN +
sing-box-роутер + failover-watchdog + AdGuard + VictoriaMetrics +
панель + бота на entry. Идемпотентно — можно перезапускать.

### 5. Создать первого клиента

В панели: «➕ Новый клиент» → имя → «Создать» → откроется карточка
клиента → «➕ Добавить устройство» → имя → «Создать». Дальше — кнопки
«Скачать WG / .conf / QR» или «Скачать OVPN».

---

## Перезапуск отдельных стадий

С entry-ноды:

```bash
./deploy.sh 30-frontend ru       # пересобрать панель
./deploy.sh 35-telegram ru       # перезапустить бота
./deploy.sh 10-foreign foreign   # все exit-ноды
./deploy.sh 19-yt-zapret ru      # пересобрать/перенастроить обход DPI YouTube
./deploy.sh 99-verify all        # smoke-тест
```

---

## YouTube — почему он идёт мимо экзитов

Включается секцией `youtube:` в `config.yaml` (`route: zapret`), по умолчанию
выключено.

**Смысл.** YouTube не крутит рекламу на российских IP. Через зарубежный экзит
сайт видит иностранный адрес — и реклама включается. То есть VPN «чинит»
доступ и ломает ровно то, ради чего его ставили семье. Поэтому YouTube
выводится **прямо с entry-ноды** (московский адрес), а блокировка DPI
обходится **десинком TLS**, а не туннелем. Побочный плюс: самый тяжёлый
трафик перестаёт есть канал экзита.

**Как отбирается трафик.** sing-box (стадия 20) отправляет YouTube в отдельный
outbound `youtube-ru` с `routing_mark`. Ядро ставит метку на сокет, а
nft-цепочки стадии 19 забирают в `nfqueue` **ровно помеченный** TCP/443 —
ничего больше с ноды в очередь не попадает. Списки IP/CIDR вести не нужно:
что считать YouTube, решает роутер по домену (`geosite:youtube` + суффиксы,
включая `googlevideo.com` — это само видео).

**Порядок правил критичен.** YouTube-правила стоят выше `geoip: ru`. GGC-хосты
(`rrN---sn-*.googlevideo.com`) физически стоят у российских провайдеров, и
geoip увёл бы их в `direct-ru` — тот же выход, но **без метки**, то есть без
десинка. Симптом: «морда открывается, видео виснет».

**QUIC режется намеренно** (`quic: block`): десинк проверен на TCP-TLS, а на
UDP/443 плеер висит до таймаута. Блок заставляет откатиться на TCP сразу.

**Проверка — только A/B.** Статус юнита ничего не доказывает: nfqws2
поднимается и с нерабочей стратегией. На entry лежит `anysda-yt-check`: он
дёргает один и тот же хост из-под диаг-юзера (его трафик nft метит той же
меткой — это ровно клиентский путь) и из-под root (чистый путь провайдера).

```bash
ssh root@<entry> anysda-yt-check          # 0 = обход живой
ssh root@<entry> 'nft list table inet ytzapret'      # счётчики очереди
ssh root@<entry> 'journalctl -u anysda-yt-nfqws -n 30'
```

Стадия 19 гоняет эту проверку сама и **валит деплой**, если обход не
заработал. Это защита: без неё стадия 20 увела бы YouTube на РФ-выход, где его
режет DPI, и он лёг бы у всех клиентов разом. Стадия 19 поэтому и стоит в
пайплайне **до** стадии 20.

**Если A/B не проходит:**

1. `youtube.offload: off` — TSO/GSO на WAN может склеить ClientHello в
   super-пакет, и резать будет нечего;
2. подобрать стратегию: `/opt/zapret2/blockcheck2.sh` на entry, результат — в
   `youtube.strategy`;
3. `youtube.route: off` — откат к прежнему поведению (YouTube через экзиты,
   с рекламой, но работает).

Дома этот же приём применён на отдельном боксе (CT302 за VyOS); там отбор идёт
списками сетей, и они протухают молча. Здесь этой проблемы нет by design.

---

## Failover-вотчдог экзитов

**`interrupt_exist_connections: false`** на всех трёх группах sing-box
(`direct-best`, `warp-best`, `foreign-best`). Переключение на новый экзит
берёт только НОВЫЕ соединения; живые (в т.ч. идущая загрузка) дорабатывают на
старом. Раньше каждое переключение рвало всё сразу - при 63 переключениях за
7 дней это било по всем клиентам, а не только по тем, чей экзит правда умер.

**Пороги вотчдога** (`infra/scripts/21-failover-watchdog.sh`):
`PROBE_TIMEOUT_MS=3000`, `DEAD_AFTER=3` - отсекают короткие всплески (2
промаха по 1500 мс с возвратом через 2 минуты), не удлиняя признание
настоящего простоя больше, чем на один лишний тик.

**Журнал вотчдога** (`infra/lib/failover-watchdog.py`) пишет причину каждого
промаха зонда: `таймаут`, `отказ соединения`, `код <N>` или текст исключения -
раньше писался только факт промаха без причины, и разбор простоев упирался в
«следов нет».

---

## Backup & Disaster Recovery

`./deploy.sh backup` снимает **один зашифрованный архив** всего, что нужно для
полного восстановления entry-ноды: `db.sqlite` (через `sqlite3 .backup` — без
даунтайма панели), `/etc/anysda/*` (admin-password, tgbot-secret, clash-secret,
telegram-runtime, manual-routes, session-secret), **серверный приватный ключ
WireGuard** (`/etc/wireguard/wg0.conf` — без него клиентские `.conf` ломаются),
**OpenVPN CA + PKI** (`/etc/openvpn/pki/`, `server.conf`, `ccd/`, `crl.pem` —
без них существующие `.ovpn` ломаются), **IKEv2 CA + server cert/key + swanctl
conf'ы** (`/etc/strongswan`, `/etc/swanctl` — без них клиенты не доверят
сертификату сервера) и `manifest.json` с SHA-256 каждого компонента. Архив шифруется [`age`](https://github.com/FiloSottile/age)
(symmetric passphrase) на entry **до** записи на диск — plaintext `.tar.gz`
никогда не попадает в `/var/backups/...` или в S3.

### Команды

```bash
./deploy.sh backup                    # снять архив сейчас, в active backend
./deploy.sh backup-list               # список доступных архивов
./deploy.sh restore                   # восстановить из latest (default)
./deploy.sh restore <archive-name>    # из конкретного архива
./deploy.sh restore <archive> --force # перетереть не-пустую db.sqlite
```

Пример вывода `backup`:

```
backup:       /var/backups/anysda-vpn2/anysda-vpn2-2026-05-23T020000Z.tar.gz.age
size:         245 678 bytes
hash:         sha256:7f8a...
backend:      local
retention:    keep last 10
```

### Где хранятся

| Backend | Куда | Конфиг |
|---|---|---|
| `local` (default) | `/var/backups/anysda-vpn2/*.tar.gz.age` (chmod 700) | `backup.local.dir` |
| `s3` | объект `s3://<bucket>/<name>.tar.gz.age`, любой S3-совместимый endpoint | `backup.s3.*` |

`/var/backups/anysda-vpn2/` — стандартный путь Linux: можешь натравить туда
свой внешний backup-агент (Restic, BorgBackup, rsync.net) — он будет видеть
готовые `.tar.gz.age` и тянуть их куда угодно. Этот путь и предполагает
свободное использование сторонних инструментов.

Pre-restore safety snapshots складываются в `/var/backups/anysda-vpn2/.pre-restore/`
и **исключены из ротации** — пока их вручную не удалят, они никуда не денутся.
Если restore прошёл криво — пригодится восстановиться обратно.

### Расписание

Бэкап раз в сутки в **02:00 по локальной TZ entry-ноды** — `systemd.timer`
с `Persistent=true` (если нода была выключена в 02:00, прогон случится
после загрузки). По умолчанию **включено** (`backup.schedule: daily`);
отключить — `schedule: off` в config.yaml или ответом «n» в setup.sh.

Юнит читает системную таймзону через `timedatectl`. Хочешь МСК вместо UTC —
один раз на entry:
```bash
ssh root@<entry> 'timedatectl set-timezone Europe/Moscow'
./deploy.sh 26-backup ru   # daemon-reload подхватит новую TZ
```
Юнит: `anysda-backup.timer` / `.service`, журнал — `journalctl -u anysda-backup`.

### Ротация

- Локально: keep last `backup.retention` (default 10), удаляются самые старые.
  **Последний успешный бэкап никогда не удаляется** — даже если он выходит
  за лимит retention, остаётся.
- S3: тот же `retention` применяется client-side при следующей выгрузке.
  Дополнительно **рекомендуется поставить S3 lifecycle policy** на bucket
  (например `Expiration: Days=30`) — это belt-and-suspenders на случай если
  client-side ротация по какой-то причине не сработала.

### Runbook — entry с нуля

После полной потери entry-ноды (хостер пересоздал / диск убит / DC сгорел):

```bash
# 1. На свежей Ubuntu 24.04 entry: ставим SSH-ключ оркестратора
#    (или будем заходить паролем — config.yaml содержит его)
ssh root@<новый-entry-ip>

# 2. Клонируем репозиторий
git clone https://github.com/anysda/vpn2 /opt/anysda-vpn2
cd /opt/anysda-vpn2

# 3. Кладём резервную копию config.yaml с оркестратора (он же — мастер-секрет)
#    config.yaml содержит backup.passphrase, который ОБЯЗАТЕЛЕН для restore.
#    Переноси его на новую entry безопасным каналом (scp с старой машины, USB и т.п.).
scp config.yaml-сохранённый-где-то root@<новый-entry-ip>:/opt/anysda-vpn2/config.yaml
chmod 600 /opt/anysda-vpn2/config.yaml

# 4. Стандартный деплой (2-5 мин) — поднимает весь стек на пустой ноде
./deploy.sh

# 5. Кладём свежий backup-файл (если нет в /var/backups уже) или используем S3
./deploy.sh backup-list                       # посмотреть что есть
./deploy.sh restore latest --force            # --force т.к. свежая db.sqlite не пустая после deploy

# 6. Существующие клиенты переподключаются со СВОИМИ старыми WG/OVPN-конфигами,
#    БЕЗ перевыпуска ключей. Если CA OpenVPN или server-private-key WG не
#    восстановились — клиенты увидят rejected handshakes; это значит backup
#    был сделан до того, как ключи были инициализированы — пересоздавай клиентов.
```

`config.yaml` — **мастер-секрет файл**. Помимо backup.passphrase в нём root-пароли
всех нод (`entry.password`, `exits[].password`), `admin.password` панели,
Telegram bot_token, и сохранённый `orchestrator_key` для повторного бутстрапа
exit-нод. `chmod 600`, не коммитить в git, не пересылать по неконтролируемым
каналам. **На компрометацию оркестратора:** ротируй config.yaml (новые пароли
+ новый passphrase) И **перешифруй существующие backup'ы новым passphrase**,
иначе у нападающего остаётся возможность их расшифровать.

### Безопасность хранения

⚠️ **Backup-таргет должен жить на отдельной инфраструктуре от entry-ноды.**
Если backup в `/var/backups/anysda-vpn2/` — стандартное это нормально для
повседневной работы (rollback), но **это не disaster-recovery**: пожар в DC
заберёт и entry, и его локальные `.tar.gz.age`. Для DR нужен внешний:
- S3 (через настройку backend) на другом провайдере / в другом регионе
- ИЛИ `rsync` / `restic` / `borg` из `/var/backups/anysda-vpn2/` на удалённое хранилище

⚠️ **age-passphrase — единственный секрет.** Если потерян — все архивы
становятся нечитаемыми. Сохраняй где-то ВНЕ entry-ноды (1Password, KeePassXC,
бумажка в сейфе).

### Конфигурация

Заполняется интерактивно через `./setup.sh` (вопросы про бэкап появляются
после Telegram-настройки). Все настройки можно пропустить нажатием Enter:
включён по умолчанию, backend=local, **schedule=daily** (02:00 по локальной
TZ entry), passphrase auto-генерится и распечатывается **один раз** в выводе
мастера — **запиши её в этот момент в менеджер паролей и храни ВНЕ
entry-ноды**: больше она нигде открыто не покажется, а потеря = потеря
всех существующих архивов (см. предупреждение ниже).

Прямой вид в `config.yaml` (см. [config.example.yaml](config.example.yaml)):

```yaml
backup:
  enabled: true
  backend: local              # local | s3
  passphrase: ABC123XYZ...    # age symmetric passphrase
  retention: 10
  schedule: daily             # off | daily — 02:00 по локальной TZ entry-ноды
  local:
    dir: /var/backups/anysda-vpn2
  # s3:                       # раскомментировать когда backend: s3
  #   endpoint: https://s3.cloud.ru     # любой S3-совместимый
  #   bucket: anysda-vpn2-backups
  #   region: ru-central-1              # ОБЯЗАТЕЛЕН: cloud.ru→ru-central-1,
  #                                     # yandex→ru-central1, selectel→ru-1,
  #                                     # AWS→регион из endpoint
  #   access_key: ""
  #   secret_key: ""
```

Стадия `26-backup` (между `25-monitoring` и `30-frontend`) сама ставит `age`,
`expect`, `sqlite3` (и официальный бинарь aws-cli v2 из репозитория Amazon, если
backend: s3 — apt-пакет `awscli` выкинут из репов Ubuntu 24.04), кладёт скрипты в
`/usr/local/bin/anysda-{backup,restore,backup-list}.sh`, рендерит секреты
в `/etc/anysda/backup.env` (chmod 600), и (опционально) включает systemd-timer.
Идемпотентна — повторный запуск только перезаписывает env и скрипты.


---

## Как пользоваться админским ботом

Бот опционален; включается, если в `setup.sh` (или в разделе «Боты»
веб-панели) заданы `bot_token` и `chat_id`. Админский чат — тот, чей
`chat_id` указан (своя личка с ботом или приватная группа с
выключенным Group Privacy у `@BotFather`).

### Команды

| Команда | Что делает |
|---|---|
| `/clients` | управление клиентами (список + создание + действия) |
| `/status` | статус нод (CPU/RAM/трафик) и outbound'ов (RTT) |
| `/nodes` | список exit-нод с RTT |
| `/start`, `/help` | админское приветствие |

### `/clients` — управление клиентами

Бот пришлёт список — каждый клиент это кнопка вида
`✅ Иван Иванов — 3/3 девайс.` (`✅` активен, `❄️` заморожен; `3/3` —
устройств/лимит). Внизу — кнопка `➕ Создать клиента`.

Тапни клиента → откроется карточка с кнопками:

| Кнопка | Действие |
|---|---|
| `❄️ Заморозить` / `☀️ Разморозить` | приостановить/вернуть доступ |
| `➖ Лимит` / `➕ Лимит` | изменить лимит устройств на ±1 |
| `🗓 Срок` | задать дату окончания (`ДД.ММ.ГГГГ` или `бессрочно`) |
| `📨 Отправить приглашение` | бот пришлёт готовое сообщение-инвайт для пересылки клиенту |
| `🗑 Удалить` | удалить клиента (со спросом подтверждения) |
| `‹ К списку` | назад |

Создать: `➕ Создать клиента` → бот спросит имя → клиент создан, сразу
открыта его карточка (фильтрация трафика включается по умолчанию).

### Конечный клиент в боте

Когда админ нажмёт «📨 Отправить приглашение» — бот пришлёт в админский
чат **готовый текст с ссылкой**
`t.me/<bot>?start=<password>` для пересылки клиенту. Клиент жмёт
ссылку, бот привязывает его чат, открывает меню устройств — дальше он
сам качает WG/OVPN-конфиги, добавляет устройства и видит уведомления
(смена имени, лимита, срока, заморозка, удаление профиля).

«Отозвать доступ к боту» в карточке клиента в панели — отвязывает
Telegram **и перевыпускает пароль клиента**: старая инвайт-ссылка
становится недействительной.

### Чего бот не делает (это только в веб-панели)

- Перевыпуск ключей устройств/клиента.
- Манипуляции с маршрутизацией.
- Управление настройками бота (токен, chat_id, admin_username).

