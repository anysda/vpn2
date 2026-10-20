#!/usr/bin/env bash
# Отложенный откат фаервола на время стадии деплоя.
#
#   fw-guard.sh arm STAGE WINDOW  снимок ufw и своих nft-таблиц, запуск сторожа
#   fw-guard.sh beat              отметка «новый ssh-вход прошёл»
#   fw-guard.sh watch WINDOW      сторож (запускает arm через systemd-run)
#   fw-guard.sh disarm STAGE      снять сторожа; код 3 - фаервол уже откатан
#
# deploy.sh во время стадии каждые несколько секунд открывает НОВОЕ
# ssh-соединение и зовёт beat. Уже открытое соединение стадии живёт и при
# сломанном правиле (conntrack пропускает established), поэтому оно ничего не
# доказывает. Нет новых входов дольше окна - сторож возвращает ufw и таблицы к
# снимку до стадии, а deploy.sh узнаёт об этом при disarm и валит стадию.
# Сторож - отдельный юнит systemd: он переживает обрыв ssh и саму стадию.
set -euo pipefail

DIR=${FW_GUARD_DIR:-/run/vpn2-fw-guard}
ETC=${FW_GUARD_ETC:-/etc}
UNIT=vpn2-fw-guard
# Свои nft-таблицы стадий (19-yt-zapret). Чужие таблицы (Docker, fail2ban) не
# снимаем: откат всего ruleset стёр бы то, что службы добавили за время стадии.
TABLES=('inet ytzapret' 'inet anysda_ytquic')

say() { echo "vpn2-fw-guard: $*"; }

snapshot() {
  local snap=$DIR/snap table file
  mkdir -p "$snap"
  if command -v ufw >/dev/null; then
    cp -a "$ETC/ufw" "$snap/ufw"
    [[ ! -f "$ETC/default/ufw" ]] || cp -a "$ETC/default/ufw" "$snap/default-ufw"
    if LC_ALL=C ufw status | grep -qx 'Status: active'; then touch "$snap/ufw-active"; fi
  else
    touch "$snap/no-ufw"
  fi
  for table in "${TABLES[@]}"; do
    file=$snap/nft-${table// /-}
    # shellcheck disable=SC2086
    if command -v nft >/dev/null && nft list table $table > "$file" 2>/dev/null; then :; else rm -f "$file"; fi
  done
}

restore() {
  local snap=$DIR/snap table file rc=0
  if [[ -d "$snap/ufw" ]]; then
    rm -rf "$ETC/ufw.vpn2-guard"
    cp -a "$snap/ufw" "$ETC/ufw.vpn2-guard"
    rm -rf "$ETC/ufw"
    mv "$ETC/ufw.vpn2-guard" "$ETC/ufw"
    [[ ! -f "$snap/default-ufw" ]] || cp -a "$snap/default-ufw" "$ETC/default/ufw"
  fi
  if command -v ufw >/dev/null; then
    if [[ -e "$snap/ufw-active" ]]; then
      # enable на включённом ufw правил с диска не перечитывает - нужен reload.
      ufw --force enable >/dev/null && ufw reload >/dev/null || rc=1
    else
      ufw --force disable >/dev/null || rc=1
    fi
  fi
  if command -v nft >/dev/null; then
    for table in "${TABLES[@]}"; do
      file=$snap/nft-${table// /-}
      # shellcheck disable=SC2086
      nft delete table $table 2>/dev/null || true
      [[ ! -f "$file" ]] || nft -f "$file" || rc=1
    done
  fi
  return $rc
}

age() {
  echo $(( $(date +%s) - $(stat -c %Y "$DIR/alive") ))
}

case "${1:-}" in
  arm)
    stage=$2 window=$3
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    rm -rf "$DIR"
    mkdir -p "$(dirname "$DIR")"
    mkdir -m 700 "$DIR"
    echo "$stage" > "$DIR/stage"
    snapshot
    cp "${BASH_SOURCE[0]}" "$DIR/fw-guard.sh"
    touch "$DIR/alive"
    systemd-run --quiet --collect --unit="$UNIT" \
      --setenv=FW_GUARD_DIR="$DIR" --setenv=FW_GUARD_ETC="$ETC" \
      /bin/bash --norc "$DIR/fw-guard.sh" watch "$window"
    ;;
  beat)
    [[ ! -d "$DIR" ]] || touch "$DIR/alive"
    ;;
  watch)
    window=$2
    while sleep 2; do
      [[ -d "$DIR" && ! -e "$DIR/decided" ]] || exit 0
      (( $(age) > window )) || continue
      # decided - общий замок с disarm: решает тот, кто создал его первым.
      mkdir "$DIR/decided" 2>/dev/null || exit 0
      say "новых ssh-входов нет $(age) с (окно ${window} с), откатываю фаервол к снимку до стадии $(cat "$DIR/stage")"
      rc=0
      restore || rc=$?
      echo "$rc" > "$DIR/restored"
      exit "$rc"
    done
    ;;
  disarm)
    stage=$2
    [[ -d "$DIR" ]] || { say "сторож стадии $stage не найден"; exit 4; }
    if mkdir "$DIR/decided" 2>/dev/null; then
      systemctl stop "$UNIT" >/dev/null 2>&1 || true
      rm -rf "$DIR"
      exit 0
    fi
    for _ in $(seq 120); do [[ -e "$DIR/restored" ]] && break; sleep 1; done
    rc=$(cat "$DIR/restored" 2>/dev/null || echo '?')
    if [[ "$rc" == 0 ]]; then
      say "стадия $stage: новые ssh-входы не проходили дольше окна, ufw и таблицы стадий откатаны к состоянию до неё"
    else
      say "стадия $stage: новые ssh-входы не проходили дольше окна, откат фаервола не удался (код $rc), см. journalctl -u $UNIT"
    fi
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    rm -rf "$DIR"
    exit 3
    ;;
  *)
    echo "usage: $0 arm STAGE WINDOW | beat | watch WINDOW | disarm STAGE" >&2
    exit 64
    ;;
esac
