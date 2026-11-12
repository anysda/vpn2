#!/usr/bin/env bash
# ./deploy.sh backup и restore: архив снимают и разворачивают скрипты entry.

do_backup() {
  local rc=0 out warning
  out=$(mktemp -p "$TMP")
  /usr/local/bin/anysda-backup.sh | tee "$out" || rc=$?
  warning=$(grep -m1 '^warning:' "$out" || true)
  # Код 4 со строкой warning: архив годный. Без неё - сбой, архива нет.
  [[ $rc -eq 4 && -n "$warning" ]] || return "$rc"
  if [[ "$warning" == *'сверка БД с конфигами VPN не выполнена'* ]]; then
    warn 'архив сохранён, сверка с конфигами VPN не выполнена - не проверено, причина выше'
  else
    warn 'архив сохранён с предупреждением: БД и конфиги VPN расходятся - перевыпусти перечисленные выше устройства в панели'
  fi
}

# restore [<имя>|latest|<файл>] [--force] [--dry-run]: архив разворачивает anysda-restore.sh,
# затем стадии пересобирают всё, что читает восстановленное (ключи и PKI стадии не трогают).
do_restore() {
  local archive=latest flags dir base stage
  if [[ -n "${1:-}" && "$1" != -* ]]; then archive=$1; shift; fi
  flags=("$@")
  if [[ "$archive" != latest && -f "$archive" ]]; then
    base=$(basename "$archive")
    if [[ " ${flags[*]} " == *' --dry-run '* ]]; then
      echo "restore: dry-run — загрузил бы на entry и восстановил локальный архив $base, ничего не изменено"
      return 0
    fi
    dir=$(source /etc/anysda/backup.env 2>/dev/null; echo "${BACKUP_LOCAL_DIR:-/var/backups/anysda-vpn2}")
    [[ "$archive" -ef "$dir/$base" ]] || install -D -m 600 "$archive" "$dir/$base"
    archive=$base
  fi
  /usr/local/bin/anysda-restore.sh "$archive" "${flags[@]}" || return $?
  [[ " ${flags[*]} " != *' --dry-run '* && " ${flags[*]} " != *' --preflight '* ]] || return 0
  # После restore стадии читают пароль панели из восстановленного файла, а не из config.yaml.
  export VPN2_PRESERVE_ADMIN=1
  for stage in 29-openvpn 20-ru-router 21-failover-watchdog 26-backup 27-ikev2 35-telegram 30-frontend; do
    run_stage "$stage" ru
  done
}

# Отказ restore (архива нет, не тот пароль, версия, живая база без --force) приходит до
# открытия выпуска. Пакет узла старше дерева флага --preflight не знает - тогда --dry-run.
restore_preflight() {
  local out rc=0
  out=$(do_restore "$@" --preflight 2>&1) || rc=$?
  if (( rc == 2 )) && [[ "$out" == *'неизвестный аргумент: --preflight'* ]]; then
    rc=0
    out=$(do_restore "$@" --dry-run 2>&1) || rc=$?
  fi
  (( rc == 0 )) || printf '%s\n' "$out" >&2
  return "$rc"
}
