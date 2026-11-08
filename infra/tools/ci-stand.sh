#!/usr/bin/env bash
# Стенд внутри одной машины с KVM: entry и экзит - ВМ из облачного образа
# Ubuntu 26.04 в своей сети за NAT хоста. Продукт ставится штатным
# ./deploy.sh с самого entry, как по README; 99-verify гоняет настоящий
# трафик WireGuard/OpenVPN/IKEv2.
#
#   sudo infra/tools/ci-stand.sh up                     поднять ВМ
#   sudo infra/tools/ci-stand.sh up-bg | up-wait        то же фоном и ожидание
#   sudo infra/tools/ci-stand.sh deploy <образ панели>  полный деплой
#   sudo infra/tools/ci-stand.sh deploy-udp             стадии до 22-adguard
#   sudo infra/tools/ci-stand.sh verify [--stop-openvpn]
#   sudo infra/tools/ci-stand.sh udp-cut [--break-tcp-fallback]
#   sudo infra/tools/ci-stand.sh collect <каталог>      отчёты и журналы
#   sudo infra/tools/ci-stand.sh down                   снести всё
#
# Образ панели - локальный docker-образ, собранный из этого же дерева; на
# entry он загружается, как только 00-bootstrap поднял docker, deploy берёт его по panel.image.
# verify возвращает код 99-verify: 0 работает, 1 ошибка, 2 не проверено.
# deploy-udp ставит только то, на чём стоит обрыв UDP: экзит, sing-box entry,
# сторож и AdGuard (DNS sing-box идёт через него).
# udp-cut режет UDP с entry на экзите и проверяет, что трафик ушёл на запасной
# TCP без перезапуска sing-box и вернулся на Hysteria2: 0 прошло, 1 нет.
set -Eeuo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STAND_DIR=${STAND_DIR:-/var/tmp/vpn2-ci-stand}
IMAGE_URL=${STAND_IMAGE_URL:-https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img}
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
# Ожидание выпуска сертификата впустую: на адрес 198.18.x ACME не выпустит.
# Стадии берут его из окружения ssh-сессии (pam_env, /etc/environment).
TLS_INITIAL_WAIT=0
# Пакеты ВМ идут через кэширующий прокси apt на хосте (apt-cacher-ng на мосту):
# установка на ВМ та же - apt-get update/install с чистой Ubuntu, но пакеты
# берутся из кэша раннера, а не с зеркал на каждом прогоне. Каталог кэша
# переживает прогон через actions/cache (stand.yml).
# HTTPS-источники (репо Docker) идут мимо: прокси не держит туннели CONNECT.
APT_CACHE=${STAND_APT_CACHE:-/var/tmp/vpn2-apt-cache}
APT_PROXY=http://$NET.1:3142
# Сторож возвращает канал на hy2 после стольких секунд живого hy2 (в продукте 60).
TCP_RETURN_S=20

log() { printf '[stand] %s\n' "$*"; }
die() { printf '[stand] ✗ %s\n' "$*" >&2; exit 1; }

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
  # Метка и причина - копия _SSH_MARK и _SSH_NO_MARK_REASON из infra/lib/ssh.sh:
  # инструмент ssh.sh не подключает.
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
if apt-get -q update >/dev/null; then say 'индекс apt'; else say 'индекс apt не вышел'; fi
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
  # Как у хостера: root по паролю (00-bootstrap разносит ключ оркестратора).
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
      TLS_INITIAL_WAIT=$TLS_INITIAL_WAIT
  - path: /etc/systemd/system/anysda-failover-watchdog.service.d/10-vpn2-ci.conf
    content: |
      [Service]
      Environment=TCP_RETURN_S=$TCP_RETURN_S
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

fetch_image() {
  local image=$1 sums name url size i a b pids=() started=$SECONDS
  log "образ: $IMAGE_URL"
  # Один поток упирается в ~50 МБ/с: образ (~850 МБ) - четырьмя диапазонами
  # с одного зеркала (адрес после редиректа); без длины - одним потоком.
  url=$(curl -fsIL --retry 3 -o /dev/null -w '%{url_effective}' "$IMAGE_URL")
  size=$(curl -fsI --retry 3 "$url" | tr -d '\r' | awk 'tolower($1) == "content-length:" {print $2}' | tail -n1)
  if [[ "$size" =~ ^[0-9]+$ ]] && (( size > 4 )); then
    for i in 0 1 2 3; do
      a=$((size * i / 4)) b=$((size * (i + 1) / 4 - 1))
      curl -fsS --retry 3 -r "$a-$b" -o "$image.part$i" "$url" & pids+=($!)
    done
    for i in "${pids[@]}"; do wait "$i" || die 'образ ВМ не скачан'; done
    cat "$image.part0" "$image.part1" "$image.part2" "$image.part3" > "$image.part"
    rm -f "$image.part0" "$image.part1" "$image.part2" "$image.part3"
    (( $(stat -c %s "$image.part") == size )) || die 'образ ВМ скачан не целиком'
  else
    curl -fsSL --retry 3 -o "$image.part" "$IMAGE_URL"
  fi
  log "образ скачан за $((SECONDS - started)) с"
  sums=$(curl -fsSL --retry 3 "${IMAGE_URL%/*}/SHA256SUMS")
  name=${IMAGE_URL##*/}
  printf '%s\n' "$sums" | awk -v n="$name" '$2 == n || $2 == "*" n {print $1 "  '"$image.part"'"}' \
    | grep . | sha256sum --check --status || die "контрольная сумма $name не сошлась"
  mv "$image.part" "$image"
}

# Ядро того же образа (каталог unpacked рядом с ним): qemu грузит его сам,
# без GRUB, читающего initrd (~70 МБ) через BIOS, - до ядра ~2 с вместо ~9.
# Без initrd: virtio и ext4 в ядре, корень - первый раздел диска (~4 с
# загрузки). Корень тот же, плюс mitigations=off: ВМ одноразовая, чужого кода
# в ней нет, а retpoline и Safe RET на AMD раннера тормозят dpkg и стадии.
# snapd продукту не нужен: его засев держит cloud-init на загрузке ~2 с.
# Нет ядра - через GRUB.
fetch_kernel() {
  local dir=$1 name sums
  name=$(basename "$IMAGE_URL" .img)-vmlinuz-generic
  sums=$(curl -fsSL --retry 3 "${IMAGE_URL%/*}/unpacked/SHA256SUMS") || return 1
  curl -fsSL --retry 3 -o "$dir/vmlinuz.part" "${IMAGE_URL%/*}/unpacked/$name" || return 1
  printf '%s\n' "$sums" | awk -v n="$name" '$2 == n || $2 == "*" n {print $1 "  '"$dir/vmlinuz.part"'"}' \
    | grep . | sha256sum --check --status || return 1
  mv "$dir/vmlinuz.part" "$dir/vmlinuz"
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

cmd_up() {
  [[ -e /dev/kvm ]] || die '/dev/kvm нет: нужна машина с аппаратной виртуализацией'
  mkdir -p "$STAND_DIR"
  host_machine
  host_stats 'начало up'
  chmod 700 "$STAND_DIR"
  local image=$STAND_DIR/base.img fetch='' up_t0=$SECONDS
  # Образ качается, пока ставится qemu.
  if [[ ! -f "$image" ]]; then
    fetch_image "$image" &
    fetch=$!
  fi
  local kfetch='' boot=()
  [[ -f "$STAND_DIR/vmlinuz" ]] || { fetch_kernel "$STAND_DIR" & kfetch=$!; }
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
  if [[ -f "$STAND_DIR/vmlinuz" ]]; then
    boot=(-kernel "$STAND_DIR/vmlinuz"
      -append 'root=/dev/vda1 rootwait ro console=tty1 console=ttyS0 mitigations=off systemd.mask=snapd.service systemd.mask=snapd.socket systemd.mask=snapd.seeded.service')
  fi
  [[ -f "$STAND_DIR/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N '' -C vpn2-ci-stand -f "$STAND_DIR/id_ed25519"
  [[ -f "$STAND_DIR/root-password" ]] || (umask 077; openssl rand -hex 16 > "$STAND_DIR/root-password")
  net_up
  apt_proxy_up
  local node name ip cpus mem mac tap vhost=''
  # Сеть ВМ в ядре хоста (vhost-net), а не в потоке qemu: меньше CPU раннера
  # на образ панели, пакеты и трафик 99-verify.
  modprobe vhost_net 2>/dev/null || true
  [[ -c /dev/vhost-net ]] && vhost=,vhost=on
  for node in "${NODES[@]}"; do
    read -r name ip cpus mem mac <<<"$node"
    tap=$BRIDGE-$name
    seed "$name" "$ip" "$mac" "$(cat "$STAND_DIR/root-password")"
    # Диск одноразовый: fsync гостя (dpkg на каждый пакет) хосту не нужен.
    qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$STAND_DIR/$name.qcow2" 20G
    ip link show "$tap" >/dev/null 2>&1 || ip tuntap add dev "$tap" mode tap
    ip link set "$tap" master "$BRIDGE" up
    qemu-system-x86_64 -name "vpn2-$name" -machine q35,accel=kvm -cpu host -smp "$cpus" -m "$mem" \
      -drive "file=$STAND_DIR/$name.qcow2,if=virtio,discard=unmap,cache=unsafe" \
      -drive "file=$STAND_DIR/$name-seed.iso,if=virtio,format=raw,readonly=on" \
      -netdev "tap,id=lan,ifname=$tap,script=no,downscript=no$vhost" -device "virtio-net-pci,netdev=lan,mac=$mac" \
      -device virtio-rng-pci -display none -serial "file:$STAND_DIR/$name-console.log" \
      -daemonize -pidfile "$STAND_DIR/$name.pid" "${boot[@]}"
    log "ВМ $name: $ip, ${cpus} vCPU, ${mem} МиБ, запущена через $((SECONDS - up_t0)) с от начала up${boot:+, ядро без GRUB}${vhost:+, vhost-net}"
  done
  # Узлы ждём параллельно: последовательно второй замечали на ~3 с позже.
  local waits=()
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    vm_wait_ready "$name" "$ip" "$up_t0" & waits+=($!)
  done
  local p
  for p in "${waits[@]}"; do wait "$p" || die 'ВМ стенда не поднялись'; done
  host_stats 'ВМ готовы'
  entry_stats 'ВМ готовы'
}

# up фоном: ВМ грузятся, пока раннер собирает образ панели; up-wait ждёт
# конца и отдаёт вывод и код up.
cmd_up_bg() {
  mkdir -p "$STAND_DIR"
  chmod 700 "$STAND_DIR"
  : > "$STAND_DIR/up.rc"
  setsid nohup bash -c '"$0" up > "$1/up.log" 2>&1; echo $? > "$1/up.rc"' "$0" "$STAND_DIR" \
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

push_tree() {
  local password
  password=$(cat "$STAND_DIR/root-password")
  log "исходники $(git -C "$REPO_ROOT" rev-parse --short HEAD) → entry:/opt/anysda-vpn2"
  ssh_node "$ENTRY" 'rm -rf /opt/anysda-vpn2 && mkdir -p /opt/anysda-vpn2'
  git -C "$REPO_ROOT" ls-files -z | tar -C "$REPO_ROOT" --null -T - -czf - | ssh_node "$ENTRY" 'tar -xzf - -C /opt/anysda-vpn2'
  ssh_node "$ENTRY" "umask 077; cat > /opt/anysda-vpn2/config.yaml" <<EOF
entry:
  host: $ENTRY
  password: "$password"
exits:
  - tag: $EXIT_TAG
    host: $EXIT
    password: "$password"
admin:
  user: anysda
panel:
  image: $PANEL_REF
EOF
}

# Docker на entry ставит 00-bootstrap, а образ панели нужен только к
# 35-telegram и 30-frontend: грузится, как только docker поднялся.
load_panel() {
  local deadline=$((SECONDS + 600))
  until ssh_node "$ENTRY" 'systemctl is-active -q docker' 2>/dev/null \
      && docker save "$PANEL_REF" | gzip -1 | ssh_node "$ENTRY" 'gunzip | docker load' >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { log 'образ панели не загрузился на entry за 10 мин'; return 1; }
    sleep 2
  done
  log "образ панели $PANEL_REF → entry"
}

# Итог прогрева по узлам: время от загрузки ВМ (монотонные часы журнала),
# рядом - аптайм entry на старте деплоя.
warm_report() {
  local node name ip
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    ssh_node "$ip" 'journalctl -u vpn2-ci-warm -o short-monotonic --no-pager -q' 2>/dev/null \
      | sed -n "s/^\[ *\([0-9.]*\)\].*\(прогрев: .*\)/[stand] ВМ $name, \1 с от загрузки: \2/p" || true
  done
}

entry_uptime() { ssh_node "$ENTRY" 'cut -d" " -f1 /proc/uptime' 2>/dev/null || echo '?'; }

cmd_deploy() {
  local image=${1:?образ панели (docker-образ на этой машине)}
  host_stats 'до деплоя'
  entry_stats 'до деплоя'
  apt_proxy_up --if-down
  log "деплой начат на $(entry_uptime) с от загрузки entry"
  push_tree
  docker tag "$image" "$PANEL_REF"
  load_panel &
  local loader=$! started=$SECONDS rc=0
  entry_run deploy './deploy.sh --development' || rc=$?
  wait "$loader" || (( rc )) || rc=1
  log "деплой: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  warm_report
  host_stats 'после деплоя'
  return "$rc"
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
  local stage started=$SECONDS
  for stage in "${UDP_STAGES[@]}"; do
    close_entry_mux
    entry_run "deploy-${stage%% *}" \
      "mkdir -p infra/state && flock -w 70 infra/state/deploy.lock true && ./deploy.sh --development $stage" \
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

# Сколько держать обрыв. Сторож проверяет каналы раз в INTERVAL=2 с, мёртвым
# канал считает после DEAD_AFTER промахов, sing-box перезапускает после
# RESTART_AFTER_CHECKS проверок: любой исход виден за ~20 с.
UDP_CUT_HOLD_S=30
UDP_CUT_MAX_GAP=15  # окно без ответа через экзит, с: на стенде из VPS 2,4 с

# Каналы с запасным TCP на entry: «селектор hy2|tcp» по строке, как их видит
# сторож (_transport_pairs в failover-watchdog.py).
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
  ssh_node "$ENTRY" "systemctl show -p Environment anysda-failover-watchdog | grep -qw 'TCP_RETURN_S=$tcp_return'" \
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
  sleep "$UDP_CUT_HOLD_S"
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

  local rc=0 n back_s
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
  log "обрыв UDP: код $rc"
  return "$rc"
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
  up) cmd_up ;;
  up-bg) cmd_up_bg ;;
  up-wait) cmd_up_wait ;;
  deploy) cmd_deploy "${@:2}" ;;
  deploy-udp) cmd_deploy_udp ;;
  verify) cmd_verify "${@:2}" ;;
  udp-cut) cmd_udp_cut "${@:2}" ;;
  collect) cmd_collect "${@:2}" ;;
  down) cmd_down ;;
  *) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 64 ;;
esac
