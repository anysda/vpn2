#!/usr/bin/env bash
# Регресс выбора APT-репозитория и подготовки минимального Debian/Ubuntu.
# Только временное дерево и подставные команды, root и сеть не нужны.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/etc/apt/sources.list.d" "$FIXTURE/etc/apt/keyrings"
sed "s|/etc/|$FIXTURE/etc/|g" "$ROOT/infra/lib/anysda-svc.sh" > "$FIXTURE/svc.sh"
# shellcheck source=/dev/null
source "$FIXTURE/svc.sh"

# Подставные внешние команды вызываются из подключённой библиотеки.
# shellcheck disable=SC2317,SC2329
apt_have() { return 0; }
# shellcheck disable=SC2317,SC2329
apt_get() { echo 'unexpected apt call' >&2; return 1; }
# shellcheck disable=SC2317,SC2329
dpkg() { [[ "$*" == --print-architecture ]] && echo amd64; }
# shellcheck disable=SC2317,SC2329
curl() { printf '%s\n' "$*" >> "$FIXTURE/curl.calls"; echo public-key; }
# shellcheck disable=SC2317,SC2329
gpg() { [[ "$1 $2 $3" == '--dearmor --yes -o' ]]; command cat > "$4"; }

for release in 'debian trixie' 'ubuntu noble' 'ubuntu resolute'; do
  read -r distro codename <<<"$release"
  printf 'ID=%s\nVERSION_CODENAME=%s\n' "$distro" "$codename" > "$FIXTURE/etc/os-release"
  rm -f "$FIXTURE/etc/apt/keyrings/docker.gpg" "$FIXTURE/curl.calls"
  docker_apt_source
  expected="deb [arch=amd64 signed-by=$FIXTURE/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$distro $codename stable"
  actual=$(cat "$FIXTURE/etc/apt/sources.list.d/docker.list")
  [[ "$actual" == "$expected" ]]
  [[ "$(cat "$FIXTURE/curl.calls")" == "-fsSL https://download.docker.com/linux/$distro/gpg" ]]
  # Проверяем и содержимое, и отсутствие перезаписи на повторе.
  touch -t 200001010000 "$FIXTURE/etc/apt/sources.list.d/docker.list" "$FIXTURE/etc/apt/keyrings/docker.gpg"
  before=$(stat -c '%Y' "$FIXTURE/etc/apt/sources.list.d/docker.list" "$FIXTURE/etc/apt/keyrings/docker.gpg")
  docker_apt_source
  [[ "$(stat -c '%Y' "$FIXTURE/etc/apt/sources.list.d/docker.list" "$FIXTURE/etc/apt/keyrings/docker.gpg")" == "$before" ]]
  [[ $(wc -l < "$FIXTURE/curl.calls") == 1 ]]
  printf '%s\n' "${actual//$FIXTURE/}"
done
for release in 'ID=other' 'ID=debian'; do
  printf '%s\n' "$release" > "$FIXTURE/etc/os-release"
  if docker_apt_source; then echo 'invalid os-release accepted' >&2; exit 1; fi
  [[ "$(cat "$FIXTURE/etc/apt/sources.list.d/docker.list")" == "$actual" ]]
done

# На оркестраторе проверяются команды, APT нужны имена пакетов.
eval "$(sed -n '/^install_prereqs() {$/,/^}$/p' "$ROOT/infra/deploy.sh")"
export C_B= C_END= C_G=
# shellcheck disable=SC2317,SC2329
command() {
  if [[ "$*" == '-v ssh' || "$*" == '-v scp' ]]; then return 1; fi
  builtin command "$@"
}
# shellcheck disable=SC2317,SC2329
_install_prereq_pkgs() {
  [[ " $* " == *' openssh-client '* && " $* " != *' scp '* && " $* " != *' ssh '* ]]
  echo 'SSH package mapping: OK'
}
install_prereqs

# Настоящая POSIX-блокировка: проверяем fallback при отсутствии fuser,
# затем её освобождение. Держит родительский процесс, фоновых jobs нет.
python3 - "$FIXTURE/svc.sh" "$FIXTURE/apt.lock" <<'PY'
import fcntl
import os
import subprocess
import sys

probe = '''
source "$1"
APT_LOCKS=("$2")
command() {
  [[ "$*" != '-v fuser' ]] || return 1
  builtin command "$@"
}
apt_lock_holders
'''
with open(sys.argv[2], 'w') as lock:
    fcntl.lockf(lock, fcntl.LOCK_EX)
    result = subprocess.run(['bash', '-c', probe, 'lock-test', *sys.argv[1:]],
                            capture_output=True, text=True, check=True)
    assert str(os.getpid()) in result.stdout.split(), result.stdout
    fcntl.lockf(lock, fcntl.LOCK_UN)
    result = subprocess.run(['bash', '-c', probe, 'lock-test', *sys.argv[1:]],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and not result.stdout, result
print('APT lock without fuser: held/released OK')
PY
echo 'Debian/Ubuntu compatibility: OK'
