#!/usr/bin/env bash
# Комплект строится на том же FS; до проверки рабочий каталог не меняется.
# Блокировка лежит СНАРУЖИ заменяемого каталога (OpenVPN делит её с панелью).
pki_transaction() (
  set -euo pipefail
  umask 077
  local target=$1 builder=$2 work
  shift 2
  mkdir -p "$(dirname "$target")"
  exec {pki_lock}>"$(dirname "$target")/.$(basename "$target").lock"
  flock -x "$pki_lock"
  work=$(mktemp -d "${target}.tmp.XXXXXXXX")
  trap 'rm -rf -- "$work"' EXIT
  if [[ -d "$target" ]]; then
    cp -a --no-preserve=mode "$target/." "$work/"
  fi
  "$builder" "$work" "$@"
  # renameat2(RENAME_EXCHANGE) заменяет и непустой каталог одним syscall.
  # Нет окна между удалением старого комплекта и публикацией нового.
  # После exchange в work остаётся старый комплект, его уберёт EXIT trap.
  python3 - "$work" "$target" <<'PY'
import ctypes
import os
import sys

source, target = sys.argv[1:]
if os.path.exists(target):
    os.chmod(source, os.stat(target).st_mode & 0o777)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)
else:
    os.rename(source, target)
PY
)

pki_key_valid() {
  openssl pkey -in "$1" -check -noout >/dev/null 2>&1
}

pki_cert_valid() {
  openssl x509 -in "$1" -noout -checkend 0 >/dev/null 2>&1
}

pki_pair_valid() {
  pki_key_valid "$1" && pki_cert_valid "$2" || return 1
  [[ $(openssl pkey -in "$1" -pubout 2>/dev/null) == \
     "$(openssl x509 -in "$2" -pubkey -noout 2>/dev/null)" ]]
}

# Повреждённый ключ нельзя молча заменить при существующем сертификате:
# это может быть действующая идентичность, требующая восстановления из backup.
pki_require_key() {
  local key=$1 cert=$2
  pki_key_valid "$key" && return 0
  if pki_cert_valid "$cert"; then
    echo "PKI: valid certificate but missing/invalid private key: $key" >&2
    return 1
  fi
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$key"
}

pki_check_pair() {
  if ! pki_pair_valid "$1" "$2"; then
    echo "PKI: invalid or mismatched key/certificate: $2" >&2
    return 1
  fi
}

pki_tls_crypt_valid() {
  # 2048 бит, формат --genkey secret/tls-crypt. Проверка не включает
  # устаревший static-key режим (--secret), отключённый по умолчанию в 2.7.
  awk '
    /^#/ || /^[[:space:]]*$/ { next }
    $0 == "-----BEGIN OpenVPN Static key V1-----" && state == 0 { state = 1; next }
    $0 == "-----END OpenVPN Static key V1-----" && state == 1 { state = 2; next }
    state == 1 && /^[0-9a-fA-F]+$/ { size += length($0); next }
    { bad = 1 }
    END { exit !(state == 2 && size == 512 && !bad) }
  ' "$1" 2>/dev/null
}
