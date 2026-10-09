# shellcheck shell=bash
# Общие приёмы стадий деплоя и anysda-restore: не рвать клиентов зря.
#
# Рестарт службы обрывает сессии: IKEv2-устройства после рестарта strongswan
# сами не переподключаются, у WireGuard обнуляются рукопожатия. Поэтому
# службу перезапускаем, только если она читает уже не то, с чем стартовала,
# тем же отпечатком, что у sing-box в 10-foreign и 20-ru-router.

SVC_FP_DIR=${SVC_FP_DIR:-/var/lib/anysda/applied}

svc_say() { echo "[${HOST_TAG:-anysda}]   $*"; }

# svc_restart_if_changed UNIT FILE...
# FILE — всё, что служба читает при старте: бинарник, юнит, конфиги,
# сертификаты. Отпечаток их содержимого сверяем с записанным при прошлом
# рестарте; нет файла — нет его и в отпечатке.
svc_restart_if_changed() {
  local unit=$1 fp_file fp
  shift
  fp_file="$SVC_FP_DIR/$unit.sha256"
  fp=$({ cat "$@" 2>/dev/null || true; } | sha256sum | cut -d' ' -f1)
  if systemctl is-active --quiet "$unit" && [[ "$(cat "$fp_file" 2>/dev/null)" == "$fp" ]]; then
    svc_say "$unit: конфиг, сертификаты и бинарник прежние — не перезапускаю"
    return 0
  fi
  systemctl restart "$unit" || return
  mkdir -p "$SVC_FP_DIR"
  echo "$fp" > "$fp_file"
  svc_say "$unit перезапущен"
}

# svc_forget UNIT — вход службы поменялся так, что отпечаток файлов этого не
# заметит. Следующий svc_restart_if_changed её перезапустит.
svc_forget() { rm -f "$SVC_FP_DIR/$1.sha256"; }

# wg0_apply — применить /etc/wireguard/wg0.conf. Ключи и пиры `wg syncconf`
# меняет на живом интерфейсе без обрыва. Рестарт wg-quick нужен, только если
# интерфейса нет или в wg0.conf другие адреса или порт.
wg0_conf_iface() {
  awk -F= '
    /^\[/ { sec = $0; next }
    sec == "[Interface]" {
      k = $1; v = $2; gsub(/[ \t]/, "", k); gsub(/[ \t]/, "", v)
      if (k == "Address") { n = split(v, a, ","); for (i = 1; i <= n; i++) print "addr " a[i] }
      if (k == "ListenPort") print "port " v
    }' /etc/wireguard/wg0.conf | sort
}
wg0_live_iface() {
  { ip -o addr show dev wg0 scope global | awk '{print "addr " $4}'
    echo "port $(wg show wg0 listen-port)"; } | sort
}
wg0_apply() {
  if systemctl is-active --quiet wg-quick@wg0 && ip link show wg0 >/dev/null 2>&1 \
     && [[ "$(wg0_conf_iface)" == "$(wg0_live_iface)" ]]; then
    wg syncconf wg0 <(wg-quick strip wg0) || return
    svc_say "адреса и порт wg0 прежние — wg syncconf без рестарта"
  else
    systemctl restart wg-quick@wg0 || return
    svc_say "wg-quick@wg0 перезапущен"
  fi
}
