#!/usr/bin/env bash
# Мастер целиком: вывод сводки, сохранение конфига и повторный запуск.
# В CI запускается с Python 3.11 — минимальной версией матрицы выпуска.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/infra/lib"
cp "$ROOT/setup.sh" "$FIXTURE/"
cp "$ROOT/infra/lib/config2env.py" "$FIXTURE/infra/lib/"

# Не обращаться к сети и не устанавливать пакеты на машине теста.
# shellcheck disable=SC2317,SC2329
ip() { return 0; }
# shellcheck disable=SC2317,SC2329
dig() { return 0; }
# shellcheck disable=SC2317,SC2329
curl() {
  case "$*" in
    *api.ipify.org*) printf '192.0.2.1' ;;
    *ip-api.com*) printf '{}' ;;
    *) return 1 ;;
  esac
}
export -f ip dig curl
for run in 1 2; do
  # Один экзит, без домена/бота/бэкапа/YouTube; пароль панели сгенерирует деплой.
  printf '%s\n' 1 test-entry-password 198.51.100.2 test-exit-password de \
    '' '' '' n n n n y | bash "$FIXTURE/setup.sh" > "$FIXTURE/output" 2>&1 || {
      cat "$FIXTURE/output"; exit 1;
    }
  grep -F '┌────────────────────────────────────────────┐' "$FIXTURE/output" >/dev/null
  grep -F 'Входная нода:  192.0.2.1' "$FIXTURE/output" >/dev/null
  grep -F 'Выход de: 198.51.100.2' "$FIXTURE/output" >/dev/null
  test "$(stat -c %a "$FIXTURE/config.yaml")" = 600
  test -s "$FIXTURE/infra/envs/all.env"
  grep -Fx 'EXIT_TAGS="de"' "$FIXTURE/infra/envs/exits.env" >/dev/null
  echo "setup.sh save/repeat $run: OK"
done
python3 --version
