#!/usr/bin/env bash
# Экзит, убранный из установки (менеджер установки, deploy exit-remove): с сервера уходят наши службы, файлы,
# правила ufw и ключи entry. Ключи человека и правило ssh остаются, sshd снова пускает по паролю, как до установки.
# Идёт по ssh (bash -s) ключом оркестратора, ключи entry убираются последними. Что не убралось - строкой «left: ...».
set -u
echo 'vpn2-forget: start'
left() { echo "left: $*"; }

units=(sing-box.service node_exporter.service anysda-fq.service caddy.service)
for path in /etc/systemd/system/anysda-* /etc/systemd/system/vpn2-*; do
  [[ -f $path ]] && units+=("${path##*/}")
done
systemctl disable --now "${units[@]}" >/dev/null 2>&1
for unit in "${units[@]}"; do
  if systemctl is-active --quiet "$unit"; then left "служба $unit не остановилась"; fi
done

# Пакеты, что поставила установка, - вместе с их настройками; пакеты ОС остаются.
packages=$(dpkg-query -W -f='${Package} ${db:Status-Status}\n' sing-box caddy docker-ce docker-ce-cli containerd.io \
  docker-compose-plugin docker-buildx-plugin 2>/dev/null | awk '$2 == "installed" {print $1}')
if [[ -n $packages ]]; then
  # shellcheck disable=SC2086 # список пакетов - слова
  DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq $packages >/dev/null 2>&1 || left "пакеты: $packages"
fi

# Автообновления ОС, что деплой поставил на паузу маской --runtime, - сразу назад, без таймера возврата.
systemctl stop anysda-apt-resume.timer >/dev/null 2>&1
systemctl unmask --runtime apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service \
  unattended-upgrades.service >/dev/null 2>&1
for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
  if systemctl is-enabled --quiet "$unit"; then systemctl start "$unit" || left "служба $unit не запустилась"; fi
done

for table in $(nft list tables 2>/dev/null | awk '$3 ~ /^(vpn2|anysda)_/ {print $2 " " $3}' | tr ' ' ':'); do
  nft delete table "${table%%:*}" "${table#*:}" || left "таблица nft ${table#*:}"
done
# Свой /swapfile установка ставит вместе с 60-anysda-swap.conf; чужой swap не трогаем.
if [[ -f /etc/sysctl.d/60-anysda-swap.conf && -f /swapfile ]]; then
  swapoff /swapfile 2>/dev/null
  if ! { sed -i '\|^/swapfile[[:space:]]|d' /etc/fstab && rm -f /swapfile; }; then left /swapfile; fi
fi
# Прежний nftables.conf - на место; правила ufw уже в ядре, перезапуск nftables их бы сбросил.
if [[ -f /etc/nftables.conf.before-vpn2 ]]; then
  mv /etc/nftables.conf.before-vpn2 /etc/nftables.conf || left /etc/nftables.conf
fi

files=(/etc/systemd/system/anysda-* /etc/systemd/system/vpn2-* /etc/systemd/system/sing-box.service*
  /etc/systemd/system/node_exporter.service* /etc/systemd/system/esm-cache.service
  /etc/systemd/system/system.slice.d/zz-vpn2-resources.conf
  /etc/systemd/system/nftables.service.d/10-anysda-preserve.conf /etc/systemd/system/nftables.service.d/90-anysda-hy2hop.conf
  /etc/systemd/networkd.conf.d/60-anysda-foreign.conf /etc/sysctl.d/99-anysda.conf /etc/sysctl.d/60-anysda-swap.conf
  /etc/fail2ban/jail.d/anysda.local /etc/apt/apt.conf.d/*anysda*
  /etc/nftables-hy2hop.conf /etc/polkit-1/rules.d/50-vpn2-emergency-hy2.rules /etc/docker /etc/apt/sources.list.d/docker*
  /etc/apt/keyrings/docker* /etc/sing-box /etc/caddy /etc/anysda /etc/vpn2 /var/anysda /var/lib/anysda* /var/lib/vpn2-*
  /var/lib/sing-box /var/lib/caddy /var/lib/docker /var/lib/containerd /usr/local/lib/vpn2 /usr/local/bin/anysda-*
  /usr/local/bin/wgcf /usr/local/bin/node_exporter /usr/local/bin/sing-box /usr/local/sbin/anysda-* /opt/vpn2
  /opt/vpn2-emergency /opt/containerd /tmp/anysda /run/vpn2-fw-guard)
for path in "${files[@]}"; do
  [[ -e $path ]] && { rm -rf "$path" || left "$path"; }
done
for user in node_exporter vpn2-emergency vpn2-hy2 caddy; do
  getent passwd "$user" >/dev/null && userdel -r "$user" >/dev/null 2>&1
  if getent passwd "$user" >/dev/null; then left "пользователь $user"; fi
done
systemctl daemon-reload
sysctl --system >/dev/null 2>&1
systemctl try-restart systemd-networkd fail2ban >/dev/null 2>&1

# Правила ufw установки - по их комментариям; ssh остаётся, иначе владелец не войдёт.
if command -v ufw >/dev/null; then
  for number in $(ufw status numbered | awk -F'[][]' '/# (hysteria2|vless reality|emergency hysteria2|vpn2 |caddy )/ {print $2+0}' | sort -rn); do
    ufw --force delete "$number" >/dev/null || left "правило ufw $number"
  done
fi

# Вход по паролю - как до установки: без нашей вставки и с умолчанием Ubuntu в основном конфиге.
rm -f /etc/ssh/sshd_config.d/00-anysda.conf /etc/ssh/sshd_config.d/99-anysda-no-penalties.conf
# KbdInteractiveAuthentication no - и умолчание Ubuntu, его не трогаем.
sed -i -e 's/^PasswordAuthentication no$/#PasswordAuthentication yes/' \
  -e 's/^PermitRootLogin prohibit-password$/#PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
if sshd -t; then systemctl try-reload-or-restart ssh >/dev/null 2>&1 || left 'sshd не перечитал конфиг'; else left 'sshd: конфиг не проходит sshd -t'; fi

# Ключи entry (оркестратор, прежний RSA, служебный mgmt): их тела entry передаёт строкой ENTRY_KEYS перед скриптом.
keys=/root/.ssh/authorized_keys
if [[ -f $keys && -n ${ENTRY_KEYS:-} ]]; then
  grep -vF -f <(printf '%s\n' "$ENTRY_KEYS") "$keys" > "$keys.vpn2"
  if ! { chmod 600 "$keys.vpn2" && mv "$keys.vpn2" "$keys"; }; then left "$keys"; fi
fi
echo 'vpn2-forget: done'
