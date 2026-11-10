#!/usr/bin/env bash
# Состояние деплоя на entry: адреса mesh, исключённые экзиты, манифест выпуска,
# known_hosts узлов. Живёт вне checkout и входит в бэкап. Файл для source.

: "${VPN2_STATE_DIR:=/var/lib/anysda-vpn2/state}"
# Замок запуска - не состояние: в бэкап и слепок не попадает.
: "${VPN2_DEPLOY_LOCK:=/run/lock/anysda-vpn2-deploy.lock}"
export VPN2_STATE_DIR VPN2_DEPLOY_LOCK

# Каталог состояния: только root, панель (свой uid в том же /var/lib/anysda-vpn2)
# known_hosts не читает.
state_dir_ensure() {
  install -d -m 700 "$VPN2_STATE_DIR"
}

# Один файл из checkout в каталог состояния. Ничего не стирается: при разном
# содержимом главнее цель (новый код пишет только её), источник уходит в checkout-old.
_state_move() {
  local src="$1" dst="$2" keep
  [[ -f "$src" ]] || return 0
  if [[ ! -e "$dst" ]]; then
    # Между файловыми системами mv не атомарен: копия рядом с целью и rename.
    cp -p "$src" "$dst.tmp" && mv -T "$dst.tmp" "$dst" || return 1
  elif ! cmp -s "$src" "$dst"; then
    keep="$VPN2_STATE_DIR/checkout-old/${dst##*/}"
    mkdir -p "${keep%/*}"
    cp -p "$src" "$keep" || return 1
    printf '⚠ состояние в checkout отличалось от %s, сохранено в %s\n' "$dst" "$keep" >&2
  fi
  rm -f "$src"
}

# Перенос состояния старой раскладки (infra/state/*, infra/.known_hosts) в
# VPN2_STATE_DIR. Аргумент - каталог infra. Зовётся под замком деплоя и до
# config2env.py: тот при пустом каталоге засеял бы октеты mesh заново.
# Повторный вызов и вызов после обрыва посередине доводят перенос до конца.
state_dir_migrate() {
  local infra="$1" src name
  for name in known_hosts known_hosts.old; do
    _state_move "$infra/.$name" "$VPN2_STATE_DIR/$name" || return 1
  done
  [[ ! -f "$VPN2_STATE_DIR/known_hosts" ]] || chmod 600 "$VPN2_STATE_DIR/known_hosts"
  [[ -d "$infra/state" ]] || return 0
  for src in "$infra/state"/* "$infra/state"/.[!.]*; do
    [[ -f "$src" ]] || continue
    name="${src##*/}"
    if [[ "$name" == deploy.lock ]]; then rm -f "$src"; continue; fi
    _state_move "$src" "$VPN2_STATE_DIR/$name" || return 1
  done
  rmdir "$infra/state" 2>/dev/null || true
}
