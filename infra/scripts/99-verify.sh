#!/usr/bin/env bash
# Stage 99 — проверка узла пакетом vpn2: 0 успешно, 1 отказ, 2 не проверено.
# Пакет лежит рядом (доставка deploy) или в дереве исходников (restore).
set -Eeuo pipefail

set -a
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
set +a
dir=$(dirname "${BASH_SOURCE[0]}")
src=$dir/vpn2-src
[[ -d "$src/vpn2" ]] || src=$dir/../vpn2/src
PYTHONPATH=$src exec python3 -B -m vpn2.cli verify
