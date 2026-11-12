#!/usr/bin/env bash
# Обновляет список IP-диапазонов Google и перезаливает nft-таблицу.
# Свежий список тянем у Google; если не вышло — остаёмся на том, что уже есть
# (до первого обновления это список из пакета vpn2).
# Пустой/битый ответ не должен превращаться в дыру, поэтому файл заменяем
# только после успешной генерации конфига из него.
set -euo pipefail
PREFIXES='/var/lib/anysda/google-prefixes.json'
NFT_CONF='/etc/nftables-yt-quic.conf'
GEN='/opt/vpn2/venv/bin/vpn2 nft yt-quic'
TMP=$(mktemp); TMP_NFT=$(mktemp)
trap 'rm -f "$TMP" "$TMP_NFT"' EXIT

if curl -fsS --max-time 30 https://www.gstatic.com/ipranges/goog.json -o "$TMP" \
   && $GEN "$TMP" > "$TMP_NFT" 2>/dev/null \
   && nft -c -f "$TMP_NFT"; then
  install -m 0644 "$TMP" "$PREFIXES"
  echo "список обновлён с gstatic"
else
  echo "gstatic недоступен или ответ битый — остаюсь на сохранённом списке" >&2
  [[ -f "$PREFIXES" ]] || PREFIXES=''
  $GEN ${PREFIXES:+"$PREFIXES"} > "$TMP_NFT"
  nft -c -f "$TMP_NFT"
fi

install -m 0644 "$TMP_NFT" "$NFT_CONF"
nft -f "$NFT_CONF"
echo "QUIC-блокировка применена: $(nft list table inet anysda_ytquic | grep -c 'udp dport 443') правил"
