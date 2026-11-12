#!/usr/bin/env bash
# anysda-vpn — интерактивный конфигуратор
# Задаёт вопросы → записывает config.yaml

set -euo pipefail

# Принудительно UTF-8, чтобы пароли/тексты не уехали в cp1251 и не сломали YAML
export LANG=C.UTF-8 LC_ALL=C.UTF-8

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

usage() {
  cat <<'EOF'
anysda-vpn — интерактивный конфигуратор
Задаёт вопросы → записывает config.yaml

Команды:
  ./setup.sh                    Запустить мастер настройки
  ./setup.sh -h | --help        Показать справку
EOF
}

# Все аргументы проверяем до запуска мастера.
case "$#:${1:-}" in
  0:) ;;
  1:-h|1:--help) usage; exit 0 ;;
  1:*) printf 'Неизвестный аргумент: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  *) printf 'Неожиданный аргумент: %s\n' "$2" >&2; usage >&2; exit 1 ;;
esac

# Мастер запускается до bootstrap: на минимальной системе его утилиты ещё
# никто не установил. В частности, отсутствие dig не означает ошибку DNS.
setup_prereqs() {
  local cmd pkgs=()
  for cmd in curl ip python3 dig; do
    command -v "$cmd" >/dev/null 2>&1 && continue
    case "$cmd" in
      ip) pkgs+=(iproute2) ;;
      dig) pkgs+=(bind9-dnsutils) ;;
      *) pkgs+=("$cmd") ;;
    esac
  done
  ((${#pkgs[@]})) || return 0
  if [[ $EUID -ne 0 || ! -f /etc/debian_version ]]; then
    local red="" end=""
    if [[ -t 1 ]]; then red='\033[31m' end='\033[0m'; fi
    printf '%b  ✗ До запуска мастера установите пакеты: %s%b\n' "$red" "${pkgs[*]}" "$end" >&2
    return 1
  fi
  # shellcheck source=infra/lib/anysda-svc.sh
  source "$REPO_ROOT/infra/lib/anysda-svc.sh"
  export DEBIAN_FRONTEND=noninteractive
  apt_prepare
  apt_get update -qq
  apt_get install -y -qq "${pkgs[@]}" ca-certificates
}
setup_prereqs

# Вопросы и запись config.yaml - в vpn2.setup. Пакет ещё не установлен, модуль
# берётся из дерева репы и обходится стандартной библиотекой.
PYTHONSAFEPATH=1 PYTHONPATH="$REPO_ROOT/infra/vpn2/src" exec python3 -m vpn2.setup
