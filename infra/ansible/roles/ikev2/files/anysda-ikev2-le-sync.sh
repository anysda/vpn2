#!/usr/bin/env bash
# Раскладка LE-cert Caddy в swanctl: leaf → x509, промежуточные → x509ca.
# Зовётся стадией 27-ikev2 и path-юнитом anysda-ikev2-cert-sync (ротация LE).
set -euo pipefail
LE_CERT="$1"
LE_KEY="$2"
DOMAIN="${3:-}"
if [[ -n "$DOMAIN" ]]; then
  exec 8>/run/anysda-ikev2-cert.lock
  flock -x 8
  # Деплой мог удалить/сменить домен, пока старый запуск ждал замок.
  [[ "$(cat /etc/anysda/ikev2-domain 2>/dev/null)" == "$DOMAIN" ]] || exit 0
  # Нет пары / Caddy ещё пишет её: таймер повторит попытку.
  [[ -s "$LE_CERT" && -s "$LE_KEY" ]] || exit 0
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cp "$LE_CERT" "$TMP/fullchain.pem"
cp "$LE_KEY" "$TMP/key.pem"
csplit -sz -f "$TMP/cert-" -b '%02d.pem' "$TMP/fullchain.pem" '/-----BEGIN CERTIFICATE-----/' '{*}'
# Caddy заменяет cert и key отдельно. Не устанавливаем неполную пару,
# просроченный сертификат или сертификат другого домена.
openssl x509 -in "$TMP/cert-00.pem" -noout -checkend 0 >/dev/null
openssl x509 -in "$TMP/cert-00.pem" -pubkey -noout > "$TMP/cert.pub"
openssl pkey -in "$TMP/key.pem" -pubout > "$TMP/key.pub"
cmp -s "$TMP/cert.pub" "$TMP/key.pub"
if [[ -n "$DOMAIN" ]]; then
  openssl x509 -in "$TMP/cert-00.pem" -noout -checkhost "$DOMAIN" | grep 'does match' >/dev/null
  # Отпечаток пишется только после успешного reload; сбой будет повторён.
  DIGEST=$(cat "$TMP/fullchain.pem" "$TMP/key.pem" | sha256sum | cut -d' ' -f1)
  if [[ "$(cat /etc/anysda/ikev2-cert.sha256 2>/dev/null)" == "$DIGEST" &&
        "$(cat /etc/anysda/ikev2-server-host 2>/dev/null)" == "$DOMAIN" &&
        "$(cat /etc/anysda/ikev2-mode 2>/dev/null)" == letsencrypt ]]; then
    exit 0
  fi
fi
install -m 0644 "$TMP/cert-00.pem" /etc/swanctl/x509/anysda-server.crt
install -m 0600 "$TMP/key.pem"     /etc/swanctl/private/anysda-server.key

rm -f /etc/swanctl/x509ca/anysda-le-chain-*.crt
n=1
for f in "$TMP"/cert-*.pem; do
  [[ "$f" == "$TMP/cert-00.pem" ]] && continue
  install -m 0644 "$f" "/etc/swanctl/x509ca/anysda-le-chain-${n}.crt"
  n=$((n + 1))
done

if [[ -n "$DOMAIN" ]]; then
  # anysda.conf не трогаем: деплой с доменом сразу пишет conn на обе цепочки, и пара
  # своего CA в ней остаётся для профилей, выданных до этого сертификата.
  swanctl --load-creds >/dev/null
  # Conn хранит выбранный leaf: одного --load-creds недостаточно.
  swanctl --load-conns >/dev/null
  printf '%s' "$DOMAIN" > /etc/anysda/ikev2-server-host
  printf '%s' letsencrypt > /etc/anysda/ikev2-mode
  printf '%s' "$DIGEST" > /etc/anysda/ikev2-cert.sha256
  echo "IKEv2: mode=letsencrypt server-host=$DOMAIN"
else
  # Стадия сама загрузит creds/conns после создания конфигурации.
  swanctl --load-creds >/dev/null 2>&1 || true
  swanctl --load-conns >/dev/null 2>&1 || true
fi
