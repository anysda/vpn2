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

