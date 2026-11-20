#!/usr/bin/env bash
# Стенд внутри одной машины с KVM: entry и экзит - ВМ из облачного образа
# Ubuntu (STAND_OS=24.04 или 26.04, по умолчанию 26.04; сборка образа
# закреплена серийным номером, STAND_IMAGE_URL задаёт образ сам) в своей сети
# за NAT хоста. Продукт ставится штатным
# ./deploy.sh с самого entry, как по README; 99-verify гоняет настоящий
# трафик WireGuard/OpenVPN/IKEv2.
#
#   sudo infra/tests/ci-stand.sh up [--release]         поднять ВМ; с флагом - с дисков сохранённого выпуска
#   sudo infra/tests/ci-stand.sh up-bg [--release] | up-wait  то же фоном и ожидание
#   sudo infra/tests/ci-stand.sh deploy <образ панели>  полный деплой
#   sudo infra/tests/ci-stand.sh deploy-release <тег> <манифест>  установка выпуска по манифесту
#   sudo infra/tests/ci-stand.sh release-save           остановить ВМ, диски и базу слепка выпуска - в кэш
#   infra/tests/ci-stand.sh release-key <тег>            ключ кэша установленного выпуска (stand.yml)
#   sudo infra/tests/ci-stand.sh deploy-upgrade <образ> <тег>     поверх выпуска - дерево ветки
#   sudo infra/tests/ci-stand.sh deploy-udp             стадии до 22-adguard
#   sudo infra/tests/ci-stand.sh verify [--stop-openvpn]
#   sudo infra/tests/ci-stand.sh ansible-ping           ansible с entry достаёт все узлы
#   sudo infra/tests/ci-stand.sh udp-cut [--break-tcp-fallback]
#   sudo infra/tests/ci-stand.sh snapshot <каталог> [префикс]  слепок обоих узлов после деплоя
#   sudo infra/tests/ci-stand.sh deploy-again <каталог> повторный ./deploy.sh и слепок snapshot-again
#   sudo infra/tests/ci-stand.sh snapshot-corrupt       положительный контроль слепка
#   sudo infra/tests/ci-stand.sh bot-fake-token         боту поддельный токен: пауза, не цикл перезапусков
#   sudo infra/tests/ci-stand.sh backup-fixture <каталог> эталонный архив бэкапа и etalon.expect.json
#   sudo infra/tests/ci-stand.sh restore-check [--foreign-pass] restore эталонного архива и сверка; с флагом - чужой пароль, шаг обязан упасть
#   sudo infra/tests/ci-stand.sh recover-fresh          потеря entry: архив, новая ВМ entry, restore без ключа на экзите
#   sudo infra/tests/ci-stand.sh collect <каталог>      отчёты и журналы
#   sudo infra/tests/ci-stand.sh down                   снести всё
#   infra/tests/ci-stand.sh image-key                    ключ кэша образа и ядра (stand.yml)
#
# Образ панели - локальный docker-образ, собранный из этого же дерева; на
# entry он загружается, как только 00-bootstrap поднял docker, deploy берёт его по panel.image.
# verify возвращает код 99-verify: 0 работает, 1 ошибка, 2 не проверено.
# deploy-udp ставит только то, на чём стоит обрыв UDP: экзит, sing-box entry,
# сторож и AdGuard (DNS sing-box идёт через него).
# udp-cut режет UDP с entry на экзите и проверяет, что трафик ушёл на запасной
# TCP без перезапуска sing-box и вернулся на Hysteria2: 0 прошло, 1 нет.
# Слепок хоста: база снимается в deploy до установки, snapshot после неё
# кладёт в <каталог> snapshot-entry.json и snapshot-exit.json - только то, что
# установка изменила. snapshot-corrupt дописывает строку в юнит на entry:
# сверка с эталоном обязана после этого покраснеть.
set -Eeuo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STAND_DIR=${STAND_DIR:-/var/tmp/vpn2-ci-stand}
# ОС узлов: 24.04 или 26.04. Образ - по серийному номеру сборки: каталог
# release/ переезжает на новую сборку, и эталон слепка хоста расходился бы без
# изменений продукта. Новый номер - отдельной правкой вместе с пересъёмкой эталона.
STAND_OS=${STAND_OS:-26.04}
case $STAND_OS in
  24.04) IMAGE_SERIAL=20261017 ;;
  26.04) IMAGE_SERIAL=20261016 ;;
  *) IMAGE_SERIAL='' ;;
esac
IMAGE_NAME=ubuntu-$STAND_OS-server-cloudimg-amd64.img
IMAGE_KEY=$STAND_OS-$IMAGE_SERIAL
if [[ -n "${STAND_IMAGE_URL:-}" ]]; then
  IMAGE_NAME=${STAND_IMAGE_URL##*/}
  IMAGE_KEY=$STAND_OS-url-$(printf '%s' "$STAND_IMAGE_URL" | sha256sum | cut -c1-12)
fi
# Образ и ядро живут вне STAND_DIR (down его стирает); в CI каталог между
# прогонами хранит actions/cache по ключу image-key.
IMAGE_DIR=${STAND_IMAGE_CACHE:-/var/tmp/vpn2-image-cache}/$IMAGE_KEY
# Установленный выпуск для сценария обновления: диски узлов после чистой
# остановки, база слепка и секреты стенда. В CI каталог хранит actions/cache
# по ключу release-key; диски ссылаются на образ из IMAGE_DIR.
RELEASE_DIR=${STAND_RELEASE_CACHE:-/var/tmp/vpn2-release-cache}
# Адреса вне частных диапазонов: sing-box отклоняет клиентские соединения в
# частные сети, а проверка трафика ходит на адреса entry и экзита.
NET=198.18.10
BRIDGE=vpn2ci
PANEL_REF=vpn2-ci/panel:local
EXIT_TAG='nl'
# имя адрес vCPU память(МиБ) MAC
# entry ведёт почти все стадии - ему все ядра раннера, экзит большей частью ждёт.
NODES=("entry $NET.10 4 4096 52:54:00:12:0a:0a" "exit $NET.20 2 2048 52:54:00:12:0a:14")
ENTRY=$NET.10
EXIT=$NET.20
# Состояние деплоя на entry и ожидание его замка. Выпуск со старой раскладкой
# держит замок в checkout: его ждём, только если файл есть - flock создал бы.
STATE=/var/lib/anysda-vpn2/state
# Пароль панели стенда на entry: деплой его не хранит, вход в панель скриптами - по нему.
ADMIN_PASSWORD_FILE=/root/stand-admin-password
OLD_STATE=/opt/anysda-vpn2/infra/state
DEPLOY_LOCK_WAIT="flock -w 70 /run/lock/anysda-vpn2-deploy.lock true"
DEPLOY_LOCK_WAIT+=" && { [ ! -e $OLD_STATE/deploy.lock ] || flock -w 70 $OLD_STATE/deploy.lock true; }"
# Let's Encrypt на адрес 198.18.x не выпустит: сертификаты выдаёт тестовый ACME (pebble) на мосту,
# без проверки вызова. Стадии берут адрес и корень из окружения ssh-сессии (pam_env, /etc/environment).
PEBBLE_URL=https://github.com/letsencrypt/pebble/releases/download/v2.10.1/pebble-linux-amd64.tar.gz
PEBBLE_SHA256=4f2fcb5bca8c85c9cf73ad140fccfc0d2be40bd81ab99879c79b7b8a0b4f70ed
ACME_URL=https://$NET.1:14000/dir
ACME_ROOT=/etc/vpn2-ci/acme.pem
# Пакеты ВМ идут через кэширующий прокси apt на хосте (apt-cacher-ng на мосту):
# установка на ВМ та же - apt-get update/install с чистой Ubuntu, но пакеты
# берутся из кэша раннера, а не с зеркал на каждом прогоне. Каталог кэша
# переживает прогон через actions/cache (stand.yml).
# HTTPS-источники (репо Docker) идут мимо: прокси не держит туннели CONNECT.
APT_CACHE=${STAND_APT_CACHE:-/var/tmp/vpn2-apt-cache}
APT_PROXY=http://$NET.1:3142
# Сторож возвращает канал на hy2 после стольких секунд живого hy2 (в продукте 60).
# Стенд обрыва пишет его в env-файл сторожа до деплоя, как правят руками: деплой
# строку переносит, по ней же ждёт проверка транспорта в verify.
TCP_RETURN_S=20

log() { printf '[stand] %s\n' "$*"; }
die() { printf '[stand] ✗ %s\n' "$*" >&2; exit 1; }
[[ $STAND_OS == 24.04 || $STAND_OS == 26.04 ]] || die "STAND_OS=$STAND_OS: поддержаны 24.04 и 26.04"

# Машина раннера: модель CPU и ядра один раз, затем нагрузка хоста за отрезок
# с прошлой отметки (/proc/stat в файле стенда переживает шаги workflow).
# steal хоста - время, отнятое у ВМ раннера её гипервизором.
host_machine() {
  log "хост: $(sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo | sort | uniq -c | sed 's/^ *//' | paste -sd ';'), $(nproc) ядер, $(awk '/MemTotal/ {printf "%d", $2 / 1024}' /proc/meminfo) МиБ"
}
# Занятость и steal по строке «cpu» из /proc/stat за отрезок с прошлого вызова
# с тем же файлом-отметкой; первый вызов только ставит отметку.
cpu_delta() {  # <файл-отметка> <строка cpu> <ядер>
  local _cpu u n s i w q sq st p=() dt
  read -r _cpu u n s i w q sq st _ <<<"$2"
  [[ -f "$1" ]] && read -r -a p < "$1"
  echo "$((u + n + s + i + w + q + sq + st)) $((i + w)) $st" > "$1"
  (( ${#p[@]} == 3 )) && dt=$((u + n + s + i + w + q + sq + st - p[0])) && (( dt > 0 )) || return 0
  printf ', занято %d %%, steal %d %% за %d с' "$(( 100 * (dt - (i + w - p[1])) / dt ))" \
    "$(( 100 * (st - p[2]) / dt ))" "$(( dt / $3 / 100 ))"
}
host_stats() {  # <метка>
  mkdir -p "$STAND_DIR"
  log "хост [$1]: load $(cut -d' ' -f1-3 /proc/loadavg)$(cpu_delta "$STAND_DIR/host-stat" "$(head -n1 /proc/stat)" "$(nproc)")"
}
# То же внутри entry: steal гостя - vCPU стенда ждали ядро хоста.
entry_stats() {  # <метка>
  local g
  g=$(ssh_node "$ENTRY" 'cut -d" " -f1-3 /proc/loadavg; head -n1 /proc/stat; nproc' 2>/dev/null) || return 0
  mapfile -t g <<<"$g"
  (( ${#g[@]} == 3 )) || return 0
  log "entry [$1]: load ${g[0]}$(cpu_delta "$STAND_DIR/entry-stat" "${g[1]}" "${g[2]}")"
}

ssh_node() {
  local host=$1
  shift
  # Текст из .bashrc ВМ приходит в stdout раньше вывода команды: команда
  # сначала печатает метку, всё до неё отбрасывается, после неё идёт потоком.
  # Без метки shell узла не дошёл до команды (exit или exec в .bashrc) - это
  # отказ с причиной даже при коде 0; при 255 (соединение) - код и сообщение ssh.
  # Метка и причина - копия MARK и NO_MARK_REASON из infra/vpn2/src/vpn2/verify/stage.py:
  # инструмент пакет не подключает.
  local mark=vpn2-ssh-output-begin st
  local reason='shell узла не дошёл до команды (.bashrc root делает exit или exec), команда не выполнена'
  { ssh -i "$STAND_DIR/id_ed25519" -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=8 "root@$host" \
    "printf '\\n%s\\n' $mark;" "$@" \
      | (while IFS= read -r line; do [[ $line != "$mark" ]] || exec cat; done; exit 3)
    st=("${PIPESTATUS[@]}"); } || :
  if (( st[1] == 3 && st[0] != 255 )); then
    printf 'ssh %s: %s\n' "$host" "$reason" >&2
    return 1
  fi
  return "${st[0]}"
}

net_up() {
  ip link show "$BRIDGE" >/dev/null 2>&1 || ip link add "$BRIDGE" type bridge
  ip addr replace "$NET.1/24" dev "$BRIDGE"
  ip link set "$BRIDGE" up
  sysctl -qw net.ipv4.ip_forward=1
  local wan
  wan=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
  [[ -n "$wan" ]] || die 'нет маршрута по умолчанию на хосте'
  # Docker на хосте ставит FORWARD в DROP: свои правила - первыми.
  iptables -w -t nat -C POSTROUTING -s "$NET.0/24" -o "$wan" -j MASQUERADE 2>/dev/null \
    || iptables -w -t nat -A POSTROUTING -s "$NET.0/24" -o "$wan" -j MASQUERADE
  iptables -w -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -w -I FORWARD 1 -i "$BRIDGE" -j ACCEPT
  iptables -w -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || iptables -w -I FORWARD 1 -o "$BRIDGE" -j ACCEPT
}

# Тестовый ACME на мосту: сертификат его HTTPS - корень доверия Caddy узлов (trusted_roots).
acme_up() {
  local dir=$STAND_DIR/acme
  mkdir -p "$dir"
  if [[ ! -x "$dir/pebble" ]]; then
    curl -fsSL --retry 3 -o "$dir/pebble.tgz" "$PEBBLE_URL"
    sha256sum -c <<<"$PEBBLE_SHA256  $dir/pebble.tgz" >/dev/null || die 'pebble: контрольная сумма не совпала'
    tar -xzf "$dir/pebble.tgz" -C "$dir"
    install -m 755 "$dir/pebble-linux-amd64/linux/amd64/pebble" "$dir/pebble"
  fi
  [[ -f "$STAND_DIR/acme.pem" ]] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 7 \
    -subj /CN=vpn2-ci-acme -addext "subjectAltName=IP:$NET.1" -keyout "$dir/acme.key" -out "$STAND_DIR/acme.pem" 2>/dev/null
  cat > "$dir/pebble.json" <<EOF
{"pebble": {"listenAddress": "$NET.1:14000", "managementListenAddress": "127.0.0.1:15000",
  "certificate": "$STAND_DIR/acme.pem", "privateKey": "$dir/acme.key", "httpPort": 80, "tlsPort": 443,
  "ocspResponderURL": "", "externalAccountBindingRequired": false,
  "profiles": {"default": {"description": "default", "validityPeriod": 7776000},
               "shortlived": {"description": "shortlived", "validityPeriod": 576000}}}}
EOF
  pgrep -f "$dir/pebble -config" >/dev/null && return
  PEBBLE_VA_ALWAYS_VALID=1 PEBBLE_VA_NOSLEEP=1 PEBBLE_WFE_NONCEREJECT=0 \
    setsid nohup "$dir/pebble" -config "$dir/pebble.json" > "$dir/pebble.log" 2>&1 < /dev/null &
  log "тестовый ACME: $ACME_URL"
}

# Прогрев ВМ сразу после загрузки, пока раннер собирает панель и деплой
# раскладывает исходники. Установка та же: деплой сам делает свои apt-get
# update/install, прогрев лишь кладёт индекс apt (через прокси) и, на entry,
# пакеты Docker в кэш apt (HTTPS идёт мимо прокси, это самое долгое
# скачивание 00-bootstrap). Стадии ждут чужие блокировки apt сами.
warm_script() {
  cat <<'WARM'
#!/bin/sh
t0=$(date +%s)
say() { echo "прогрев: $* за $(( $(date +%s) - t0 )) с"; }
# Таймер прошивок образа срабатывает в случайный момент и без выхода в LVFS падает:
# слепок видел бы failed fwupd-refresh от часов, а не от деплоя.
systemctl stop fwupd-refresh.timer 2>/dev/null || true
# Старый mtime заставляет стадию обновить списки самой, как на машине без прогрева;
# --error-on=any - иначе update с ошибкой зеркала возвращает 0. timeout - жёсткий
# предел на случай, когда прокси держит соединение без ответа; по пределу попытка
# неудачна и идёт обычным путём (вторая попытка, затем старый mtime списков).
# Обычный update идёт 5-7 с, с холодным кэшем прокси до 20 с; предел 30 с: зависший
# прокси держит apt деплоя полминуты, а не минуту с лишним.
n=1; ok=
while [ "$n" -le 2 ]; do
  echo "попытка $n" >>/var/log/vpn2-ci-warm-update.log
  if timeout 30 apt-get -q --error-on=any -o Acquire::http::Timeout=15 -o Acquire::Retries=1 update >>/var/log/vpn2-ci-warm-update.log 2>&1; then ok=1; break; fi
  [ "$n" -lt 2 ] && sleep 2
  n=$((n + 1))
done
if [ -z "$ok" ]; then
  touch -d '2 hours ago' /var/lib/apt/lists
  say 'индекс apt не вышел'
elif [ "$n" -eq 1 ]; then say 'индекс apt'; else say 'индекс apt со второй попытки'; fi
[ "$1" = entry ] || exit 0
w=/var/tmp/vpn2-ci-warm
. /etc/os-release
mkdir -p "$w/sources" "$w/lists/partial" "$w/debs"
chown _apt "$w/debs"
curl -fsSL --retry 3 "https://download.docker.com/linux/$ID/gpg" | gpg --dearmor --yes -o "$w/docker.gpg" || exit 0
echo "deb [arch=$(dpkg --print-architecture) signed-by=$w/docker.gpg] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  > "$w/sources/docker.list"
o="-o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts=$w/sources -o Dir::State::Lists=$w/lists"
# shellcheck disable=SC2086
apt-get -q $o update >/dev/null || { say 'индекс Docker не вышел'; exit 0; }
cd "$w/debs" || exit 0
# shellcheck disable=SC2086
if apt-get -q $o download docker-ce docker-ce-cli containerd.io >/dev/null; then
  mv ./*.deb /var/cache/apt/archives/ && say "пакеты Docker в кэше apt"
else
  say 'пакеты Docker не скачались'
fi
WARM
}

seed() {
  local name=$1 ip=$2 mac=$3 password=$4 dir=$STAND_DIR/seed-$1
  mkdir -p "$dir"
  printf 'instance-id: vpn2-%s\nlocal-hostname: vpn2-%s\n' "$name" "$name" > "$dir/meta-data"
  cat > "$dir/network-config" <<EOF
version: 2
ethernets:
  lan:
    match:
      macaddress: "$mac"
    set-name: eth0
    addresses: [$ip/24]
    routes:
      - to: default
        via: $NET.1
    nameservers:
      addresses: [1.1.1.1, 8.8.8.8]
EOF
  # Как у хостера: у root есть пароль и drop-in sshd с PasswordAuthentication yes;
  # деплой ходит только по ключу и вход по паролю обязан закрыть.
  # Таймеры apt выключены, чтобы автообновление не держало apt весь прогон.
  # Короткие ожидания стенда - в окружение ssh и в drop-in сторожа.
  # ВМ одноразовые: dpkg без fsync на каждый файл, без man/doc/info и без
  # пересборки базы man-db триггером; apt update без переводов и индексов
  # command-not-found/AppStream. Пакеты и сервисы ставятся те же.
  cat > "$dir/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - {name: root, password: "$password", type: text}
write_files:
  - path: /etc/ssh/sshd_config.d/10-vpn2-ci.conf
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
  - path: /root/.ssh/authorized_keys
    permissions: '0600'
    content: |
      $(cat "$STAND_DIR/id_ed25519.pub")
  - path: /etc/apt/apt.conf.d/00vpn2-ci-proxy
    content: |
      Acquire::http::Proxy "$APT_PROXY";
      Acquire::https::Proxy "DIRECT";
      Acquire::Languages "none";
      Acquire::IndexTargets::deb::CNF::DefaultEnabled "false";
      Acquire::IndexTargets::deb::DEP-11::DefaultEnabled "false";
      Acquire::IndexTargets::deb::DEP-11-icons::DefaultEnabled "false";
      Acquire::IndexTargets::deb::DEP-11-icons-small::DefaultEnabled "false";
  - path: /etc/dpkg/dpkg.cfg.d/90vpn2-ci
    content: |
      force-unsafe-io
      path-exclude=/usr/share/man/*
      path-exclude=/usr/share/info/*
      path-exclude=/usr/share/doc/*
      path-include=/usr/share/doc/*/copyright
  - path: /etc/environment
    append: true
    content: |
      TLS_ACME_URL=$ACME_URL
      TLS_ACME_ROOT=$ACME_ROOT
  - path: $ACME_ROOT
    content: |
$(sed 's/^/      /' "$STAND_DIR/acme.pem")
  - path: /usr/local/sbin/vpn2-ci-warm
    permissions: '0755'
    content: |
$(warm_script | sed 's/^/      /')
bootcmd:
  - [systemctl, disable, --now, apt-daily.timer, apt-daily-upgrade.timer]
  # Службы образа, которых продукт не касается: packagekit будит хук apt после
  # каждого apt-get update, остальные жгут CPU ВМ на загрузке.
  - [systemctl, mask, --now, packagekit.service, fwupd.service, udisks2.service, multipathd.service, multipathd.socket]
  - echo 'man-db man-db/auto-update boolean false' | debconf-set-selections
  - [rm, -f, /var/lib/man-db/auto-update]
runcmd:
  - [systemctl, restart, ssh]
  - [systemd-run, --unit=vpn2-ci-warm, /usr/local/sbin/vpn2-ci-warm, $name]
EOF
  cloud-localds --network-config="$dir/network-config" "$STAND_DIR/$name-seed.iso" "$dir/user-data" "$dir/meta-data"
}

# Слушает только мост стенда. Поднимается в up до запуска ВМ (их прогрев
# идёт через него): каталог кэша actions/cache восстанавливает до up от
# пользователя раннера, потом владелец - прокси, иначе он не допишет новые
# пакеты. Деплой лишь проверяет, что прокси жив: перезапуск оборвал бы прогрев.
apt_proxy_up() {
  if [[ "${1:-}" == --if-down ]] && curl -fs -o /dev/null "$APT_PROXY/acng-report.html"; then
    log "прокси apt: $APT_PROXY уже поднят, в кэше $(du -sh "$APT_CACHE" | cut -f1)"
    return 0
  fi
  mkdir -p "$APT_CACHE"
  printf 'CacheDir: %s\nBindAddress: %s\nPort: 3142\n' "$APT_CACHE" "$NET.1" \
    > /etc/apt-cacher-ng/zz-vpn2-ci.conf
  # Запросы ВМ к archive.ubuntu.com прокси шлёт на бэкенды uburep по порядку.
  # Без этого файла их угадывает postinst по региону раннера (mirrors.ubuntu.com):
  # первый - тот же, что в источниках ВМ, следом запасные вне ubuntu.com.
  { echo 'http://archive.ubuntu.com/ubuntu/'
    grep -Ev '^[[:space:]]*(#|$)' "$REPO_ROOT/.github/actions/apt-install/mirrors.txt" | sed 's|/*$|/|'
  } > /etc/apt-cacher-ng/backends_ubuntu
  chown -R apt-cacher-ng: "$APT_CACHE"
  systemctl restart apt-cacher-ng
  local deadline=$((SECONDS + 30))
  until curl -fs -o /dev/null "$APT_PROXY/acng-report.html"; do
    (( SECONDS < deadline )) || die "прокси apt не поднялся на $APT_PROXY"
    sleep 1
  done
  log "прокси apt: $APT_PROXY, в кэше $(du -sh "$APT_CACHE" | cut -f1)"
}

# Сколько прокси взял с зеркал и сколько отдал ВМ (журнал apt-cacher-ng:
# I - пришло с зеркала, O - отдано клиенту).
apt_proxy_stats() {
  local log=/var/log/apt-cacher-ng/apt-cacher.log
  [[ -f "$log" ]] || return 0
  awk -F'|' '$2 == "I" {i += $3} $2 == "O" {o += $3}
    END {printf "[stand] прокси apt: с зеркал %.1f МБ, отдано ВМ %.1f МБ\n", i / 1e6, o / 1e6}' "$log"
}

# Каталоги образа по порядку: Canonical, затем зеркала вне ubuntu.com с той же
# сборкой - сбой DNS Canonical их не задевает. Сумма - из SHA256SUMS источника.
image_sources() {
  if [[ -n "${STAND_IMAGE_URL:-}" ]]; then echo "${STAND_IMAGE_URL%/*}"; return; fi
  local mirror
  for mirror in https://cloud-images.ubuntu.com https://mirror.yandex.ru/ubuntu-cloud-images \
    https://mirror.nju.edu.cn/ubuntu-cloud-images; do
    echo "$mirror/releases/$STAND_OS/release-$IMAGE_SERIAL"
  done
}

# check_sum <каталог> <файл> <имя в SHA256SUMS>
check_sum() {
  local sums
  sums=$(curl -fsSL --retry 3 --connect-timeout 15 "$1/SHA256SUMS") || return 1
  printf '%s\n' "$sums" | awk -v n="$3" -v f="$2" '$2 == n || $2 == "*" n {print $1 "  " f}' \
    | grep . | sha256sum --check --status
}

fetch_image() {
  local image=$1 src started=$SECONDS
  for src in $(image_sources); do
    log "образ: $src/$IMAGE_NAME"
    if download_image "$src/$IMAGE_NAME" "$image.part" && check_sum "$src" "$image.part" "$IMAGE_NAME"; then
      mv "$image.part" "$image"
      log "образ скачан за $((SECONDS - started)) с"
      return 0
    fi
    log "образ с $src не скачан или сумма не сошлась"
  done
  return 1
}

# Один поток упирается в ~50 МБ/с: образ (~850 МБ) - четырьмя диапазонами
# с одного зеркала (адрес после редиректа); без длины - одним потоком.
download_image() {
  local url part=$2 size i a b rc=0 pids=()
  url=$(curl -fsIL --retry 3 --connect-timeout 15 -o /dev/null -w '%{url_effective}' "$1") || return 1
  size=$(curl -fsI --retry 3 "$url" | tr -d '\r' | awk 'tolower($1) == "content-length:" {print $2}' | tail -n1)
  if ! [[ "$size" =~ ^[0-9]+$ ]] || (( size <= 4 )); then
    curl -fsSL --retry 3 -o "$part" "$url"
    return
  fi
  for i in 0 1 2 3; do
    a=$((size * i / 4)) b=$((size * (i + 1) / 4 - 1))
    curl -fsS --retry 3 -r "$a-$b" -o "$part$i" "$url" & pids+=($!)
  done
  for i in "${pids[@]}"; do wait "$i" || rc=1; done
  (( rc == 0 )) && cat "$part"0 "$part"1 "$part"2 "$part"3 > "$part"
  rm -f "$part"0 "$part"1 "$part"2 "$part"3
  (( rc == 0 )) && (( $(stat -c %s "$part") == size ))
}

# Ядро того же образа (каталог unpacked рядом с ним): qemu грузит его сам,
# без GRUB, читающего initrd (~70 МБ) через BIOS, - до ядра ~2 с вместо ~9.
# Без initrd: virtio и ext4 в ядре, корень - первый раздел диска (~4 с
# загрузки). Корень тот же, плюс mitigations=off: ВМ одноразовая, чужого кода
# в ней нет, а retpoline и Safe RET на AMD раннера тормозят dpkg и стадии.
# snapd продукту не нужен: его засев держит cloud-init на загрузке ~2 с.
# Нет ядра - через GRUB.
fetch_kernel() {
  local kernel=$1 name src
  name=${IMAGE_NAME%.img}-vmlinuz-generic
  for src in $(image_sources); do
    if curl -fsSL --retry 3 --connect-timeout 15 -o "$kernel.part" "$src/unpacked/$name" \
      && check_sum "$src/unpacked" "$kernel.part" "$name"; then
      mv "$kernel.part" "$kernel"
      log "ядро: $src/unpacked/$name"
      return 0
    fi
  done
  return 1
}

vm_wait_ready() {
  local name=$1 ip=$2 up_t0=$3 deadline=$((SECONDS + 600))
  until ssh_node "$ip" 'cloud-init status --wait >/dev/null 2>&1; test -f /var/lib/cloud/instance/boot-finished'; do
    (( SECONDS < deadline )) || { tail -n 40 "$STAND_DIR/$name-console.log" >&2; die "ВМ $name не поднялась за 10 мин"; }
    sleep 1
  done
  # Ядро снаружи образа: модули стадий (wireguard, xfrm) берутся из корня.
  ssh_node "$ip" 'test -d "/lib/modules/$(uname -r)/kernel"' \
    || die "ВМ $name: ядро $(ssh_node "$ip" 'uname -r') без модулей в образе"
  log "ВМ $name готова через $((SECONDS - up_t0)) с от начала up: $(ssh_node "$ip" '. /etc/os-release; printf "%s, ядро %s, dpkg %s, база man-db %s" "$PRETTY_NAME" "$(uname -r)" \
    "$(grep -q unsafe-io /etc/dpkg/dpkg.cfg.d/90vpn2-ci 2>/dev/null && echo без fsync || echo с fsync)" \
    "$(test -e /var/lib/man-db/auto-update && echo обновляется || echo не обновляется)"')"
}

# ВМ узла <строка NODES> на одноразовом диске поверх <базы> (пусто - образ Ubuntu): seed, tap и qemu.
vm_boot() {
  local name ip cpus mem mac tap base=${2:-$IMAGE_DIR/$IMAGE_NAME} t0=${3:-$SECONDS} vhost='' boot=()
  read -r name ip cpus mem mac <<<"$1"
  [[ -c /dev/vhost-net ]] && vhost=,vhost=on
  if [[ -f $IMAGE_DIR/vmlinuz ]]; then
    boot=(-kernel "$IMAGE_DIR/vmlinuz"
      -append 'root=/dev/vda1 rootwait ro console=tty1 console=ttyS0 mitigations=off systemd.mask=snapd.service systemd.mask=snapd.socket systemd.mask=snapd.seeded.service')
  fi
  tap=$BRIDGE-$name
  seed "$name" "$ip" "$mac" "$(cat "$STAND_DIR/root-password")"
  # Диск одноразовый: fsync гостя (dpkg на каждый пакет) хосту не нужен.
  qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$STAND_DIR/$name.qcow2" 20G
  ip link show "$tap" >/dev/null 2>&1 || ip tuntap add dev "$tap" mode tap
  ip link set "$tap" master "$BRIDGE" up
  qemu-system-x86_64 -name "vpn2-$name" -machine q35,accel=kvm -cpu host -smp "$cpus" -m "$mem" \
    -drive "file=$STAND_DIR/$name.qcow2,if=virtio,discard=unmap,cache=unsafe" \
    -drive "file=$STAND_DIR/$name-seed.iso,if=virtio,format=raw,readonly=on" \
    -netdev "tap,id=lan,ifname=$tap,script=no,downscript=no$vhost" -device "virtio-net-pci,netdev=lan,mac=$mac" \
    -device virtio-rng-pci -display none -serial "file:$STAND_DIR/$name-console.log" \
    -daemonize -pidfile "$STAND_DIR/$name.pid" "${boot[@]}"
  log "ВМ $name: $ip, ${cpus} vCPU, ${mem} МиБ, запущена через $((SECONDS - t0)) с${boot:+, ядро без GRUB}${vhost:+, vhost-net}"
}

cmd_up() {
  [[ -e /dev/kvm ]] || die '/dev/kvm нет: нужна машина с аппаратной виртуализацией'
  local from=''
  [[ ${1:-} != --release ]] || from=$RELEASE_DIR
  mkdir -p "$STAND_DIR"
  host_machine
  host_stats 'начало up'
  chmod 700 "$STAND_DIR"
  local image=$IMAGE_DIR/$IMAGE_NAME kernel=$IMAGE_DIR/vmlinuz fetch='' up_t0=$SECONDS
  mkdir -p "$IMAGE_DIR"
  # Образ качается, пока ставится qemu.
  if [[ -f "$image" ]]; then
    log "образ из кэша: $image"
  else
    fetch_image "$image" &
    fetch=$!
  fi
  local kfetch=''
  if [[ -f "$kernel" ]]; then
    log "ядро из кэша: $kernel"
  else
    fetch_kernel "$kernel" & kfetch=$!
  fi
  if ! command -v qemu-system-x86_64 cloud-localds qemu-img apt-cacher-ng >/dev/null; then
    # Раннер одноразовый: dpkg без fsync, needrestart не опрашивает сервисы.
    # Индекс apt в образе раннера свежий: update (~3 с) - только если без
    # него пакеты не встали.
    local install=(env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -yq
      --no-install-recommends -o Dpkg::Options::=--force-unsafe-io
      qemu-system-x86 qemu-utils cloud-image-utils apt-cacher-ng)
    "${install[@]}" || { apt-get update -q && "${install[@]}"; }
    log "qemu и прокси apt на раннере: $((SECONDS - up_t0)) с от начала up"
  fi
  if [[ -n "$fetch" ]]; then wait "$fetch" || die 'образ ВМ не скачан'; fi
  if [[ -n "$kfetch" ]] && ! wait "$kfetch"; then log 'ядро образа не скачано: загрузка через GRUB'; fi
  if [[ -n $from ]]; then
    [[ -f $from/entry.qcow2 ]] || die "в $from нет сохранённого выпуска"
    install -d -m 700 "$STAND_DIR/acme"
    install -m 600 "$from/root-password" "$from/id_ed25519" "$from/id_ed25519.pub" "$from/acme.pem" "$STAND_DIR/"
    install -m 600 "$from/acme.key" "$STAND_DIR/acme/"
  fi
  [[ -f "$STAND_DIR/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N '' -C vpn2-ci-stand -f "$STAND_DIR/id_ed25519"
  [[ -f "$STAND_DIR/root-password" ]] || (umask 077; openssl rand -hex 16 > "$STAND_DIR/root-password")
  [[ -f "$STAND_DIR/admin-password" ]] || (umask 077; openssl rand -hex 12 > "$STAND_DIR/admin-password")
  net_up
  apt_proxy_up
  acme_up
  local node name ip
  # Сеть ВМ в ядре хоста (vhost-net), а не в потоке qemu: меньше CPU раннера
  # на образ панели, пакеты и трафик 99-verify.
  modprobe vhost_net 2>/dev/null || true
  for node in "${NODES[@]}"; do
    read -r name _ <<<"$node"
    vm_boot "$node" "${from:+$from/$name.qcow2}" "$up_t0"
  done
  # Узлы ждём параллельно: последовательно второй замечали на ~3 с позже.
  local waits=()
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    vm_wait_ready "$name" "$ip" "$up_t0" & waits+=($!)
  done
  local p
  for p in "${waits[@]}"; do wait "$p" || die 'ВМ стенда не поднялись'; done
  # База слепка жила в /run выключенного узла.
  for node in "${NODES[@]}"; do
    [[ -n $from ]] || break
    read -r name ip _ <<<"$node"
    { snapshot_tool "$ip" && ssh_node "$ip" "cat > $SNAP_DIR/base.json" < "$from/base-$name.json"; } \
      || die "база слепка выпуска не вернулась на $name"
  done
  [[ -z $from ]] || release_base_tree
  host_stats 'ВМ готовы'
  entry_stats 'ВМ готовы'
}

# up фоном: ВМ грузятся, пока раннер собирает образ панели; up-wait ждёт
# конца и отдаёт вывод и код up.
cmd_up_bg() {
  mkdir -p "$STAND_DIR"
  chmod 700 "$STAND_DIR"
  : > "$STAND_DIR/up.rc"
  setsid nohup bash -c '"$0" up "${@:2}" > "$1/up.log" 2>&1; echo $? > "$1/up.rc"' "$0" "$STAND_DIR" "$@" \
    < /dev/null > /dev/null 2>&1 &
}

cmd_up_wait() {
  local deadline=$((SECONDS + 900))
  until [[ -s "$STAND_DIR/up.rc" ]]; do
    (( SECONDS < deadline )) || { cat "$STAND_DIR/up.log"; die 'up не кончился за 15 мин'; }
    sleep 1
  done
  cat "$STAND_DIR/up.log"
  return "$(cat "$STAND_DIR/up.rc")"
}

# Долгая команда на entry отвязана от ssh: обрыв сессии её не убивает.
# Вывод идёт в журнал на entry, отсюда его хвост печатается по мере роста.
entry_run() {
  local name=$1 command=$2 shown=0 lines rc
  ssh_node "$ENTRY" "rm -f /root/$name.rc; cd /opt/anysda-vpn2 || exit; setsid nohup bash -c '$command > /root/$name.log 2>&1; echo \$? > /root/$name.rc' >/dev/null 2>&1 < /dev/null &"
  while :; do
    lines=$(ssh_node "$ENTRY" "wc -l < /root/$name.log" 2>/dev/null || echo "$shown")
    if (( lines > shown )); then
      ssh_node "$ENTRY" "sed -n '$((shown + 1)),${lines}p' /root/$name.log" | stage_stats || true
      shown=$lines
    fi
    if rc=$(ssh_node "$ENTRY" "cat /root/$name.rc 2>/dev/null") && [[ -n "$rc" ]]; then
      ssh_node "$ENTRY" "sed -n '$((shown + 1)),\$p' /root/$name.log" | stage_stats || true
      return "$rc"
    fi
    sleep 1
  done
}

# Вывод стадий насквозь; после строк «[стадия → узел] done» - нагрузка
# хоста и entry за время с прошлой отметки.
stage_stats() {
  local line done=()
  while IFS= read -r line; do
    printf '%s\n' "$line"
    [[ $line =~ \[([^]]+)\]\ done ]] && done+=("${BASH_REMATCH[1]}")
  done
  (( ${#done[@]} )) || return 0
  local label
  label=$(printf '%s, ' "${done[@]}")
  host_stats "после ${label%, }"
  entry_stats "после ${label%, }"
}

# push_tree             - чистый каталог, исходники ветки (ls-files);
# push_tree --ref <тег>  - чистый каталог, исходники тега (git archive);
# push_tree --over <тег> - каталог остаётся, поверх файлы ветки, удалённые с
#                          тега пути стираются; config.yaml не трогается и не
#                          рендерится, неотслеживаемые infra/secrets и
#                          infra/state старого выпуска остаются (в потоке их нет).
push_tree() {
  local mode=${1:-} tag=${2:-} template=$REPO_ROOT/infra/tests/fixtures/config/etalon.yaml
  case "$mode" in
    '') log "исходники $(git -C "$REPO_ROOT" rev-parse --short HEAD) → entry:/opt/anysda-vpn2"
        ssh_node "$ENTRY" 'rm -rf /opt/anysda-vpn2 && mkdir -p /opt/anysda-vpn2'
        git -C "$REPO_ROOT" ls-files -z | tar -C "$REPO_ROOT" --null -T - -czf - | ssh_node "$ENTRY" 'tar -xzf - -C /opt/anysda-vpn2' ;;
    --ref) log "исходники $tag → entry:/opt/anysda-vpn2"
        ssh_node "$ENTRY" 'rm -rf /opt/anysda-vpn2 && mkdir -p /opt/anysda-vpn2'
        # umask 0022: права каталогов как у дерева ветки, иначе слепок считает их лишними
        git -C "$REPO_ROOT" -c tar.umask=0022 archive "${tag:?тег}" | ssh_node "$ENTRY" 'tar -xf - -C /opt/anysda-vpn2' ;;
    --over) log "исходники $(git -C "$REPO_ROOT" rev-parse --short HEAD) поверх $tag → entry:/opt/anysda-vpn2"
        # поток из ls-files: секретов и состояния в нём нет, они остаются на месте;
        # отслеживаемый infra/secrets/.gitignore ложится как в чистой установке
        git -C "$REPO_ROOT" ls-files -z | tar -C "$REPO_ROOT" --null -T - -czf - | ssh_node "$ENTRY" 'tar -xzf - -C /opt/anysda-vpn2 --exclude=config.yaml'
        # --no-renames: переименованный после тега файл считается удалённым, старое имя стирается
        # опустевший каталог уходит вместе с файлом, как при git pull: иначе слепок
        # видит каталог, которого в чистой установке нет
        git -C "$REPO_ROOT" diff -z --name-only --no-renames --diff-filter=D "${tag:?тег}" HEAD \
          | ssh_node "$ENTRY" 'cd /opt/anysda-vpn2 && xargs -0 -r sh -c '\''for f; do rm -f -- "$f"; d=$(dirname -- "$f"); [ "$d" = . ] || rmdir -p --ignore-fail-on-non-empty -- "$d" 2>/dev/null || true; done'\'' _'
        return 0 ;;
    *) die "push_tree: неизвестный режим $mode" ;;
  esac
  # Выпуск ставится своим шаблоном: config.yaml 1.2.0 - с паролем root.
  local rendered
  rendered=$( { if [[ $mode == --ref ]]; then git -C "$REPO_ROOT" show "$tag:infra/tests/fixtures/config/etalon.yaml"; else cat "$template"; fi; } \
    | sed -e "s|@ENTRY_HOST@|$ENTRY|g" -e "s|@EXIT_HOST@|$EXIT|g" -e "s|@EXIT_TAG@|$EXIT_TAG|g" \
      -e "s|@PASSWORD@|$(cat "$STAND_DIR/root-password")|g" -e "s|@ADMIN_KEY@|$(cat "$STAND_DIR/id_ed25519.pub")|g" \
      -e "s|@ADMIN_PASSWORD@|$(cat "$STAND_DIR/admin-password")|g" -e "s|@PANEL_IMAGE@|$PANEL_REF|g")
  if grep -q '@[A-Z_]*@' <<<"$rendered"; then
    die "в config.yaml стенда остались неподставленные метки (шаблон $template)"
  fi
  ssh_node "$ENTRY" "umask 077; cat > /opt/anysda-vpn2/config.yaml" <<<"$rendered"
  # Пароль панели деплой не хранит: стенд помнит свой (выпуск 1.2.0 держал его в /etc/anysda).
  [[ $mode == --ref ]] || ssh_node "$ENTRY" "umask 077; cat > $ADMIN_PASSWORD_FILE" < "$STAND_DIR/admin-password"
}

# Как человек по README: ./deploy.sh -key на entry, строка - в authorized_keys экзитов.
lay_key() {
  local key node name ip
  key=$(ssh_node "$ENTRY" '/opt/anysda-vpn2/deploy.sh -key') || die 'deploy.sh -key не отдал ключ'
  [[ $key == ssh-ed25519\ * ]] || die "deploy.sh -key: не ключ: $key"
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    [[ $name == entry ]] || ssh_node "$ip" "grep -qxF '$key' /root/.ssh/authorized_keys || echo '$key' >> /root/.ssh/authorized_keys" \
      || die "ключ оркестратора не лёг на $name"
  done
  log 'ключ оркестратора на экзитах'
}

# Вход по паролю закрыт везде: entry проверяет экзиты, экзит - entry. sshd без пароля
# в списке методов отвечает «Permission denied (publickey)».
password_refused() {
  local node name ip from said rc=0
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    from=$ENTRY; [[ $name != entry ]] || from=$EXIT
    said=$(ssh_node "$from" "ssh -o BatchMode=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 root@$ip true 2>&1; echo rc=\$?") || true
    if [[ $said == *'Permission denied (publickey).'* && $said == *rc=255* ]]; then
      log "вход по паролю на $name закрыт: ${said//$'\n'/ }"
    else
      log "✗ вход по паролю на $name не закрыт: $said"; rc=1
    fi
  done
  return "$rc"
}

# Docker на entry ставит 00-bootstrap, а образ панели нужен только к
# 35-telegram и 30-frontend: грузится, как только docker поднялся и образ
# <образ> есть на этой машине (stand.yml собирает его, пока идёт деплой).
load_panel() {
  local image=$1 deadline=$((SECONDS + 600))
  until docker tag "$image" "$PANEL_REF" 2>/dev/null && ssh_node "$ENTRY" 'systemctl is-active -q docker' 2>/dev/null \
      && docker save "$PANEL_REF" | gzip -1 | ssh_node "$ENTRY" 'gunzip | docker load' >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { log 'образ панели не загрузился на entry за 10 мин'; return 1; }
    sleep 2
  done
  log "образ панели $PANEL_REF → entry"
}

# Итог прогрева по узлам: время от загрузки ВМ (монотонные часы журнала),
# рядом - аптайм entry на старте деплоя.
warm_report() {
  local node name ip out slow=0
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    out=$(ssh_node "$ip" 'journalctl -u vpn2-ci-warm -o short-monotonic --no-pager -q' 2>/dev/null \
      | sed -n "s/^\[ *\([0-9.]*\)\].*\(прогрев: .*\)/[stand] ВМ $name, \1 с от загрузки: \2/p") || true
    [[ -z "$out" ]] || echo "$out"
    # Индекс apt дольше минуты: что ответил update узлу и что осталось в lists.
    (( $(sed -n 's/.*индекс apt.* за \([0-9]*\) с$/\1/p' <<<"$out" | head -1) + 0 > 60 )) || continue
    slow=1
    ssh_node "$ip" 'echo "списков Packages: $(ls /var/lib/apt/lists | grep -c _Packages)"
      tail -n 40 /var/log/vpn2-ci-warm-update.log' 2>/dev/null \
      | sed "s/^/[stand] ВМ $name, update прогрева: /" || true
  done
  (( slow )) || return 0
  # Запросы индекса через прокси: секунды от первой записи, I - с зеркала,
  # O - узлу; размер, клиент, путь (хэш by-hash укорочен).
  awk -F'|' 'NR == 1 {t0 = $1}
    $5 ~ /\/dists\// || ($2 != "I" && $2 != "O") {u = $5; sub(/.*\/dists\//, "", u)
      if (u ~ /by-hash/) u = substr(u, 1, length(u) - 52)
      printf "[stand] прокси apt +%d с %s %s %s %s\n", $1 - t0, $2, $3, $4, u}' \
    /var/log/apt-cacher-ng/apt-cacher.log 2>/dev/null || true
  tail -n 20 /var/log/apt-cacher-ng/apt-cacher.err 2>/dev/null | sed 's/^/[stand] прокси apt, ошибки: /' || true
}

entry_uptime() { ssh_node "$ENTRY" 'cut -d" " -f1 /proc/uptime' 2>/dev/null || echo '?'; }

# /run - tmpfs вне корней слепка: инструмент и файлы слепка сами в него не
# попадают. Инструмент - один файл на stdlib, на узел кладётся как есть.
SNAP_DIR=/run/vpn2-snapshot

snapshot_tool() {  # <адрес узла>
  ssh_node "$1" "mkdir -p $SNAP_DIR && cat > $SNAP_DIR/snapshot.py" < "$REPO_ROOT/infra/tests/snapshot.py"
}

snapshot_run() {  # <адрес узла> <аргументы snapshot...>
  local out
  out=$(ssh_node "$1" "python3 -B $SNAP_DIR/snapshot.py ${*:2}" 2>&1) \
    || die "слепок на $1: код $?, хвост вывода:
$(tail -n 20 <<<"$out")"
}

# База - чистая ОС плюс исходники в /opt/anysda-vpn2 (после push_tree): в
# итоговом слепке остаётся только сделанное установкой.
snapshot_base() {
  local node name ip started=$SECONDS p waits=()
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    { snapshot_tool "$ip" && snapshot_run "$ip" "--out $SNAP_DIR/base.json"; } & waits+=($!)
  done
  for p in "${waits[@]}"; do wait "$p" || die 'база слепка не снята'; done
  log "база слепка обоих узлов снята за $((SECONDS - started)) с"
}

# Строк нет нигде на entry: ни в файлах (кроме журналов и файлов самого стенда: /root/*.log, /root/*.rc,
# фикстуры infra/tests клона), ни в журнале systemd. /proc, /sys, /dev не читаются.
secrets_absent() {  # <что проверяется> <строка>...
  local what=$1 found
  shift
  found=$(printf '%s\n' "$@" | ssh_node "$ENTRY" 'umask 077; cat > /run/vpn2-needles
    { grep -rlsF -D skip --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev -f /run/vpn2-needles / \
        | grep -vE "^(/root/[^/]*\.(log|rc)|/run/vpn2-needles|/opt/anysda-vpn2/infra/tests/.*)$"
      journalctl -o cat --no-pager -q | grep -qF -f /run/vpn2-needles && echo "журнал systemd"; } || true
    rm -f /run/vpn2-needles') || die "$what: диск entry не прочитан"
  [[ -z $found ]] || die "$what: секрет лежит на entry: ${found//$'\n'/, }"
  log "$what: на диске entry и в журнале systemd секрета нет ($# строк)"
}

# Ключ бэкапов показан ровно один раз в выводе прогона <имя> (как его видит человек): ключ - в
# $STAND_DIR/backup-key стенда, на entry его нет нигде.
backup_key_shown() {  # <имя прогона entry_run>
  local key
  key=$(ssh_node "$ENTRY" "grep -o 'AGE-SECRET-KEY-1[0-9A-Z]*' /root/$1.log | sort -u") || die "$1: ключ бэкапов не показан"
  [[ -n $key && $key != *$'\n'* ]] || die "$1: ключей бэкапов в выводе не один"
  [[ $(ssh_node "$ENTRY" "grep -c 'Ключ бэкапов - показан один раз' /root/$1.log") == 1 ]] || die "$1: ключ показан не один раз"
  ssh_node "$ENTRY" "test -s /etc/anysda/backup.pub && test ! -e /etc/anysda/backup.pub.unseen" \
    || die "$1: открытого ключа бэкапов нет или пара не отмечена показанной"
  ( umask 077; printf '%s\n' "$key" > "$STAND_DIR/backup-key" )
  secrets_absent "$1: закрытый ключ бэкапов" "$key"
}

# Опись состояния entry для отказов restore: данные и ключи, клиенты в базе, наши службы и контейнеры.
# entry_fingerprint <имя> - опись в $STAND_DIR/fp-<имя>; same_fingerprint <до> <после> - равны, иначе разница в лог.
same_fingerprint() {
  diff "$STAND_DIR/fp-$1" "$STAND_DIR/fp-$2" | head -20 | sed 's/^/    /' >&2
  [[ ${PIPESTATUS[0]} == 0 ]]
}

entry_fingerprint() {
  ssh_node "$ENTRY" 'set -o pipefail
    { find /etc/anysda /etc/wireguard /etc/openvpn /etc/swanctl /var/lib/vpn2-secrets /var/lib/caddy /var/lib/anysda-vpn2 \
        /opt/anysda-vpn2/config.yaml -xdev -type f ! -name "db.sqlite*" -print0 2>/dev/null | sort -z | xargs -0 sha256sum
      sqlite3 -readonly /var/lib/anysda-vpn2/db.sqlite "SELECT id, name FROM clients ORDER BY id; SELECT id, client_id, name FROM devices ORDER BY id"
      systemctl list-units --type=service,timer --state=active --no-legend --plain | awk "{print \$1}" \
        | grep -E "^(anysda|vpn2|sing-box|openvpn|strongswan|wg-quick|caddy|docker)" | sort
      docker ps --format "{{.Names}} {{.ID}}" | sort; }' > "$STAND_DIR/fp-$1"
}

cmd_deploy() {
  local image=${1:?образ панели (docker-образ на этой машине)}
  host_stats 'до деплоя'
  entry_stats 'до деплоя'
  apt_proxy_up --if-down
  log "деплой начат на $(entry_uptime) с от загрузки entry"
  push_tree
  snapshot_base
  lay_key
  load_panel "$image" &
  local loader=$! started=$SECONDS rc=0
  entry_run deploy './deploy.sh --development -yaml' || rc=$?
  wait "$loader" || (( rc )) || rc=1
  (( rc )) || password_refused || rc=1
  (( rc )) || backup_key_shown deploy
  log "деплой: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  warm_report
  host_stats 'после деплоя'
  return "$rc"
}

# Сценарий 4, шаг 1: выпуск <тег> ставится по манифесту (образы по digest).
cmd_deploy_release() {
  local tag=${1:?тег выпуска} manifest=${2:?файл манифеста выпуска} started=$SECONDS rc=0
  [[ -f $manifest ]] || die "нет файла манифеста $manifest"
  host_stats 'до деплоя'
  apt_proxy_up --if-down
  reboot_nodes
  push_tree
  snapshot_base
  push_tree --ref "$tag"
  ssh_node "$ENTRY" 'cat > /root/release-manifest.json' < "$manifest"
  entry_run release './deploy.sh --manifest /root/release-manifest.json' || rc=$?
  log "установка выпуска $tag: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  warm_report
  return "$rc"
}

# Сохранённый выпуск: узлы гасятся штатно, их диски, база слепка и секреты
# стенда, которыми выпуск ставился, уходят в RELEASE_DIR. Сеть, прокси и ACME
# стенда остаются: следом up --release.
cmd_release_save() {
  local node name ip started=$SECONDS
  rm -rf "$RELEASE_DIR"
  mkdir -p "$RELEASE_DIR"
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    ssh_node "$ip" "cat $SNAP_DIR/base.json" > "$RELEASE_DIR/base-$name.json" || die "база слепка $name не забрана"
    ssh_node "$ip" 'fstrim -a >/dev/null 2>&1; systemctl poweroff --no-block' || true
  done
  for node in "${NODES[@]}"; do
    read -r name _ <<<"$node"
    while kill -0 "$(cat "$STAND_DIR/$name.pid")" 2>/dev/null; do
      (( SECONDS - started < 120 )) || die "ВМ $name не остановилась за 2 мин"
      sleep 1
    done
    mv "$STAND_DIR/$name.qcow2" "$RELEASE_DIR/"
  done
  cp "$STAND_DIR/root-password" "$STAND_DIR/id_ed25519" "$STAND_DIR/id_ed25519.pub" "$STAND_DIR/acme.pem" \
    "$STAND_DIR/acme/acme.key" "$RELEASE_DIR/"
  # actions/cache сохраняет каталог от пользователя раннера.
  chmod -R a+rX "$RELEASE_DIR"
  log "выпуск сохранён за $((SECONDS - started)) с: $(du -sh "$RELEASE_DIR" | cut -f1)"
}

# Исходники в базе слепка выпуска - ветки, с которой кэш снимали. Как в прогоне
# без кэша, база несёт дерево этой ветки: оно снимается в отдельном корне и
# заменяет в базе /opt/anysda-vpn2, кроме config.yaml стенда.
release_base_tree() {
  local tree=$SNAP_DIR/tree
  ssh_node "$ENTRY" "rm -rf $tree && mkdir -p $tree/opt/anysda-vpn2"
  git -C "$REPO_ROOT" ls-files -z | tar -C "$REPO_ROOT" --null -T - -czf - \
    | ssh_node "$ENTRY" "tar -xzf - -C $tree/opt/anysda-vpn2"
  snapshot_run "$ENTRY" "--root $tree --out $SNAP_DIR/tree.json"
  ssh_node "$ENTRY" "python3 - $SNAP_DIR/base.json $SNAP_DIR/tree.json" <<'PY' || die 'база слепка: дерево ветки не подставлено'
import json, sys
base, tree = (json.load(open(path)) for path in sys.argv[1:])
top = '/opt/anysda-vpn2'
inside = lambda name: name == top or name.startswith(top + '/')
files = {k: v for k, v in base['files'].items() if not inside(k) or k == top + '/config.yaml'}
files.update({k: v for k, v in tree['files'].items() if inside(k) and k != top + '/config.yaml'})
base['files'] = files
json.dump(base, open(sys.argv[1], 'w'))
PY
}

# Узлы перезагружаются и ждутся: база слепка выпуска снимается с уже
# перезагруженной ОС - ВМ с дисков выпуска тоже грузятся не впервые.
reboot_nodes() {
  local node name ip waits=() p t0=$SECONDS
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    (
      boot=$(ssh_node "$ip" 'cat /proc/sys/kernel/random/boot_id')
      ssh_node "$ip" 'systemctl reboot --no-block' || true
      until now=$(ssh_node "$ip" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null) && [[ -n $now && $now != "$boot" ]]; do
        (( SECONDS - t0 < 300 )) || die "ВМ $name не перезагрузилась за 5 мин"
        sleep 1
      done
      ssh_node "$ip" 'systemctl is-system-running --wait >/dev/null' || true
    ) & waits+=($!)
  done
  for p in "${waits[@]}"; do wait "$p" || die 'ВМ стенда не перезагрузились'; done
  log 'узлы перезагружены'
}

# Ключ сохранённого выпуска: тег, образ и то, чем стенд его ставит - узлы,
# заготовка cloud-init, раскладка исходников, config.yaml и инструмент слепка.
release_key() {
  local tag=${1:?тег выпуска}
  printf '%s-%s-%s\n' "$tag" "$IMAGE_KEY" "$( {
    declare -p NODES NET
    declare -f seed warm_script push_tree snapshot_base snapshot_tool snapshot_run cmd_deploy_release acme_up reboot_nodes
    git -C "$REPO_ROOT" show "$tag:infra/tests/fixtures/config/etalon.yaml"
    cat "$REPO_ROOT/infra/tests/snapshot.py"
  } | sha256sum | cut -c1-16)"
}

# ssh-мультиплексор стадии живёт 60 с и держит унаследованный замок
# deploy.sh: следующий запуск без этого упал бы на flock -n.
close_entry_mux() {
  ssh_node "$ENTRY" 'for s in /tmp/anysda-ssh-0/*; do [ -S "$s" ] && ssh -o ControlPath="$s" -O exit mux; done' \
    >/dev/null 2>&1 || true
}

# Только то, на чём стоит обрыв UDP: без VPN-серверов, аварийки, панели и бота.
UDP_STAGES=('00-bootstrap all' '05-mgmt-mesh all' "10-foreign $EXIT_TAG" '20-ru-router ru'
            '21-failover-watchdog ru' '22-adguard ru')

cmd_deploy_udp() {
  host_stats 'до деплоя'
  apt_proxy_up --if-down
  log "деплой начат на $(entry_uptime) с от загрузки entry"
  push_tree
  lay_key
  ssh_node "$ENTRY" "install -d -m 0755 /etc/anysda && printf 'TCP_RETURN_S=%s\n' $TCP_RETURN_S > /etc/anysda/failover-watchdog.env"
  local stage started=$SECONDS
  for stage in "${UDP_STAGES[@]}"; do
    close_entry_mux
    entry_run "deploy-${stage%% *}" \
      "$DEPLOY_LOCK_WAIT && ./deploy.sh --development $stage" \
      || die "$stage: код $?"
  done
  log "стадии для обрыва UDP: $(( SECONDS - started )) с"
  warm_report
}

cmd_verify() {
  if [[ "${1:-}" == --stop-openvpn ]]; then
    # Отрицательный контроль: процесс OpenVPN остановлен, остальное живо.
    log 'отрицательный контроль: останавливаю OpenVPN на entry'
    ssh_node "$ENTRY" 'systemctl stop openvpn-server@server; systemctl is-active openvpn-server@server || true'
  fi
  local rc=0
  entry_run verify './deploy.sh --development 99-verify all' || rc=$?
  log "99-verify all: код $rc"
  return "$rc"
}

# ansible с entry достаёт все узлы стенда: строк SUCCESS не меньше, чем узлов.
cmd_ansible_ping() {
  local rc=0 ok want=${#NODES[@]}
  entry_run ansible-ping 'ANSIBLE_CONFIG=infra/ansible/ansible.cfg /opt/vpn2/venv/bin/ansible all -m ping -o' || rc=$?
  log "ansible all -m ping: код $rc"
  (( rc == 0 )) || return "$rc"
  ok=$(ssh_node "$ENTRY" "grep -c SUCCESS /root/ansible-ping.log" || true)
  [[ "$ok" =~ ^[0-9]+$ ]] || ok=0
  (( ok >= want )) || die "ansible ping: SUCCESS у $ok узлов из $want"
  log "ansible ping: SUCCESS у $ok узлов из $want"
}

# Сколько держать обрыв. Сторож проверяет каналы раз в INTERVAL=2 с, мёртвым
# канал считает после DEAD_AFTER промахов, sing-box перезапускает после
# RESTART_AFTER_CHECKS проверок: любой исход виден за ~20 с.
UDP_CUT_HOLD_S=30
UDP_CUT_MAX_GAP=15  # окно без ответа через экзит, с: на стенде из VPS 2,4 с

# Каналы с запасным TCP на entry: «селектор hy2|tcp» по строке, как их видит
# сторож (_transport_pairs в vpn2.watchdog.failover).
TRANSPORTS_PY=$(cat <<'EOF'
import json, urllib.request
env = {}
for line in open('/etc/anysda/failover-watchdog.env'):
    key, sep, value = line.strip().partition('=')
    if sep and not key.startswith('#'):
        env[key] = value.strip('"\'')
req = urllib.request.Request(env['CLASH_API'].rstrip('/') + '/proxies',
                             headers={'Authorization': 'Bearer ' + env['CLASH_SECRET']})
proxies = json.load(urllib.request.urlopen(req, timeout=5))['proxies']
for name, p in sorted(proxies.items()):
    members = p.get('all') or []
    if ((p.get('type') or '').lower() == 'selector' and len(members) == 2
            and members[0].startswith('transport-hy2-')
            and members[1] == 'transport-tcp-' + members[0][14:]):
        print(name, 'hy2' if p.get('now') == members[0] else 'tcp')
EOF
)

# Нога канала у живых загрузок через экзит: «начало нога» по строке, как в /connections.
LEGS_PY=$(cat <<'EOF'
import json, sys, urllib.request
env = {}
for line in open('/etc/anysda/failover-watchdog.env'):
    key, sep, value = line.strip().partition('=')
    if sep and not key.startswith('#'):
        env[key] = value.strip('"\'')
req = urllib.request.Request(env['CLASH_API'].rstrip('/') + '/connections',
                             headers={'Authorization': 'Bearer ' + env['CLASH_SECRET']})
for c in sorted(json.load(urllib.request.urlopen(req, timeout=5))['connections'] or [], key=lambda c: c['start']):
    if c['metadata'].get('host') == sys.argv[1]:
        print(c['start'], next((x for x in c['chains'] if x.startswith('transport-')), c['chains'][0]))
EOF
)
DOWNLOAD_HOST=speed.cloudflare.com
# Загрузка с запасного TCP идёт 500 КиБ/с и живёт дольше возврата канала на hy2
# (TCP_RETURN_S и подъём QUIC) с запасом 40 с: её нога видна в /connections.
DOWNLOAD_RATE=500k
DOWNLOAD_BYTES=$(( 512000 * (TCP_RETURN_S + 40) ))

# Запрос через экзит раз в секунду: время конца запроса и код ответа.
CURL_LOOP=$(cat <<'EOF'
while [[ ! -e /root/udp-cut.stop ]]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -x socks5h://127.0.0.1:7897 https://api.ipify.org) || true
  printf '%s %s\n' "$(date +%s.%N)" "$code"
  sleep 1
done
EOF
)

check() {  # <текст> <команда проверки...>; провал копится в rc вызывающего
  if "${@:2}"; then log "✓ $1"; else log "✗ $1"; rc=1; fi
}
transports() { ssh_node "$ENTRY" python3 - <<<"$TRANSPORTS_PY"; }
legs() { ssh_node "$ENTRY" "python3 - $DOWNLOAD_HOST" <<<"$LEGS_PY"; }
download() {  # <имя> <байт/с>: загрузка через экзит в фоне, итог «код размер rc» в /root/<имя>.out
  ssh_node "$ENTRY" "setsid nohup bash -c 'curl -s -o /dev/null --limit-rate $2 -w \"%{http_code} %{size_download}\" \
    -x socks5h://127.0.0.1:7897 \"https://$DOWNLOAD_HOST/__down?bytes=$DOWNLOAD_BYTES\" > /root/$1.out; \
    echo \" \$?\" >> /root/$1.out' > /dev/null 2>&1 < /dev/null &"
}
on_hy2() { local t; t=$(transports) && [[ -n "$t" ]] && ! grep -q ' tcp$' <<<"$t"; }
singbox_state() { ssh_node "$ENTRY" 'systemctl show -p NRestarts -p ActiveEnterTimestamp sing-box' | sort | paste -sd ' '; }

# Правила обрыва на экзите: только трафик с адресов entry, ssh с хоста не задет.
cut_rules() {
  local action=$1 v6 port
  ssh_node "$EXIT" "iptables -w $action INPUT -p udp -s $ENTRY -j DROP" || return
  for v6 in $UDP_CUT_V6; do
    ssh_node "$EXIT" "ip6tables -w $action INPUT -p udp -s $v6 -j DROP" || return
  done
  for port in $UDP_CUT_TCP_PORTS; do
    ssh_node "$EXIT" "iptables -w $action INPUT -p tcp -s $ENTRY --dport $port -j DROP" || return
  done
}

cmd_udp_cut() {
  UDP_CUT_TCP_PORTS=''
  if [[ "${1:-}" == --break-tcp-fallback ]]; then
    # Отрицательный контроль: запасной TCP тоже мёртв, сторожу остаётся
    # только перезапуск sing-box - проверки обязаны упасть.
    UDP_CUT_TCP_PORTS=$(ssh_node "$EXIT" "ss -Hltnp | awk '/\"sing-box\"/ {n = split(\$4, a, \":\"); print a[n]}' | sort -u | paste -sd ' '")
    UDP_CUT_TCP_PORTS=${UDP_CUT_TCP_PORTS:-4443}
    log "отрицательный контроль: режу и TCP sing-box экзита с entry (порты $UDP_CUT_TCP_PORTS)"
  fi
  UDP_CUT_V6=$(ssh_node "$ENTRY" "ip -6 -o addr show scope global | awk '{sub(\"/.*\", \"\", \$4); print \$4}' | paste -sd ' '")
  if [[ -n "$UDP_CUT_V6" ]]; then log "IPv6 entry под обрывом: $UDP_CUT_V6"
  else log 'у entry нет глобального IPv6: обрыв только по IPv4'; fi

  local tcp_return=$TCP_RETURN_S deadline
  # Значение у живого процесса: EnvironmentFile= перебивает Environment= юнита.
  ssh_node "$ENTRY" "tr '\\0' '\\n' < /proc/\$(systemctl show -p MainPID --value anysda-failover-watchdog)/environ | grep -qx 'TCP_RETURN_S=$tcp_return'" \
    || die "сторож без TCP_RETURN_S=$tcp_return стенда"
  deadline=$((SECONDS + 120))
  until on_hy2; do
    (( SECONDS < deadline )) || { transports || true; die 'до обрыва не все каналы на hy2'; }
    sleep 2
  done
  log "каналы до обрыва: $(transports | paste -sd ',' | sed 's/,/, /g')"
  local before after
  before=$(singbox_state)
  log "sing-box до: $before"

  ssh_node "$ENTRY" 'cat > /root/udp-cut-curl.sh' <<<"$CURL_LOOP"
  ssh_node "$ENTRY" 'rm -f /root/udp-cut.stop; setsid nohup bash /root/udp-cut-curl.sh > /root/udp-cut-curl.log 2>&1 < /dev/null &'
  sleep 5
  local t0 t1 t_end
  t0=$(ssh_node "$ENTRY" 'date +%s.%N')
  trap 'cut_rules -D 2>/dev/null || true' EXIT
  cut_rules -I
  log "обрыв UDP с entry на экзите: $(date -u +%T), держу ${UDP_CUT_HOLD_S} с"
  local rc=0 cut_at=$SECONDS verdict
  if [[ -z "$UDP_CUT_TCP_PORTS" ]]; then
    # Канал на запасном TCP при закрытом hy2: проверка verify обязана краснеть.
    until grep -q ' tcp$' <<<"$(transports)" || (( SECONDS - cut_at >= UDP_CUT_HOLD_S )); do sleep 2; done
    verdict=$(ssh_node "$ENTRY" "/opt/vpn2/venv/bin/python3 -c 'from vpn2.verify import stage; print(*stage.exit_transport(\"$EXIT_TAG\"))'") || true
    check "verify «$EXIT_TAG: транспорт hy2, не запасной TCP» при обрыве красная: $verdict" test "${verdict%% *}" = 1
    # Загрузка, начатая на запасном TCP, обязана пережить возврат канала на hy2.
    ssh_node "$ENTRY" 'rm -f /root/download-tcp.out /root/download-hy2.out'
    download download-tcp $DOWNLOAD_RATE
    log "загрузка $DOWNLOAD_BYTES байт через экзит на запасном TCP: $(date -u +%T)"
  fi
  sleep $(( SECONDS - cut_at < UDP_CUT_HOLD_S ? UDP_CUT_HOLD_S - SECONDS + cut_at : 0 ))
  cut_rules -D
  trap - EXIT
  t1=$(ssh_node "$ENTRY" 'date +%s.%N')
  log "обрыв снят: $(date -u +%T), жду возврата на hy2 (TCP_RETURN_S=$tcp_return)"
  local back=''
  deadline=$((SECONDS + ${tcp_return%.*} + 60))
  while (( SECONDS < deadline )); do
    if on_hy2; then
      back=$(ssh_node "$ENTRY" 'date +%s.%N')
      break
    fi
    sleep 2
  done
  local old_leg='' new_leg='' download_out=''
  if [[ -z "$UDP_CUT_TCP_PORTS" && -n "$back" ]]; then
    # Новое соединение после возврата - по hy2, живое с обрыва - на TCP.
    download download-hy2 200k
    sleep 5
    local listing
    listing=$(legs) || true
    log "загрузки через экзит после возврата: $(paste -sd ',' <<<"$listing" | sed 's/,/, /g')"
    old_leg=$(awk 'NR == 1 {print $2}' <<<"$listing")
    new_leg=$(awk 'END {if (NR > 1) print $2}' <<<"$listing")
    deadline=$((SECONDS + 240))
    until download_out=$(ssh_node "$ENTRY" 'cat /root/download-tcp.out 2>/dev/null') && [[ $download_out == *' '*' '* ]] \
        || (( SECONDS >= deadline )); do sleep 2; done
    ssh_node "$ENTRY" "pkill -f '[_]_down' || true"  # [_]: не совпасть со своей командой
  fi
  sleep 3
  ssh_node "$ENTRY" 'touch /root/udp-cut.stop'
  t_end=$(ssh_node "$ENTRY" 'date +%s.%N')
  after=$(singbox_state)

  local curl_log journal gap fails
  curl_log=$(ssh_node "$ENTRY" 'cat /root/udp-cut-curl.log')
  # Самое долгое время между ответами 200 от начала обрыва до конца замера.
  gap=$(awk -v t0="$t0" -v end="$t_end" '
    $2 == 200 { if ($1 < t0) { prev = $1; next } if (prev == "") prev = t0
                if ($1 - prev > max) max = $1 - prev; prev = $1 }
    END { if (prev == "") prev = t0; if (end - prev > max) max = end - prev; printf "%.1f", max }' <<<"$curl_log")
  fails=$(awk -v t0="$t0" '$1 >= t0 && $2 != 200' <<<"$curl_log" | wc -l)
  journal=$(ssh_node "$ENTRY" "journalctl -u anysda-failover-watchdog --since @${t0%.*} -o short-precise --no-pager")
  log 'журнал сторожа с начала обрыва:'
  grep -E ' -> |UDP до экзита|штрафной|foreign-best|мертвы|перезапуск' <<<"$journal" | sed 's/^/    /' || true

  local n back_s
  check "окно без ответа через экзит ${gap} с (предел ${UDP_CUT_MAX_GAP} с), ошибок curl с начала обрыва: $fails" \
    awk -v g="$gap" -v m="$UDP_CUT_MAX_GAP" 'BEGIN { exit !(g <= m) }'
  check "sing-box не перезапускался: до «$before», после «$after»" test "$after" = "$before"
  n=$(grep -c "UDP до экзита $EXIT_TAG недоступен" <<<"$journal" || true)
  check "событие «UDP до экзита $EXIT_TAG недоступен»: $n (ждём 1)" test "$n" = 1
  n=$(grep -c "UDP до экзита $EXIT_TAG снова работает" <<<"$journal" || true)
  check "событие «UDP до экзита $EXIT_TAG снова работает»: $n (ждём 1)" test "$n" = 1
  n=$(grep -c 'в штрафной' <<<"$journal" || true)
  check "«в штрафной» в журнале: $n (ждём 0)" test "$n" = 0
  n=$(grep -c 'foreign-best' <<<"$journal" || true)
  check "«foreign-best» в журнале: $n (ждём 0)" test "$n" = 0
  # Вернулись и остались: после перезапуска sing-box каналы на миг на hy2,
  # а сторож тут же уводит их на TCP.
  local final
  final=$(transports | paste -sd ',' | sed 's/,/, /g') || true
  back_s=$([[ -z "$back" ]] || awk -v a="$t1" -v b="$back" 'BEGIN { printf "%.1f", b - a }')
  check "каналы на hy2 после снятия: ${back_s:-нет за $(( ${tcp_return%.*} + 60 ))} с, в конце: $final" \
    test -n "$back" -a -n "$final" -a "${final/tcp/}" = "$final"
  if [[ -z "$UDP_CUT_TCP_PORTS" ]]; then
    check "загрузка с запасного TCP дошла после возврата на hy2: «${download_out:-нет итога}» (ждём 200 $DOWNLOAD_BYTES 0)" \
      test "$download_out" = "200 $DOWNLOAD_BYTES 0"
    check "живая загрузка на ноге transport-tcp-$EXIT_TAG-*: «$old_leg»" test "${old_leg#transport-tcp-"$EXIT_TAG"-}" != "$old_leg"
    check "новая загрузка после возврата на ноге transport-hy2-$EXIT_TAG-*: «$new_leg»" \
      test "${new_leg#transport-hy2-"$EXIT_TAG"-}" != "$new_leg"
  fi
  log "обрыв UDP: код $rc"
  return "$rc"
}

cmd_snapshot() {
  local out=${1:?каталог для слепков} prefix=${2:-snapshot} node name ip started=$SECONDS
  mkdir -p "$out"
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    snapshot_run "$ip" "--baseline $SNAP_DIR/base.json --out $SNAP_DIR/now.json"
    ssh_node "$ip" "cat $SNAP_DIR/now.json" > "$out/$prefix-$name.json" \
      || die "слепок $name не забран с узла"
  done
  log "слепки в $out за $((SECONDS - started)) с"
}

# Сценарий 2: тот же ./deploy.sh второй раз; слепок после него сверяется
# с первым, база не переснимается.
cmd_deploy_again() {
  local out=${1:?каталог для слепков} started=$SECONDS rc=0
  close_entry_mux
  ssh_node "$ENTRY" "$DEPLOY_LOCK_WAIT" \
    || die 'замок деплоя на entry не освободился за 70 с'
  local public
  public=$(ssh_node "$ENTRY" 'cat /etc/anysda/backup.pub') || die 'повторный деплой: открытого ключа бэкапов нет'
  entry_run deploy-again './deploy.sh --development' || rc=$?
  log "повторный деплой: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  (( rc == 0 )) || return "$rc"
  ssh_node "$ENTRY" "! grep -qE 'AGE-SECRET-KEY-1|Ключ бэкапов - показан' /root/deploy-again.log" \
    || die 'повторный деплой показал ключ бэкапов'
  [[ $(ssh_node "$ENTRY" 'cat /etc/anysda/backup.pub') == "$public" ]] || die 'повторный деплой сменил пару ключей бэкапов'
  ssh_node "$ENTRY" "grep -qF 'ansible: пины те же' /opt/vpn2/ansible-venv.log" \
    || die 'повторный деплой переустановил venv с ansible'
  local changed
  changed=$(ssh_node "$ENTRY" "sed 's/\x1b\[[0-9;]*m//g' /root/deploy-again.log" \
    | awk '/^TASK \[/ { task = $0; sub(/ \*+$/, "", task) } /^changed: / && task { print task; task = "" }' | paste -sd ';')
  [[ -z "$changed" ]] || die "повторный деплой без правок что-то изменил: $changed"
  cmd_snapshot "$out" snapshot-again
}

# Сценарий 4, шаг 2: поверх выпуска <тег> - дерево ветки, ./deploy.sh --development.
cmd_deploy_upgrade() {
  local image=${1:?образ панели (docker-образ на этой машине)} tag=${2:?тег выпуска} started=$SECONDS rc=0
  close_entry_mux
  ssh_node "$ENTRY" "$DEPLOY_LOCK_WAIT" \
    || die 'замок деплоя на entry не освободился за 70 с'
  local mgmt left
  # Адреса - строки «тег октет»; шапку-комментарий деплой пишет свою.
  mgmt=$(ssh_node "$ENTRY" "grep -v '^#' $STATE/mgmt-ips.txt 2>/dev/null || grep -v '^#' $OLD_STATE/mgmt-ips.txt") \
    || die 'обновление: адреса mesh выпуска не прочитаны'
  ssh_node "$ENTRY" "install -m 600 /etc/anysda/admin-password.txt $ADMIN_PASSWORD_FILE" \
    || die 'обновление: пароля панели выпуска нет'
  push_tree --over "$tag"
  load_panel "$image" || return 1
  entry_run deploy './deploy.sh --development' || rc=$?
  log "обновление с $tag: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  (( rc == 0 )) || return "$rc"
  password_refused || die 'обновление: вход по паролю не закрыт'
  # Пароль архивов 1.2.0 (stand-etalon из config.yaml выпуска) показан рядом с новым ключом и ушёл с сервера.
  [[ $(ssh_node "$ENTRY" "grep -c 'Пароль архивов, сделанных до перехода на ключ: stand-etalon' /root/deploy.log") == 1 ]] \
    || die 'обновление: пароль архивов 1.2.0 не показан один раз'
  ssh_node "$ENTRY" 'test ! -e /var/lib/vpn2-secrets/backup_passphrase.cred && ! grep -q PASSPHRASE /etc/anysda/backup.env' \
    || die 'обновление: пароль архивов 1.2.0 остался в хранилище или backup.env'
  backup_key_shown deploy
  secrets_absent 'обновление: пароль архивов 1.2.0' stand-etalon
  # config.yaml выпуска нёс пароли root: обновление их стёрло.
  ssh_node "$ENTRY" "! grep -nE '^[[:space:]]*(- )?password:' /opt/anysda-vpn2/config.yaml" || die 'обновление: пароли root остались в config.yaml'
  [[ $(ssh_node "$ENTRY" "grep -v '^#' $STATE/mgmt-ips.txt") == "$mgmt" ]] \
    || die 'обновление: адреса mesh после переноса состояния не те, что у выпуска'
  ssh_node "$ENTRY" "test -s $STATE/known_hosts" || die "обновление: нет $STATE/known_hosts"
  left=$(ssh_node "$ENTRY" "ls -d $OLD_STATE ${OLD_STATE%/state}/.known_hosts* 2>/dev/null") || true
  [[ -z $left ]] || die "обновление: состояние деплоя осталось в checkout: $left"
}

# Положительный контроль: правка файла продукта, которую сверка с эталоном
# обязана заметить. Службы не перезапускаются, daemon-reload тоже не нужен.
cmd_snapshot_corrupt() {
  local file=/etc/systemd/system/anysda-failover-watchdog.service
  log "положительный контроль слепка: дописываю строку в entry:$file"
  ssh_node "$ENTRY" "printf '# positive control\\n' >> $file"
}

# Запрос к панели на entry: вход один раз (cookie-jar /run/vpn2-panel.jar),
# пароль читается на самом entry и в журнал раннера не попадает.
# Использование: panel_api <метод> <путь> [json-тело]; печатает тело ответа.
panel_api() {
  local method=$1 path=$2 body=${3:-}
  local base=http://127.0.0.1:51821 jar=/run/vpn2-panel.jar
  # Origin со схемой панели: с сертификатом она ждёт https (session.cookie.secure).
  local hdr="-H \"Origin: \$o\" -H 'Content-Type: application/json'"
  local login="curl -fsS -c $jar $hdr -d \"\$(printf '{\"username\":\"anysda\",\"password\":\"%s\"}' \"\$(cat $ADMIN_PASSWORD_FILE 2>/dev/null || cat /etc/anysda/admin-password.txt)\")\" $base/api/auth/sign-in/username >/dev/null"
  local req="curl -fsS -b $jar -X $method $hdr ${body:+-d '$body'} $base$path"
  ssh_node "$ENTRY" "o=\$(sed -n 's/^true\$/https/p' /etc/vpn2/tls/panel/secure 2>/dev/null); o=\${o:-http}://127.0.0.1:51821
    [ -f $jar ] || { $login; }; $req"
}

# Сценарий 5: боту через панель - поддельный токен. Telegram отвечает
# InvalidToken, бот обязан уйти в паузу (_wait_token_fix), а не в цикл
# перезапусков, и всю паузу отвечать на /health признаком invalid_token.
cmd_bot_fake_token() {
  local fake='1234567890:stand-fake-token-not-real-000000000'
  local t0=$SECONDS waited rc_before rc_after running since health
  ssh_node "$ENTRY" 'curl -fsS http://127.0.0.1:8877/health' >/dev/null \
    || die "бот до смены токена не отвечает на /health"
  since=$(ssh_node "$ENTRY" 'date -u +%Y-%m-%dT%H:%M:%SZ')
  panel_api PUT /api/admin/telegram "{\"bot_token\": \"$fake\", \"chat_id\": \"1\"}" >/dev/null \
    || die "PUT /api/admin/telegram не прошёл"
  t0=$SECONDS
  until ssh_node "$ENTRY" "docker logs --since '$since' anysda-tgbot 2>&1 | grep -q InvalidToken"; do
    (( SECONDS - t0 < 120 )) || die "за 120 с в журнале бота нет InvalidToken: $(ssh_node "$ENTRY" "docker logs --since '$since' --tail 20 anysda-tgbot 2>&1")"
    sleep 3
  done
  waited=$((SECONDS - t0))
  rc_before=$(ssh_node "$ENTRY" "docker inspect -f '{{.RestartCount}}' anysda-tgbot")
  sleep 30
  running=$(ssh_node "$ENTRY" "docker inspect -f '{{.State.Running}}' anysda-tgbot")
  rc_after=$(ssh_node "$ENTRY" "docker inspect -f '{{.RestartCount}}' anysda-tgbot")
  [[ $running == true && $rc_after == "$rc_before" ]] \
    || die "бот не на паузе: Running=$running, RestartCount $rc_before -> $rc_after"
  health=$(ssh_node "$ENTRY" 'curl -fsS http://127.0.0.1:8877/health') \
    || die 'бот на паузе не отвечает на /health'
  [[ $health == *'"invalid_token": true'* ]] || die "бот на паузе: в /health нет invalid_token: $health"
  panel_api GET /api/admin/telegram >/dev/null || die "панель не отвечает после смены токена бота"
  log "бот с поддельным токеном: InvalidToken через $waited с, RestartCount $rc_after, контейнер работает, панель отвечает"
}

# Эталонный архив бэкапа (VPN2-751): на развёрнутом стенде создаётся известный
# набор данных, снимается anysda-backup.sh; архив и файл ожиданий кладутся в
# <каталог>. Токен ненастоящий; архив зашифрован открытой фразой стенда и несёт
# данные одноразового стенда (в том числе хэш пароля панели), настоящих секретов в нём нет.
cmd_backup_fixture() {
  local out=${1:?каталог для эталона} name json id dir newest ret
  local token='1234567890:stand-etalon-archive-token-0000000'
  mkdir -p "$out"
  for name in etalon-1 etalon-2; do
    json=$(panel_api POST /api/clients "{\"name\": \"$name\"}") || die "POST /api/clients ($name) не прошёл"
    id=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' <<<"$json") \
      || die "в ответе на создание клиента $name нет id"
    panel_api POST "/api/clients/$id/devices" '{"name": "dev-1"}' >/dev/null \
      || die "POST /api/clients/$id/devices ($name) не прошёл"
  done
  panel_api PUT /api/admin/telegram "{\"bot_token\": \"$token\", \"chat_id\": \"2\"}" >/dev/null \
    || die "PUT /api/admin/telegram не прошёл"

  ret=0
  json=$(ssh_node "$ENTRY" '/usr/local/bin/anysda-backup.sh 2>&1') || ret=$?
  (( ret == 0 )) || die "anysda-backup.sh завершился с кодом $ret: $(tail -n 20 <<<"$json")"
  dir=/var/backups/anysda-vpn2
  newest=$(ssh_node "$ENTRY" "ls -1t $dir/*.tar.gz.age | head -n 1") || die "в $dir нет архива"
  ssh_node "$ENTRY" "cat '$newest'" > "$out/etalon.tar.gz.age" || die "архив $newest не скачан"
  [[ -s $out/etalon.tar.gz.age ]] || die "скачанный архив $newest пуст"

  local peers names sums
  peers=$(ssh_node "$ENTRY" 'wg show wg0 peers | sort') || die "wg show wg0 peers не прошёл"
  names=$(panel_api GET /api/clients | python3 -c 'import json, sys; print("\n".join(sorted(c["name"] for c in json.load(sys.stdin))))') \
    || die "GET /api/clients не разобран"
  # Сертификаты *.crt (закрытых ключей нет) и runtime-файл бота; на стенде это
  # ca.crt, server.crt, anysda-server.crt, anysda-ca.crt.
  sums=$(ssh_node "$ENTRY" "find /etc/openvpn/server/pki /etc/swanctl/x509 /etc/swanctl/x509ca -maxdepth 1 -type f -name '*.crt' -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum
    sha256sum /var/lib/anysda-vpn2/runtime/telegram-runtime.json") || die "sha256 файлов эталона не собраны"
  (( $(grep -c . <<<"$sums") >= 5 )) || die "в files меньше 5 записей: $(grep -c . <<<"$sums")"
  PEERS=$peers NAMES=$names SUMS=$sums python3 - "$out/etalon.expect.json" <<'PY' || die "etalon.expect.json не собран"
import json, os, sys
files = {}
for line in os.environ['SUMS'].splitlines():
    digest, path = line.split(None, 1)
    files[path.strip()] = digest
expect = {
    'wg_peers': sorted(os.environ['PEERS'].split()),
    'clients': sorted(os.environ['NAMES'].split('\n')),
    'files': files,
}
with open(sys.argv[1], 'w') as f:
    json.dump(expect, f, sort_keys=True, indent=1)
    f.write('\n')
PY
  python3 - "$out/etalon.expect.json" <<'PY' || die "проверка etalon.expect.json не прошла"
import json, sys
e = json.load(open(sys.argv[1]))
assert len(e['wg_peers']) >= 2, 'wg_peers меньше 2'
assert {'etalon-1', 'etalon-2'} <= set(e['clients']), 'нет клиентов etalon-1/etalon-2'
assert e['files'], 'files пуст'
print('[stand] etalon.expect.json: wg_peers %d, clients %d, files %d' % (len(e['wg_peers']), len(e['clients']), len(e['files'])))
PY
  log "эталонный архив: $(stat -c %s "$out/etalon.tar.gz.age") байт, $newest"
}

# Сверка восстановленного узла с etalon.expect.json. Аргументы: вывод
# `wg show wg0 peers`, вывод sha256sum путей из files, тело GET /api/clients,
# путь expect. Несовпадения - строками `restore: <что> <ожидалось> <получено>`
# в stderr; код возврата - их число (не больше 255); при 0 в stdout итог.
restore_compare() {
  PEERS=$1 SUMS=$2 CLIENTS=$3 python3 - "$4" <<'PY'
import json, os, sys
e = json.load(open(sys.argv[1]))
bad = 0
def miss(what, want, got):
    global bad
    bad += 1
    print('restore: %s %s %s' % (what, want, got), file=sys.stderr)
peers = set(os.environ['PEERS'].split())
for key in e['wg_peers']:
    if key not in peers:
        miss('wg-пир', key, 'нет')
got = {}
for line in os.environ['SUMS'].splitlines():
    parts = line.split(None, 1)
    if len(parts) == 2:
        got[parts[1].strip()] = parts[0]
for path, digest in sorted(e['files'].items()):
    if got.get(path) != digest:
        miss('sha256 ' + path, digest, got.get(path, 'нет'))
try:
    names = {c['name'] for c in json.loads(os.environ['CLIENTS'])}
except (ValueError, TypeError, KeyError):
    names = set()
    miss('ответ /api/clients', 'список клиентов', 'не разобран')
for name in e['clients']:
    if name not in names:
        miss('клиент', name, 'нет')
if bad:
    sys.exit(min(bad, 255))
print('ключей %d, файлов %d, клиентов %d' % (len(e['wg_peers']), len(e['files']), len(e['clients'])))
PY
}

# Сценарий 3 карточки: restore эталонного архива на развёрнутый стенд.
# С --foreign-pass пароль архива в backup.env заменяется чужим: restore обязан
# упасть кодом 4 до любых изменений, шаг возвращает этот код.
# ./deploy.sh: --development первым словом (deploy.sh:14), restore вторым
# (infra/deploy.sh:1721).
cmd_restore_check() {
  local foreign=0 fx=$REPO_ROOT/infra/tests/fixtures/backup started=$SECONDS rc=0
  [[ ${1:-} != --foreign-pass ]] || foreign=1
  local archive=$fx/etalon.tar.gz.age expect=$fx/etalon.expect.json
  [[ -f $archive ]] || die "фикстуры нет: $archive"
  [[ -f $expect ]] || die "фикстуры нет: $expect"
  ssh_node "$ENTRY" 'cat > /root/etalon.tar.gz.age' < "$archive" || die "архив не загружен на entry"
  # Архив 1.2.0 зашифрован паролем: restore спрашивает его, без терминала читает из stdin.
  local password=stand-etalon
  if (( foreign )); then
    password=stand-foreign
    entry_fingerprint foreign-before || die 'опись entry не снята'
    log 'отрицательный контроль: чужой пароль архива'
  fi
  printf '%s\n' "$password" | ssh_node "$ENTRY" 'umask 077; cat > /run/vpn2-restore.pass' || die 'пароль архива не лёг на entry'
  close_entry_mux
  ssh_node "$ENTRY" "$DEPLOY_LOCK_WAIT" \
    || die 'замок деплоя на entry не освободился за 70 с'
  local mgmt
  mgmt=$(ssh_node "$ENTRY" "sha256sum $STATE/mgmt-ips.txt") || die "restore: нет $STATE/mgmt-ips.txt"
  # Пароль панели из архива: база восстановится с его хэшем, а файл с паролем restore не оставляет.
  (( foreign )) || ssh_node "$ENTRY" "PYTHONPATH=/opt/anysda-vpn2/infra/vpn2/src python3 - > $ADMIN_PASSWORD_FILE.etalon" <<'PY' \
    || die 'restore: пароль панели из архива не прочитан'
import sys, tarfile, tempfile
from vpn2.backup import archive
with tempfile.TemporaryDirectory() as work:
    archive.decrypt('/root/etalon.tar.gz.age', f'{work}/a.tar.gz', 'stand-etalon', 'password')
    with tarfile.open(f'{work}/a.tar.gz') as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith('/etc-anysda/admin-password.txt'))
        sys.stdout.write(tar.extractfile(member).read().decode())
PY
  entry_run restore './deploy.sh --development restore /root/etalon.tar.gz.age --force < /run/vpn2-restore.pass' || rc=$?
  ssh_node "$ENTRY" 'rm -f /run/vpn2-restore.pass'
  log "restore: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  if (( foreign )); then
    entry_fingerprint foreign-after && same_fingerprint foreign-before foreign-after || die 'чужой пароль: restore изменил entry'
    ssh_node "$ENTRY" "grep -q 'неверный пароль или архив повреждён; ничего не изменено' /root/restore.log" \
      || die 'чужой пароль: отказ restore без понятной причины'
    log "чужой пароль: отказ кодом $rc, состояние entry не изменилось"
  fi
  (( rc == 0 )) || return "$rc"
  ssh_node "$ENTRY" "mv $ADMIN_PASSWORD_FILE.etalon $ADMIN_PASSWORD_FILE"
  # Токен бота из архива 1.2.0 - в хранилище entry, в runtime-файле его нет (sha256 в expect - файл без токена).
  [[ $(ssh_node "$ENTRY" 'cat /run/vpn2-secrets/root/telegram_bot_token') == 1234567890:stand-etalon-archive-token-0000000 ]] \
    || die 'restore: токен бота из архива не в хранилище entry'

  # Эталонный архив снят до переезда состояния деплоя: restore оставляет текущее.
  [[ $(ssh_node "$ENTRY" "sha256sum $STATE/mgmt-ips.txt") == "$mgmt" ]] \
    || die 'restore: адреса mesh в состоянии деплоя изменились'
  # Сессии панели заменены вместе с базой: старый cookie-jar не годится.
  ssh_node "$ENTRY" 'rm -f /run/vpn2-panel.jar'
  local paths peers sums clients t0=$SECONDS n=0 summary
  paths=$(python3 -c 'import json, sys; print("\n".join(sorted(json.load(open(sys.argv[1]))["files"])))' "$expect") \
    || die "restore: files не прочитаны из $expect"
  peers=$(ssh_node "$ENTRY" 'wg show wg0 peers') || die "restore: wg show wg0 peers не прошёл"
  # shellcheck disable=SC2046
  sums=$(ssh_node "$ENTRY" "sha256sum -- $(printf '%q ' $paths)" 2>/dev/null) || true
  until clients=$(panel_api GET /api/clients 2>/dev/null); do
    (( SECONDS - t0 < 90 )) || { clients=''; break; }
    ssh_node "$ENTRY" 'rm -f /run/vpn2-panel.jar'
    sleep 3
  done
  summary=$(restore_compare "$peers" "$sums" "$clients" "$expect") || n=$?
  (( n == 0 )) || die "restore: несовпадений $n"
  log "restore: совпало, $summary"
}

# Потеря entry: архив живой entry, вместо неё ВМ с чистого образа (свои ключи хоста и systemd-creds),
# ./deploy.sh restore <архив> без строки ./deploy.sh -key на экзите. Экзит не трогается: его authorized_keys
# до и после совпадают, новая entry входит на него только ключом из архива.
cmd_recover_fresh() {
  local archive key mgmt authorized host loader pid rc=0 started=$SECONDS
  local mgmt_pub="ssh-keygen -y -f /run/vpn2-secrets/root/mgmt_key | cut -d' ' -f1,2"
  entry_run backup './deploy.sh --development backup' || die 'recover: бэкап живой entry не снят'
  archive=$(ssh_node "$ENTRY" 'ls -t /var/backups/anysda-vpn2/anysda-vpn2-*.tar.gz.age | head -1') \
    || die 'recover: архива живой entry нет'
  ssh_node "$ENTRY" "cat $archive" > "$STAND_DIR/recover.tar.gz.age" || die 'recover: архив не скопирован с entry'
  [[ -s $STAND_DIR/backup-key ]] || die 'recover: ключа бэкапов от деплоя стенда нет'
  # Чужой ключ (пара другой установки): отказ кодом 4 до любых изменений.
  local public
  entry_fingerprint key-before && public=$(ssh_node "$ENTRY" 'cat /etc/anysda/backup.pub') || die 'recover: опись entry не снята'
  close_entry_mux
  ssh_node "$ENTRY" "$DEPLOY_LOCK_WAIT" || die 'замок деплоя на entry не освободился за 70 с'
  entry_run restore-foreign "age-keygen 2>/dev/null | ./deploy.sh --development restore $archive --force" || rc=$?
  (( rc == 4 )) || die "recover: restore с чужим ключом - код $rc, ждём 4"
  entry_fingerprint key-after && same_fingerprint key-before key-after || die 'recover: restore с чужим ключом изменил entry'
  ssh_node "$ENTRY" "grep -q 'неверный ключ бэкапов или архив повреждён; ничего не изменено' /root/restore-foreign.log" \
    || die 'recover: отказ на чужой ключ без понятной причины'
  log 'recover: чужой ключ - отказ кодом 4, состояние entry не изменилось'
  rc=0
  key=$(ssh_node "$ENTRY" '/opt/anysda-vpn2/deploy.sh -key 2>/dev/null') && mgmt=$(ssh_node "$ENTRY" "$mgmt_pub") \
    && authorized=$(ssh_node "$EXIT" 'sha256sum /root/.ssh/authorized_keys') \
    && host=$(ssh_node "$ENTRY" 'cat /etc/ssh/ssh_host_ed25519_key.pub') || die 'recover: ключи живой entry не прочитаны'
  # Свой pidfile qemu стирает при выходе: номер процесса читается один раз.
  pid=$(cat "$STAND_DIR/entry.pid") && kill "$pid" || die 'recover: ВМ entry не остановлена'
  while kill -0 "$pid" 2>/dev/null; do sleep 1; done
  vm_boot "${NODES[0]}"
  vm_wait_ready entry "$ENTRY" "$started"
  [[ $(ssh_node "$ENTRY" 'cat /etc/ssh/ssh_host_ed25519_key.pub') != "$host" ]] && ssh_node "$ENTRY" 'test ! -e /var/lib/vpn2-secrets' \
    || die 'recover: новая entry не чистая - ключ хоста или хранилище секретов прежние'
  push_tree
  ssh_node "$ENTRY" 'umask 077; cat > /root/recover.tar.gz.age' < "$STAND_DIR/recover.tar.gz.age" \
    || die 'recover: архив не лёг на новую entry'
  ssh_node "$ENTRY" 'umask 077; cat > /run/vpn2-restore.key' < "$STAND_DIR/backup-key" || die 'recover: ключ бэкапов не лёг на entry'
  load_panel "$PANEL_REF" & loader=$!
  entry_run recover './deploy.sh --development restore /root/recover.tar.gz.age < /run/vpn2-restore.key' || rc=$?
  wait "$loader" || (( rc )) || rc=1
  log "recover: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  (( rc == 0 )) || die "recover: ./deploy.sh restore на новой entry - код $rc"
  [[ $(ssh_node "$ENTRY" '/opt/anysda-vpn2/deploy.sh -key 2>/dev/null') == "$key" ]] \
    || die 'recover: ./deploy.sh -key новой entry - не ключ из архива'
  [[ $(ssh_node "$ENTRY" "$mgmt_pub") == "$mgmt" ]] || die 'recover: ключ mgmt новой entry - не из архива'
  [[ $(ssh_node "$EXIT" 'sha256sum /root/.ssh/authorized_keys') == "$authorized" ]] \
    || die 'recover: authorized_keys экзита изменились - ключ лёг не из архива'
  ssh_node "$ENTRY" "ssh -o BatchMode=yes $EXIT_TAG true" || die "recover: ssh $EXIT_TAG с новой entry не прошёл"
  log "recover: новая entry с ключами из архива ($key), ssh $EXIT_TAG проходит, экзит не тронут"
  # Ключ спрошен один раз, в самом начале; новой пары деплой не показал - действует пара из архива.
  [[ $(ssh_node "$ENTRY" "grep -c 'архив зашифрован ключом бэкапов' /root/recover.log") == 1 ]] \
    || die 'recover: ключ бэкапов спрошен не один раз'
  ssh_node "$ENTRY" "! grep -qE 'AGE-SECRET-KEY-1|Ключ бэкапов - показан' /root/recover.log && test ! -e /etc/anysda/backup.pub.unseen" \
    || die 'recover: новая entry показала новый ключ бэкапов'
  [[ $(ssh_node "$ENTRY" 'cat /etc/anysda/backup.pub') == "$public" ]] || die 'recover: открытый ключ бэкапов - не из архива'
  # Бэкап по таймеру - только открытым ключом; архив открывает закрытый ключ человека.
  ssh_node "$ENTRY" 'set -o pipefail; before=$(ls /var/backups/anysda-vpn2 | wc -l)
    systemctl start anysda-backup.service || exit 1
    new=$(ls -t /var/backups/anysda-vpn2/anysda-vpn2-*.tar.gz.age | head -1)
    (( $(ls /var/backups/anysda-vpn2 | wc -l) > before )) && age -d -i /run/vpn2-restore.key "$new" | tar tz > /dev/null' \
    || die 'recover: бэкап таймера не снят или не открывается ключом бэкапов'
  ssh_node "$ENTRY" 'rm -f /run/vpn2-restore.key'
  log 'recover: ключ спрошен один раз, пара из архива, бэкап таймера открывается ключом'
  secrets_absent 'recover: закрытый ключ бэкапов' "$(cat "$STAND_DIR/backup-key")"
}

# Состояние apt каждого узла и журнал прокси раннера - в отчёт при любом исходе.
collect_apt() {
  local out=$1 node name ip
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    ssh_node "$ip" 'echo "== update прогрева =="; cat /var/log/vpn2-ci-warm-update.log 2>&1
      echo "== lists =="; ls -l --time-style=+%H:%M:%S /var/lib/apt/lists /var/lib/apt/lists/partial 2>&1
      stat -c "%y %n" /var/lib/apt/lists 2>&1
      echo "== sources =="
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
        [ -f "$f" ] || continue
        echo "--- $f"; cat "$f"
      done
      echo "== apt.conf =="; grep -r . /etc/apt/apt.conf.d 2>&1 | grep -iE "Proxy|Acquire|Retries"
      echo "== policy =="; apt-cache policy wireguard fail2ban qrencode 2>&1
      echo "== процессы apt =="; ps -eo pid,etimes,stat,args 2>&1 | grep -E "[a]pt|[m]ethods/"
      echo "== partial =="; ls -l --time-style=+%T /var/lib/apt/lists/partial 2>&1
      echo "== журнал =="; journalctl -u vpn2-ci-warm --no-pager -q 2>&1
      true' > "$out/apt-$name.txt" 2>&1 || true
  done
  cp /var/log/apt-cacher-ng/apt-cacher.log "$out/apt-cacher.log" 2>/dev/null || true
  cp /var/log/apt-cacher-ng/apt-cacher.err "$out/apt-cacher.err" 2>/dev/null || true
}

cmd_collect() {
  local out=${1:?каталог для отчётов} host tag
  mkdir -p "$out"
  for host in "$ENTRY:ru" "$EXIT:$EXIT_TAG"; do
    tag=${host#*:}
    ssh_node "${host%%:*}" 'cat /var/lib/anysda/verify-report.json' > "$out/verify-report-$tag.json" 2>/dev/null \
      || rm -f "$out/verify-report-$tag.json"
  done
  local name
  for name in $(ssh_node "$ENTRY" 'cd /root && ls -- *.log 2>/dev/null | sed "s/\.log$//"' || true); do
    ssh_node "$ENTRY" "cat /root/$name.log" > "$out/$name.log" 2>/dev/null || rm -f "$out/$name.log"
  done
  cp "$STAND_DIR"/*-console.log "$out/" 2>/dev/null || true
  collect_apt "$out"
  # Пароль узлов стенда в журналах не печатается, но проверяем: артефакт публичный.
  if [[ -f "$STAND_DIR/root-password" ]] && grep -rqF -- "$(cat "$STAND_DIR/root-password")" "$out"; then
    die 'в собранных файлах найден пароль стенда'
  fi
  ls -l "$out"
}

cmd_down() {
  local node name
  for node in "${NODES[@]}"; do
    read -r name _ <<<"$node"
    if [[ -f "$STAND_DIR/$name.pid" ]]; then
      kill "$(cat "$STAND_DIR/$name.pid")" 2>/dev/null || true
    fi
    ip link del "$BRIDGE-$name" 2>/dev/null || true
  done
  ip link del "$BRIDGE" 2>/dev/null || true
  local wan
  wan=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
  iptables -w -t nat -D POSTROUTING -s "$NET.0/24" -o "$wan" -j MASQUERADE 2>/dev/null || true
  iptables -w -D FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || true
  iptables -w -D FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || true
  rm -rf "$STAND_DIR"
  # Кэш сохраняет actions/cache от пользователя раннера: прокси стоп, файлы на чтение.
  if [[ -d "$APT_CACHE" ]]; then
    systemctl stop apt-cacher-ng 2>/dev/null || true
    apt_proxy_stats
    chmod -R a+rX "$APT_CACHE"
  fi
}

case "${1:-}" in
  up) cmd_up "${@:2}" ;;
  up-bg) cmd_up_bg "${@:2}" ;;
  up-wait) cmd_up_wait ;;
  deploy) cmd_deploy "${@:2}" ;;
  deploy-release) cmd_deploy_release "${@:2}" ;;
  release-save) cmd_release_save ;;
  release-key) release_key "${@:2}" ;;
  deploy-upgrade) cmd_deploy_upgrade "${@:2}" ;;
  deploy-udp) cmd_deploy_udp ;;
  deploy-again) cmd_deploy_again "${@:2}" ;;
  ansible-ping) cmd_ansible_ping ;;
  verify) cmd_verify "${@:2}" ;;
  udp-cut) cmd_udp_cut "${@:2}" ;;
  snapshot) cmd_snapshot "${@:2}" ;;
  snapshot-corrupt) cmd_snapshot_corrupt ;;
  bot-fake-token) cmd_bot_fake_token ;;
  backup-fixture) cmd_backup_fixture "${@:2}" ;;
  restore-check) cmd_restore_check "${@:2}" ;;
  recover-fresh) cmd_recover_fresh ;;
  collect) cmd_collect "${@:2}" ;;
  down) cmd_down ;;
  image-key) echo "$IMAGE_KEY" ;;
  *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 64 ;;
esac
