#!/usr/bin/env bash
# Стенд внутри одной машины с KVM: entry и экзит - ВМ из облачного образа
# Ubuntu 26.04 в своей сети за NAT хоста. Продукт ставится штатным
# ./deploy.sh с самого entry, как по README; 99-verify гоняет настоящий
# трафик WireGuard/OpenVPN/IKEv2.
#
#   sudo infra/tools/ci-stand.sh up                     поднять ВМ
#   sudo infra/tools/ci-stand.sh deploy <образ панели>  полный деплой
#   sudo infra/tools/ci-stand.sh verify [--stop-openvpn]
#   sudo infra/tools/ci-stand.sh collect <каталог>      отчёты и журналы
#   sudo infra/tools/ci-stand.sh down                   снести всё
#
# Образ панели - локальный docker-образ, собранный из этого же дерева; на
# entry он загружается до деплоя, deploy берёт его по panel.image.
# verify возвращает код 99-verify: 0 работает, 1 ошибка, 2 не проверено.
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
NODES=("entry $NET.10 2 3072 52:54:00:12:0a:0a" "exit $NET.20 2 2048 52:54:00:12:0a:14")
ENTRY=$NET.10
EXIT=$NET.20

log() { printf '[stand] %s\n' "$*"; }
die() { printf '[stand] ✗ %s\n' "$*" >&2; exit 1; }

ssh_node() {
  local host=$1
  shift
  # Текст из .bashrc ВМ приходит в stdout раньше вывода команды: команда
  # сначала печатает метку, всё до неё отбрасывается.
  ssh -i "$STAND_DIR/id_ed25519" -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=8 "root@$host" \
    "printf '\\n%s\\n' vpn2-ssh-output-begin;" "$@" | sed -u '0,/^vpn2-ssh-output-begin$/d'
  return "${PIPESTATUS[0]}"
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
bootcmd:
  - [systemctl, disable, --now, apt-daily.timer, apt-daily-upgrade.timer]
runcmd:
  - [systemctl, restart, ssh]
EOF
  cloud-localds --network-config="$dir/network-config" "$STAND_DIR/$name-seed.iso" "$dir/user-data" "$dir/meta-data"
}

cmd_up() {
  [[ -e /dev/kvm ]] || die '/dev/kvm нет: нужна машина с аппаратной виртуализацией'
  mkdir -p "$STAND_DIR"
  chmod 700 "$STAND_DIR"
  local image=$STAND_DIR/base.img
  if [[ ! -f "$image" ]]; then
    log "образ: $IMAGE_URL"
    curl -fsSL --retry 3 -o "$image.part" "$IMAGE_URL"
    local sums name
    sums=$(curl -fsSL --retry 3 "${IMAGE_URL%/*}/SHA256SUMS")
    name=${IMAGE_URL##*/}
    printf '%s\n' "$sums" | awk -v n="$name" '$2 == n || $2 == "*" n {print $1 "  '"$image.part"'"}' \
      | grep . | sha256sum --check --status || die "контрольная сумма $name не сошлась"
    mv "$image.part" "$image"
  fi
  [[ -f "$STAND_DIR/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N '' -C vpn2-ci-stand -f "$STAND_DIR/id_ed25519"
  [[ -f "$STAND_DIR/root-password" ]] || (umask 077; openssl rand -hex 16 > "$STAND_DIR/root-password")
  net_up
  local node name ip cpus mem mac tap
  for node in "${NODES[@]}"; do
    read -r name ip cpus mem mac <<<"$node"
    tap=$BRIDGE-$name
    seed "$name" "$ip" "$mac" "$(cat "$STAND_DIR/root-password")"
    qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$STAND_DIR/$name.qcow2" 20G
    ip link show "$tap" >/dev/null 2>&1 || ip tuntap add dev "$tap" mode tap
    ip link set "$tap" master "$BRIDGE" up
    qemu-system-x86_64 -name "vpn2-$name" -machine q35,accel=kvm -cpu host -smp "$cpus" -m "$mem" \
      -drive "file=$STAND_DIR/$name.qcow2,if=virtio,discard=unmap" \
      -drive "file=$STAND_DIR/$name-seed.iso,if=virtio,format=raw,readonly=on" \
      -netdev "tap,id=lan,ifname=$tap,script=no,downscript=no" -device "virtio-net-pci,netdev=lan,mac=$mac" \
      -device virtio-rng-pci -display none -serial "file:$STAND_DIR/$name-console.log" \
      -daemonize -pidfile "$STAND_DIR/$name.pid"
    log "ВМ $name: $ip, ${cpus} vCPU, ${mem} МиБ"
  done
  for node in "${NODES[@]}"; do
    read -r name ip _ <<<"$node"
    local deadline=$((SECONDS + 600))
    until ssh_node "$ip" 'cloud-init status --wait >/dev/null 2>&1; test -f /var/lib/cloud/instance/boot-finished'; do
      (( SECONDS < deadline )) || { tail -n 40 "$STAND_DIR/$name-console.log" >&2; die "ВМ $name не поднялась за 10 мин"; }
      sleep 5
    done
    log "ВМ $name готова: $(ssh_node "$ip" '. /etc/os-release; echo "$PRETTY_NAME"')"
  done
}

# Долгая команда на entry отвязана от ssh: обрыв сессии её не убивает.
# Вывод идёт в журнал на entry, отсюда его хвост печатается по мере роста.
entry_run() {
  local name=$1 command=$2 shown=0 lines rc
  ssh_node "$ENTRY" "rm -f /root/$name.rc; cd /opt/anysda-vpn2 || exit; setsid nohup bash -c '$command > /root/$name.log 2>&1; echo \$? > /root/$name.rc' >/dev/null 2>&1 < /dev/null &"
  while :; do
    lines=$(ssh_node "$ENTRY" "wc -l < /root/$name.log" 2>/dev/null || echo "$shown")
    if (( lines > shown )); then
      ssh_node "$ENTRY" "sed -n '$((shown + 1)),${lines}p' /root/$name.log" || true
      shown=$lines
    fi
    if rc=$(ssh_node "$ENTRY" "cat /root/$name.rc 2>/dev/null") && [[ -n "$rc" ]]; then
      ssh_node "$ENTRY" "sed -n '$((shown + 1)),\$p' /root/$name.log" || true
      return "$rc"
    fi
    sleep 10
  done
}

cmd_deploy() {
  local image=${1:?образ панели (docker-образ на этой машине)} password
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
  # Docker на entry ставят пререквизиты оркестратора и 00-bootstrap; образ
  # нужен к 30-frontend, поэтому сначала только первая стадия.
  entry_run bootstrap './deploy.sh --development 00-bootstrap ru' || die "00-bootstrap ru: код $?"
  log "образ панели $image → entry"
  docker tag "$image" "$PANEL_REF"
  docker save "$PANEL_REF" | gzip -1 | ssh_node "$ENTRY" 'gunzip | docker load'
  local started=$SECONDS rc=0
  entry_run deploy './deploy.sh --development' || rc=$?
  log "деплой: код $rc за $(( (SECONDS - started) / 60 )) мин $(( (SECONDS - started) % 60 )) с"
  return "$rc"
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

cmd_collect() {
  local out=${1:?каталог для отчётов} host tag
  mkdir -p "$out"
  for host in "$ENTRY:ru" "$EXIT:$EXIT_TAG"; do
    tag=${host#*:}
    ssh_node "${host%%:*}" 'cat /var/lib/anysda/verify-report.json' > "$out/verify-report-$tag.json" 2>/dev/null \
      || rm -f "$out/verify-report-$tag.json"
  done
  local name
  for name in bootstrap deploy verify; do
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
}

case "${1:-}" in
  up) cmd_up ;;
  deploy) cmd_deploy "${@:2}" ;;
  verify) cmd_verify "${@:2}" ;;
  collect) cmd_collect "${@:2}" ;;
  down) cmd_down ;;
  *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 64 ;;
esac
