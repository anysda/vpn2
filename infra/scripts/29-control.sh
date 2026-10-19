#!/usr/bin/env bash
# Local control API; switching the panel to it is a separate migration.
set -euo pipefail
[[ -n "${1:-}" && -f "$1" ]] && source "$1"
: "${HOST_TAG:?}"
if [[ "$HOST_TAG" != ru ]]; then
  echo "[$HOST_TAG] stage 29-control: skipped (only RU)"
  exit 0
fi

CONTROL_SOURCE=$(dirname "$0")/control
[[ -d "$CONTROL_SOURCE" ]] || CONTROL_SOURCE=$(dirname "$0")/../control
POOL_SOURCE=$(dirname "$0")/vpn-pools.json
[[ -f "$POOL_SOURCE" ]] || POOL_SOURCE=$(dirname "$0")/../../app/shared/vpn-pools.json
command -v python3 >/dev/null
getent group vpn2-control >/dev/null || groupadd --system vpn2-control
id vpn2-control >/dev/null 2>&1 || \
  useradd --system --gid vpn2-control --home-dir /nonexistent --shell /usr/sbin/nologin vpn2-control
id vpn2-panel >/dev/null 2>&1 || \
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin vpn2-panel
usermod -a -G vpn2-control vpn2-panel
id vpn2-bot >/dev/null 2>&1 || \
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin vpn2-bot

# App-owned data only. Legacy paths remain readable by backup and old images.
install -d -o vpn2-panel -g vpn2-panel -m 0700 /var/lib/anysda-vpn2 /var/lib/anysda-vpn2/runtime
for name in sso-runtime.json telegram-runtime.json; do
  old=/etc/anysda/$name
  new=/var/lib/anysda-vpn2/runtime/$name
  if [[ -f "$old" && ! -L "$old" ]]; then
    [[ -e "$new" ]] || cp -p "$old" "$new"
  fi
  ln -sfn "$new" "$old"
done
chown -hR vpn2-panel:vpn2-panel /var/lib/anysda-vpn2/runtime
for name in db.sqlite db.sqlite-wal db.sqlite-shm access-worker.sqlite access-worker.sqlite-journal; do
  [[ ! -e /var/lib/anysda-vpn2/$name ]] || chown -h vpn2-panel:vpn2-panel "/var/lib/anysda-vpn2/$name"
done
if [[ -f /var/lib/anysda-vpn2/runtime/telegram-runtime.json ]]; then
  chmod 0640 /var/lib/anysda-vpn2/runtime/telegram-runtime.json
fi

install -d -o root -g root -m 0755 /usr/local/lib/vpn2-control /etc/vpn2-control
for source in contract.py backend.py server.py launcher.py; do
  install -o root -g root -m 0644 "$CONTROL_SOURCE/$source" /usr/local/lib/vpn2-control/
done
install -o root -g root -m 0644 "$POOL_SOURCE" /usr/local/lib/vpn2-control/vpn-pools.json
install -o root -g root -m 0755 "$CONTROL_SOURCE/apply.py" /usr/local/sbin/vpn2-apply
# Finish an apply interrupted since the last deploy (3: the previous config
# was restored), then record what runs now as the last good config.
for resource in sing-box openvpn ikev2 wg; do
  rc=0
  /usr/local/sbin/vpn2-apply resume "$resource" || rc=$?
  [[ $rc == 0 || $rc == 3 ]] || exit "$rc"
  /usr/local/sbin/vpn2-apply mark "$resource" >/dev/null 2>&1 ||
    echo "[$HOST_TAG] vpn2-apply: $resource is not running, last good not recorded"
done
install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control.service" /etc/systemd/system/
install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control.socket" /etc/systemd/system/
install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control-expiry.service" /etc/systemd/system/
install -o root -g root -m 0644 "$CONTROL_SOURCE/vpn2-control-expiry.timer" /etc/systemd/system/
# A host UID is required for a user-namespace mapping. Never accept a UID in
# a request. Preserve an explicit root-managed mapping across redeployments.
if [[ ! -f /etc/vpn2-control/callers.json ]]; then
  printf '[0,%s]\n' "$(id -u vpn2-panel)" > /etc/vpn2-control/callers.json
fi
chown root:root /etc/vpn2-control/callers.json
chmod 0644 /etc/vpn2-control/callers.json
for directory in /etc/wireguard /etc/openvpn/server /etc/swanctl/conf.d; do
  [[ -d "$directory" ]] || install -d -o root -g root -m 0700 "$directory"
done
install -d -o vpn2-control -g vpn2-control -m 0700 /var/lib/vpn2-control
systemctl daemon-reload
systemctl enable --now vpn2-control.socket
systemctl restart vpn2-control.service
/usr/bin/python3 -I /usr/local/lib/vpn2-control/launcher.py --probe
# Expiry is enforced here too, so a stopped panel does not keep access open.
systemctl enable --now vpn2-control-expiry.timer
install -d -m 0755 /etc/vpn2
printf '1\n' > /etc/vpn2/control-api-version
install -d /var/anysda/.stamps
touch /var/anysda/.stamps/29-control
echo "[$HOST_TAG] 29-control done"
