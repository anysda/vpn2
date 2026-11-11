#!/usr/bin/env bash
# Бэкап узла: работу ведёт пакет vpn2 (команда `vpn2 backup`), здесь только его запуск.
# Пакет берётся из окружения деплоя, иначе из клона рядом со скриптом или по отметке
# /etc/anysda/source-root (её пишет стадия 26-backup); без отметки - /opt/anysda-vpn2.
set -euo pipefail

[[ -x /opt/vpn2/venv/bin/vpn2 ]] && exec /opt/vpn2/venv/bin/vpn2 backup "$@"
src=$(dirname "$(readlink -f "$0")")/../vpn2/src
if [[ ! -d "$src/vpn2" ]]; then
  root=$(cat /etc/anysda/source-root 2>/dev/null || true)
  src=${root:-/opt/anysda-vpn2}/infra/vpn2/src
fi
[[ -d "$src/vpn2" ]] || { echo "anysda-backup: нет пакета vpn2 в $src" >&2; exit 2; }
PYTHONPATH=$src exec python3 -m vpn2.cli backup "$@"
