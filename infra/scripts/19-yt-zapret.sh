#!/usr/bin/env bash
# Stage 19 — YouTube мимо экзитов: DPI-десинк (nfqws2) на entry-ноде.
#
# Зачем. YouTube не крутит рекламу на российских IP. Гоняя его через
# зарубежный экзит, мы своими руками включаем рекламу и тратим канал экзита на
# самый тяжёлый трафик в доме. Правильный путь — выпускать YouTube прямо с
# московской entry-ноды. Мешает ровно одно: DPI провайдера режет TLS к
# youtube.com. Это лечится десинком (zapret2/nfqws2), а не туннелем.
#
# Схема (та же, что дома на CT302, но на локально исходящем трафике):
#   sing-box (стадия 20) выдаёт YouTube-соединениям outbound `youtube-ru`
#   с routing_mark=$YT_MARK  →  ядро ставит метку на сокет  →  nft-цепочки
#   ниже забирают в nfqueue РОВНО помеченный TCP/443  →  nfqws2 режет
#   ClientHello  →  DPI не собирает SNI  →  наружу через WAN с РФ-адресом.
#
# Метка — единственный признак отбора. Никаких списков IP/CIDR вести не надо:
# что считать YouTube, решает роутер (домены + geosite:youtube), а nft просто
# уважает его решение. Дома этот же результат достигается списками сетей,
# которые протухают молча — здесь этой проблемы нет by design.
#
# Стадия ru-only и идемпотентна. YT_ROUTE != zapret → аккуратно снимает
# развёрнутое (юниты, nft-таблицу, sysctl) и выходит: выключение — такая же
# штатная операция, как включение.
#
# ВАЖНО: стадия обязана идти ДО 20-ru-router. Сначала готовим путь десинка,
# потом sing-box начинает на него отправлять трафик. Обратный порядок = окно,
# в котором YouTube уже уехал на РФ-выход, а обходить DPI ещё нечем.

set -euo pipefail

[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"

case "$HOST_TAG" in ru) ;; *) echo "[$HOST_TAG] 19-yt-zapret is ru-only — skipping"; exit 0;; esac

STAMP_DIR=/var/anysda/.stamps
STAGE='19-yt-zapret'

YT_ROUTE="${YT_ROUTE:-off}"
YT_MARK="${YT_MARK:-256}"
YT_ZAPRET_QUEUE="${YT_ZAPRET_QUEUE:-200}"
YT_ZAPRET_REPO="${YT_ZAPRET_REPO:-https://github.com/bol-van/zapret2}"
# Пин коммита — воспроизводимость сборки. Тот же, что проверен дома (CT302).
YT_ZAPRET_REF="${YT_ZAPRET_REF:-2c21faa80e1acb71ddceb8b49176f266b7d33f05}"
YT_ZAPRET_BASE="${YT_ZAPRET_BASE:-/opt/zapret2}"
YT_ZAPRET_BIN="$YT_ZAPRET_BASE/binaries/my/nfqws2"
# Стратегия десинка. Дома проверено экспериментально (31-07-2026): наш DPI
# берёт ТОЛЬКО split; fake (в т.ч. sni=www.google.com) и circular — хуже или
# не берут вовсе, не возвращать. wssize — анти-троттл (лечит замедление, а не
# блокировку). Если DPI у провайдера entry другой — подбирать через
# $YT_ZAPRET_BASE/blockcheck2.sh и переопределять YT_ZAPRET_STRATEGY.
YT_ZAPRET_STRATEGY="${YT_ZAPRET_STRATEGY:---lua-desync=wssize:wsize=1:scale=6 --payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1}"
# TSO/GSO на WAN: при офлоаде ядро может отдать в очередь склеенный
# super-пакет, и резать ClientHello будет нечего. На практике ClientHello
# меньше MSS и всё работает с офлоадом (проверяется A/B-тестом ниже), а
# выключение офлоада бьёт по throughput ВСЕЙ ноды — поэтому дефолт `keep`.
# Первое, что стоит попробовать, если A/B не проходит: YT_ZAPRET_OFFLOAD=off.
YT_ZAPRET_OFFLOAD="${YT_ZAPRET_OFFLOAD:-keep}"

DIAG_USER='ytzdiag'
NFT_CONF='/etc/nftables-yt-zapret.conf'
SYSCTL_CONF='/etc/sysctl.d/97-anysda-yt-zapret.conf'
QUIC_NFT_CONF='/etc/nftables-yt-quic.conf'
QUIC_PREFIXES='/var/lib/anysda/google-prefixes.json'

WAN_IFACE=$(ip -4 -o route show default | awk '{print $5; exit}')
: "${WAN_IFACE:?не удалось определить WAN-интерфейс}"

mkdir -p "$STAMP_DIR"

# ----------------------------------------------------------------------------
# 0. Выключено → снять всё развёрнутое и выйти
# ----------------------------------------------------------------------------
teardown() {
  echo "[$HOST_TAG] YT_ROUTE=$YT_ROUTE — снимаю десинк (если был развёрнут)"
  for _u in anysda-yt-nfqws anysda-yt-nft anysda-yt-offload anysda-yt-quic; do
    systemctl disable --now "$_u.service" >/dev/null 2>&1 || true
  done
  systemctl disable --now anysda-yt-quic-refresh.timer >/dev/null 2>&1 || true
  nft delete table inet ytzapret >/dev/null 2>&1 || true
  nft delete table inet anysda_ytquic >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/anysda-yt-nfqws.service \
        /etc/systemd/system/anysda-yt-nft.service \
        /etc/systemd/system/anysda-yt-offload.service \
        /etc/systemd/system/anysda-yt-quic.service \
        /etc/systemd/system/anysda-yt-quic-refresh.service \
        /etc/systemd/system/anysda-yt-quic-refresh.timer \
        /usr/local/sbin/anysda-yt-quic-refresh.sh \
        /usr/local/bin/anysda-yt-check \
        "$NFT_CONF" "$SYSCTL_CONF" "$QUIC_NFT_CONF"
  systemctl daemon-reload
  # Исходники и бинарь НЕ трогаем: пересборка занимает минуты, а место они
  # занимают копеечное. Обратное включение должно быть мгновенным.
  rm -f "$STAMP_DIR/$STAGE"
  echo "[$HOST_TAG] $STAGE: десинк снят"
}

if [[ "$YT_ROUTE" != "zapret" ]]; then
  teardown
  exit 0
fi

echo "[$HOST_TAG] $STAGE: WAN=$WAN_IFACE mark=$YT_MARK queue=$YT_ZAPRET_QUEUE"

# ----------------------------------------------------------------------------
# 1. Зависимости сборки nfqws2
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [1/7] apt deps"
export DEBIAN_FRONTEND=noninteractive
NEED_PKGS=(build-essential git ca-certificates libcap-dev libnetfilter-queue-dev
           libnfnetlink-dev libmnl-dev zlib1g-dev libluajit-5.1-dev luajit
           nftables ethtool curl)
MISSING=()
for p in "${NEED_PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "[$HOST_TAG]   ставлю: ${MISSING[*]}"
  apt-get update -qq
  apt-get install -y -qq "${MISSING[@]}" >/dev/null
fi

# ----------------------------------------------------------------------------
# 2. Исходники zapret2 на пиннутом коммите
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [2/7] исходники ($YT_ZAPRET_REF)"
SRC_CHANGED=0
if [[ ! -d "$YT_ZAPRET_BASE/.git" ]]; then
  rm -rf "$YT_ZAPRET_BASE"
  git clone -q "$YT_ZAPRET_REPO" "$YT_ZAPRET_BASE"
  git -C "$YT_ZAPRET_BASE" checkout -q "$YT_ZAPRET_REF"
  SRC_CHANGED=1
elif [[ "$(git -C "$YT_ZAPRET_BASE" rev-parse HEAD)" != "$YT_ZAPRET_REF" ]]; then
  git -C "$YT_ZAPRET_BASE" fetch -q --all
  git -C "$YT_ZAPRET_BASE" checkout -q "$YT_ZAPRET_REF"
  SRC_CHANGED=1
fi
echo "[$HOST_TAG]   HEAD=$(git -C "$YT_ZAPRET_BASE" rev-parse --short HEAD)"

# ----------------------------------------------------------------------------
# 3. Сборка nfqws2
# ----------------------------------------------------------------------------
# ⚠️ Никаких `make -q`/`make -n` по этому Makefile. Цель `all` зависит от
# `clean`, чей рецепт — одна строка с $(MAKE), а такие строки GNU make
# выполняет ДАЖЕ в «вопросительных» режимах (GNU Make §9.3). Итог «сухого»
# прогона: rm -rf binaries/my и ничего не собрано, демон живёт на удалённом
# inode до первого рестарта. Дома это уже стоило пропавшего бинаря —
# infra/reports/01-08-2026-zapret2-binary-vanished.md.
echo "[$HOST_TAG] [3/7] сборка nfqws2"
if [[ $SRC_CHANGED -eq 1 || ! -x "$YT_ZAPRET_BIN" ]]; then
  make -C "$YT_ZAPRET_BASE" >/tmp/anysda-nfqws2-build.log 2>&1 || {
    echo "[$HOST_TAG] сборка упала, хвост лога:"; tail -25 /tmp/anysda-nfqws2-build.log; exit 1; }
fi
[[ -x "$YT_ZAPRET_BIN" ]] || { echo "[$HOST_TAG] $YT_ZAPRET_BIN не собрался"; exit 1; }
"$YT_ZAPRET_BIN" --version 2>&1 | head -1 | sed "s/^/[$HOST_TAG]   /"

LUA_INIT=""
for _l in zapret-lib zapret-antidpi zapret-auto; do
  _f="$YT_ZAPRET_BASE/lua/${_l}.lua"
  [[ -f "$_f" ]] || { echo "[$HOST_TAG] нет lua-модуля $_f — nfqws2 упадёт с 'desync function does not exist'"; exit 1; }
  LUA_INIT="$LUA_INIT --lua-init=@$_f"
done

# ----------------------------------------------------------------------------
# 4. Диагностический юзер: единственный способ честно проверить десинк
# ----------------------------------------------------------------------------
# Проверять десинк «жив ли юнит» бессмысленно — nfqws2 поднимается и с
# нерабочей стратегией. Нужен A/B: один и тот же хост по помеченному пути и
# мимо него. Помеченный путь снаружи не воспроизвести (метку ставит sing-box
# на свой сокет), поэтому заводим системного юзера, чей исходящий трафик nft
# метит той же меткой. curl из-под него = ровно клиентский путь.
echo "[$HOST_TAG] [4/7] диаг-юзер $DIAG_USER"
id -u "$DIAG_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$DIAG_USER"
DIAG_UID=$(id -u "$DIAG_USER")

# ----------------------------------------------------------------------------
# 5. nftables + sysctl
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [5/7] nftables"
cat > "$SYSCTL_CONF" <<'EOF'
# Managed by anysda-vpn2 stage 19-yt-zapret.
# Строгий conntrack считает сегменты десинка INVALID и роняет их — без этого
# обход не работает вообще, а выглядит как «десинк применяется, толку ноль».
net.netfilter.nf_conntrack_tcp_be_liberal=1
EOF
sysctl -p "$SYSCTL_CONF" >/dev/null

cat > "$NFT_CONF" <<EOF
#!/usr/sbin/nft -f
# Managed by anysda-vpn2 stage 19-yt-zapret. Не редактировать руками —
# перезаписывается каждым прогоном стадии.
#
# Отбор — ТОЛЬКО по метке $YT_MARK, которую sing-box ставит на сокеты outbound
# youtube-ru. Остальной трафик ноды (панель, ACME, docker pull, hy2 к экзитам)
# в очередь не попадает ни при каких условиях.

table inet ytzapret
delete table inet ytzapret

table inet ytzapret {
    # Метим исходящее диаг-юзера — это A/B-пробник, см. anysda-yt-check.
    chain out {
        type filter hook output priority -150; policy accept;
        meta skuid $DIAG_UID meta mark set $YT_MARK counter
    }

    chain post {
        type filter hook postrouting priority 101; policy accept;
        # Метка живёт на исходящем пакете, но ответы сервера приходят без неё.
        # Переносим её в conntrack — по ct mark ловим обратное направление.
        meta mark $YT_MARK ct mark set $YT_MARK counter
        # Десинк нужен только в начале соединения: ClientHello и первые
        # сегменты. Остальное идёт мимо очереди на полной скорости.
        meta mark $YT_MARK oifname "$WAN_IFACE" tcp dport 443 ct original packets 1-12 counter queue num $YT_ZAPRET_QUEUE bypass
    }

    chain pre {
        type filter hook prerouting priority -101; policy accept;
        # Ответы сервера. БЕЗ этой цепочки wssize не видит SYN,ACK и
        # реассемблинг не заводится — обход молча перестаёт работать.
        ct mark $YT_MARK iifname "$WAN_IFACE" tcp sport 443 ct reply packets 1-12 counter queue num $YT_ZAPRET_QUEUE bypass
    }

    chain predefrag {
        type filter hook output priority -401; policy accept;
        # Пакеты, которые nfqws2 генерит сам, помечены 0x40000000. Без notrack
        # строгий conntrack дропает их как INVALID.
        mark and 0x40000000 != 0x00000000 notrack
    }
}
EOF
nft -c -f "$NFT_CONF"

cat > /etc/systemd/system/anysda-yt-nft.service <<EOF
[Unit]
Description=anysda-vpn2 — nft-цепочки десинка YouTube (+ conntrack be_liberal)
After=network-online.target
Wants=network-online.target
Before=anysda-yt-nfqws.service

[Service]
Type=oneshot
RemainAfterExit=yes
# be_liberal сбрасывается при выгрузке модуля conntrack — ставим тут же.
ExecStart=/sbin/sysctl -w net.netfilter.nf_conntrack_tcp_be_liberal=1
ExecStart=/usr/sbin/nft -f $NFT_CONF
ExecReload=/usr/sbin/nft -f $NFT_CONF
# Сносим ТОЛЬКО свою таблицу — чужие правила (ufw, docker, fail2ban) не трогаем.
ExecStop=/bin/sh -c '/usr/sbin/nft delete table inet ytzapret 2>/dev/null; true'

[Install]
WantedBy=multi-user.target
EOF

# TSO/GSO — только если явно попросили.
if [[ "$YT_ZAPRET_OFFLOAD" == "off" ]]; then
  cat > /etc/systemd/system/anysda-yt-offload.service <<EOF
[Unit]
Description=anysda-vpn2 — выключить TSO/GSO/GRO на $WAN_IFACE (десинк YouTube)
After=network-online.target
Wants=network-online.target
Before=anysda-yt-nfqws.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/ethtool -K $WAN_IFACE tso off gso off gro off
ExecStop=/usr/sbin/ethtool -K $WAN_IFACE tso on gso on gro on

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now anysda-yt-offload.service >/dev/null 2>&1 || true
  echo "[$HOST_TAG]   offload off на $WAN_IFACE"
else
  if [[ -f /etc/systemd/system/anysda-yt-offload.service ]]; then
    systemctl disable --now anysda-yt-offload.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/anysda-yt-offload.service
  fi
fi


# ----------------------------------------------------------------------------
# 5b. Второй рубеж: QUIC (UDP/443) к Google режем по IP-принадлежности
# ----------------------------------------------------------------------------
# Первый рубеж — правило sing-box `domain_suffix=[youtube...] network=udp
# port=443 => block-out`. Оно работает по SNI из QUIC Initial и ловит
# подавляющее большинство случаев (проверено: www.youtube.com, googlevideo.com,
# i.ytimg.com, yt3.ggpht.com, youtubei.googleapis.com — все blocked).
#
# Но оно верно ровно пока сниффер видит домен. Фрагментированный ClientHello,
# 0-RTT или обращение на голый IP — домена нет, правило не матчится, QUIC
# утекает мимо десинка. Десинк работает только по TCP, поэтому утечка = YouTube
# без обхода: реклама и потеря российского GGC.
#
# 09-09-2026 это выстрелило на проде: после освобождения UDP/443 (до того порт
# занимал Caddy с h3, и QUIC был мёртв у всех) клиент ушёл качать видео с
# 74.125.108.34 (lcfraa-ak-in-f2.1e100.net) потоками QUIC по 1250 байт.
#
# Цена ошибки несимметрична: лишний раз отрезанный QUIC к Google — это откат
# на TCP, всё работает штатно. Пропущенный — сломанный YouTube у всех сразу.
echo "[$HOST_TAG] [5b/7] блокировка QUIC к Google"
mkdir -p "$(dirname "$QUIC_PREFIXES")"

install -m 0755 /dev/stdin /usr/local/sbin/anysda-yt-quic-refresh.sh <<'QREFRESH'
#!/usr/bin/env bash
# Обновляет список IP-диапазонов Google и перезаливает nft-таблицу.
# Свежий список тянем у Google; если не вышло — остаёмся на том, что уже есть.
# Пустой/битый ответ не должен превращаться в дыру, поэтому файл заменяем
# только после успешной генерации конфига из него.
set -euo pipefail
PREFIXES='/var/lib/anysda/google-prefixes.json'
NFT_CONF='/etc/nftables-yt-quic.conf'
GEN='/usr/local/sbin/gen-yt-quic-nft.py'
TMP=$(mktemp); TMP_NFT=$(mktemp)
trap 'rm -f "$TMP" "$TMP_NFT"' EXIT

if curl -fsS --max-time 30 https://www.gstatic.com/ipranges/goog.json -o "$TMP" \
   && python3 "$GEN" "$TMP" > "$TMP_NFT" 2>/dev/null \
   && nft -c -f "$TMP_NFT"; then
  install -m 0644 "$TMP" "$PREFIXES"
  echo "список обновлён с gstatic"
else
  echo "gstatic недоступен или ответ битый — остаюсь на сохранённом списке" >&2
  python3 "$GEN" "$PREFIXES" > "$TMP_NFT"
  nft -c -f "$TMP_NFT"
fi

install -m 0644 "$TMP_NFT" "$NFT_CONF"
nft -f "$NFT_CONF"
echo "QUIC-блокировка применена: $(nft list table inet anysda_ytquic | grep -c 'udp dport 443') правил"
QREFRESH

install -m 0755 /tmp/anysda/gen-yt-quic-nft.py /usr/local/sbin/gen-yt-quic-nft.py

# Снапшот из репы — стартовое наполнение. Дальше его обновляет таймер, но
# даже если gstatic недоступен в момент прогона стадии, дыры не будет.
[[ -f "$QUIC_PREFIXES" ]] || install -m 0644 /tmp/anysda/google-prefixes.json "$QUIC_PREFIXES"

cat > /etc/systemd/system/anysda-yt-quic.service <<'EOF'
[Unit]
Description=anysda-vpn2 — nft-блокировка QUIC (UDP/443) к Google
After=network-online.target
Wants=network-online.target
Before=sing-box.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/anysda-yt-quic-refresh.sh
ExecStop=/usr/sbin/nft delete table inet anysda_ytquic

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/anysda-yt-quic-refresh.service <<'EOF'
[Unit]
Description=anysda-vpn2 — обновление списка IP-диапазонов Google

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/anysda-yt-quic-refresh.sh
EOF

cat > /etc/systemd/system/anysda-yt-quic-refresh.timer <<'EOF'
[Unit]
Description=anysda-vpn2 — обновлять диапазоны Google раз в сутки

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now anysda-yt-quic.service >/dev/null 2>&1 || true
systemctl restart anysda-yt-quic.service
systemctl enable --now anysda-yt-quic-refresh.timer >/dev/null 2>&1 || true
echo "[$HOST_TAG]   $(nft list table inet anysda_ytquic 2>/dev/null | grep -c 'udp dport 443') правил, префиксов: $(python3 -c "import json;d=json.load(open('$QUIC_PREFIXES'));print(len(d.get('prefixes',[])))")"

# ----------------------------------------------------------------------------
# 6. Демон nfqws2
# ----------------------------------------------------------------------------
echo "[$HOST_TAG] [6/7] nfqws2 service"
cat > /etc/systemd/system/anysda-yt-nfqws.service <<EOF
[Unit]
Description=anysda-vpn2 — nfqws2 (DPI-десинк YouTube на РФ-выходе)
After=network-online.target anysda-yt-nft.service
Wants=network-online.target
Requires=anysda-yt-nft.service
# Демон обязан подниматься всегда: при его смерти правило queue ... bypass
# пропускает трафик без десинка, то есть YouTube молча упирается в DPI.
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=$YT_ZAPRET_BIN --qnum=$YT_ZAPRET_QUEUE$LUA_INIT $YT_ZAPRET_STRATEGY
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anysda-yt-nft.service anysda-yt-nfqws.service >/dev/null 2>&1
systemctl restart anysda-yt-nft.service
systemctl restart anysda-yt-nfqws.service
sleep 2

# Инвариант: демон исполняет ИМЕННО тот файл, что лежит на диске. После
# пересборки (make = clean + build) живой процесс может держать удалённый
# inode — «работает, но не переживёт рестарта».
_pid=$(systemctl show -p MainPID --value anysda-yt-nfqws.service 2>/dev/null || echo 0)
if [[ -n "$_pid" && "$_pid" != "0" ]]; then
  _run=$(stat -Lc %i "/proc/$_pid/exe" 2>/dev/null || echo none)
  _disk=$(stat -c %i "$YT_ZAPRET_BIN" 2>/dev/null || echo none)
  if [[ "$_run" != "$_disk" ]]; then
    echo "[$HOST_TAG]   демон на устаревшем inode — рестарт"
    systemctl restart anysda-yt-nfqws.service
    sleep 2
  fi
fi
systemctl is-active anysda-yt-nft.service anysda-yt-nfqws.service | tr '\n' ' ' | sed "s/^/[$HOST_TAG]   nft+nfqws: /"; echo

# ----------------------------------------------------------------------------
# 7. A/B-проверка: тот же хост по помеченному пути и мимо него
# ----------------------------------------------------------------------------
install -m 0755 /dev/stdin /usr/local/bin/anysda-yt-check <<EOF
#!/usr/bin/env bash
# A/B-проверка десинка YouTube. Статус юнита ничего не доказывает — nfqws2
# поднимается и с нерабочей стратегией. Сравниваем ОДИН И ТОТ ЖЕ хост:
#   A — из-под $DIAG_USER: nft метит его трафик меткой $YT_MARK → десинк;
#   B — из-под root: метки нет → чистый путь провайдера (контроль).
# Коды выхода: 0 — путь десинка живой; 1 — не живой (обход не работает).
set -u
HOST="\${1:-https://www.youtube.com/}"
TMO="\${YT_CHECK_TIMEOUT:-15}"

# curl сам печатает 000 при неудаче, поэтому вторым 000 не дополняем —
# иначе в отчёте получается «000000» и непонятно, что это.
probe() { curl -4 -sS -o /dev/null -m "\$TMO" -w '%{http_code}' "\$HOST" 2>/dev/null; }

A=\$(setpriv --reuid=$DIAG_UID --regid=$(id -g "$DIAG_USER") --clear-groups \\
      curl -4 -sS -o /dev/null -m "\$TMO" -w '%{http_code}' "\$HOST" 2>/dev/null)
B=\$(probe)
A=\${A:-000}; B=\${B:-000}

echo "zapret (метка $YT_MARK): \$A"
echo "контроль (без метки):    \$B"

if [[ "\$A" =~ ^(200|204|30[0-9])\$ ]]; then
  if [[ "\$B" =~ ^(200|204|30[0-9])\$ ]]; then
    echo "OK: путь десинка работает. Контроль тоже прошёл — DPI сейчас не режет,"
    echo "    тест десинк не доказывает, но и не опровергает."
  else
    echo "OK: путь десинка работает, прямой путь режется DPI — обход делает своё дело."
  fi
  exit 0
fi
echo "FAIL: YouTube не открывается по помеченному пути — клиенты его не увидят."
echo "  журнал:   journalctl -u anysda-yt-nfqws -n 30 --no-pager"
echo "  счётчики: nft list table inet ytzapret"
echo "  подбор стратегии: $YT_ZAPRET_BASE/blockcheck2.sh"
echo "  первое, что пробовать: YT_ZAPRET_OFFLOAD=off (TSO/GSO склеивает ClientHello)"
exit 1
EOF

echo "[$HOST_TAG] [7/7] A/B-проверка"
if /usr/local/bin/anysda-yt-check 2>&1 | sed "s/^/[$HOST_TAG]   /"; then
  touch "$STAMP_DIR/$STAGE"
  echo "[$HOST_TAG] $STAGE done"
else
  echo "[$HOST_TAG] $STAGE: десинк НЕ работает — стадия 20 не должна уводить YouTube на РФ-выход."
  echo "[$HOST_TAG] Деплой остановлен намеренно: иначе YouTube ляжет у всех клиентов."
  exit 1
fi
