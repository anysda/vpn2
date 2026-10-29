# Резервные копии и восстановление

## Что в архиве

`./deploy.sh backup` снимает один зашифрованный архив всего, что нужно для
полного восстановления entry-ноды:

- `db.sqlite` панели (через `sqlite3 .backup`, без простоя панели);
- `/etc/anysda/*`: admin-password, tgbot-secret, clash-secret,
  telegram-runtime, manual-routes, session-secret;
- серверный приватный ключ WireGuard (`/etc/wireguard/wg0.conf`) - без него
  клиентские `.conf` ломаются;
- OpenVPN CA + PKI (`/etc/openvpn/pki/`, `server.conf`, `ccd/`, `crl.pem`) -
  без них ломаются существующие `.ovpn`;
- IKEv2 CA, серверный сертификат и ключ, конфиги swanctl (`/etc/strongswan`,
  `/etc/swanctl`);
- хранилище Caddy (`/var/lib/caddy/.local/share/caddy`: сертификаты, ключи,
  ACME-аккаунты);
- `manifest.json` с SHA-256 каждого компонента.

Архив шифруется [`age`](https://github.com/FiloSottile/age) (symmetric
passphrase) на entry до записи на диск: открытый `.tar.gz` не попадает ни в
`/var/backups`, ни в S3.

## Команды

```bash
./deploy.sh backup                    # снять архив сейчас
./deploy.sh backup-list               # список архивов
./deploy.sh restore                   # восстановить из latest
./deploy.sh restore <archive-name>    # из конкретного архива
./deploy.sh restore <archive> --force # перетереть непустую db.sqlite
```

Если база панели и конфиги VPN расходятся (не прошла выдача или отзыв
устройства), архив всё равно сохраняется: `./deploy.sh backup` завершается
успешно с предупреждением и номерами устройств, в «Событиях» панели -
«Бэкап сохранён с предупреждением». Эти устройства перевыпусти в панели.
Сбой бэкапа - ненулевой код выхода и «Бэкап не удался».

## Где хранятся

| Backend | Куда | Конфиг |
|---|---|---|
| `local` (default) | `/var/backups/anysda-vpn2/*.tar.gz.age` (chmod 700) | `backup.local.dir` |
| `s3` | `s3://<bucket>/<name>.tar.gz.age`, любой S3-совместимый endpoint | `backup.s3.*` |

Из `/var/backups/anysda-vpn2/` готовые `.tar.gz.age` может забирать внешний
агент (restic, borg, rsync). Снимки перед restore лежат в
`/var/backups/anysda-vpn2/.pre-restore/` и исключены из ротации.

Локальный бэкап - это откат, а не disaster recovery: авария в дата-центре
заберёт и entry, и её архивы. Для DR нужен внешний target: S3 у другого
провайдера или синхронизация на удалённое хранилище.

## Расписание и ротация

Раз в сутки с 02:00 по системному часовому поясу entry-ноды, случайная
задержка до 5 минут (`anysda-backup.timer`, `Persistent=true`: если нода была
выключена в 02:00, прогон случится после загрузки). Отключить -
`backup.schedule: off`. Сменить пояс:

```bash
ssh root@<entry> 'timedatectl set-timezone Europe/Moscow'
./deploy.sh 26-backup ru
```

Локально хранится `backup.retention` последних архивов (по умолчанию 10),
последний успешный не удаляется никогда. В S3 та же ротация применяется при
следующей выгрузке; дополнительно стоит поставить lifecycle policy на bucket.

## Конфигурация

```yaml
backup:
  enabled: true
  backend: local              # local | s3
  passphrase: ABC123XYZ...    # age symmetric passphrase
  retention: 10
  schedule: daily             # off | daily
  local:
    dir: /var/backups/anysda-vpn2
  # s3:
  #   endpoint: https://s3.cloud.ru
  #   bucket: anysda-vpn2-backups
  #   region: ru-central-1    # обязателен: cloud.ru → ru-central-1,
  #                           # yandex → ru-central1, selectel → ru-1
  #   access_key: ""
  #   secret_key: ""
```

Passphrase генерирует `setup.sh` и пишет в `config.yaml`. Стадия
`26-backup` ставит `age`, `expect`, `sqlite3` (и aws-cli v2 при `backend:
s3`), кладёт скрипты `/usr/local/bin/anysda-{backup,restore,backup-list}.sh`,
рендерит `/etc/anysda/backup.env` и включает таймер.

## config.yaml - мастер-секрет

В нём root-пароли всех нод, `admin.password` панели, токен бота,
`orchestrator_key` для повторного бутстрапа exit-нод (вход по паролю на
экзитах отключён, без ключа новая entry на них не зайдёт) и
`backup.passphrase`. Потеря passphrase - потеря всех архивов. Храните копию
вне entry-ноды, `chmod 600`, не коммитьте. При компрометации оркестратора
ротируйте `config.yaml` и перешифруйте существующие архивы новым passphrase.

## Восстановление entry-ноды с нуля

```bash
# 1. свежая entry
ssh root@<новый-entry-ip>

# 2. репозиторий
git clone https://github.com/anysda/vpn2
cd vpn2

# 3. сохранённый config.yaml, безопасным каналом
scp config.yaml root@<новый-entry-ip>:vpn2/config.yaml
chmod 600 config.yaml

# 4. временно panel.domain: "" и sso.enabled: false (SSO требует HTTPS):
#    первый деплой поднимет HTTP и не выпустит новый сертификат до restore
./deploy.sh

# 5. вернуть panel.domain и sso.enabled, восстановить
./deploy.sh backup-list
./deploy.sh restore latest --force    # база после deploy не пустая
```

Restore сам пересобирает стадии 29/20/21/26/27/35/30 под восстановленные
секреты; пароль из архива имеет приоритет над `admin.password` текущего
`config.yaml`. Restore останавливает Caddy и cert-sync, возвращает хранилище
с владельцем `caddy:caddy`, затем запускает службы. Сохранённый сертификат
должен подходить домену и оставаться действующим, иначе Caddy обратится к
ACME.

Клиенты переподключаются со своими старыми конфигами без перевыпуска
ключей. Если видны rejected handshakes - архив был снят до инициализации
ключей, клиентов придётся пересоздать. Если у entry сменился IP, а в
конфигах клиентов он прописан напрямую, им нужны свежие конфиги; с
`entry.public_host` (домен) хватит A-записи.
