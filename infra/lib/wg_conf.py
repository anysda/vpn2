"""wg0.conf так, как его читает wg (config.c wireguard-tools): секции, AllowedIPs, пиры.

Один разбор для сверки бэкапа (anysda-backup.sh) и проверки трафика (verify-traffic.py),
оба грузят этот файл рядом с собой. Файл, который wg отвергает, - ValueError с «AllowedIPs».
"""
import ipaddress
import re


def wg_sections(text):
    """wg0.conf по секциям: [(секция, [(имя, значение, строка)])].

    Строка до первого `#`, без пробельных символов, имена секций и ключей
    в любом регистре, но только ASCII. Строки до первой секции - в секции ''.
    """
    sections = [('', [])]
    for line in text.split('\n'):
        clean = re.sub(r'[ \t\n\v\f\r]', '', line.split('#', 1)[0])
        name, eq, value = clean.partition('=')
        name = name.encode().lower().decode()
        if clean.startswith('['):
            sections.append((name, []))
        elif eq:
            sections[-1][1].append((name, value, line))
    return sections


def wg_network(entry):
    """Сеть записи AllowedIPs без знака `+`/`-`, как её держит wg и печатает `wg show allowed-ips`.

    Без маски - /32 или /128. Пустая запись (`X,`, `,X`, `X,,Y`) и запись,
    которую wg не разберёт, - ValueError с этой записью.
    """
    address = entry[1:] if entry[:1] in ('+', '-') else entry
    try:
        if not re.fullmatch(r'[^/%]+(/[0-9]+)?', address):
            raise ValueError(address)
        return str(ipaddress.ip_network(address, strict=False))
    except ValueError:
        raise ValueError(f'wg0.conf: AllowedIPs `{entry}` не разбирается, wg отвергает файл') from None


def conf_peers(text):
    """Пиры wg0.conf: ключ, PSK и AllowedIPs, какими их оставит `wg setconf`.

    Каждая секция [Peer] идёт в ядро по порядку, пир с тем же ключом - тот же пир:
    секция без записей со знаком заменяет его набор, `+X` добавляет X, `-X`
    убирает X у этого пира. Адрес, который позже назвал другой пир, остаётся
    только у него. Пир в ответе один раз, в порядке первой секции; секции
    без ключа - отдельные пиры с ключом ''.
    """
    peers, by_key, owners = [], {}, {}
    for section, entries in wg_sections(text):
        if section != '[peer]':
            continue
        values = {name: value for name, value, _ in entries}
        key = values.get('publickey', '')
        peer = by_key.get(key) if key else None
        if peer is None:
            peer = {'key': key, 'preshared_key': '', 'allowed': []}
            peers.append(peer)
            by_key[key] = peer
        if 'presharedkey' in values:
            peer['preshared_key'] = values['presharedkey']
        signed = []
        for value in (value for name, value, _ in entries if name == 'allowedips'):
            if not value:
                raise ValueError('wg0.conf: пустая строка AllowedIPs, wg отвергает файл')
            signed += [(entry[:1], wg_network(entry)) for entry in value.split(',')]
        if not any(sign in ('+', '-') for sign, _ in signed):
            owners = {network: owner for network, owner in owners.items() if owner is not peer}
        for sign, network in signed:
            if sign != '-':
                owners[network] = peer
            elif owners.get(network) is peer:
                del owners[network]
    for network, peer in owners.items():
        peer['allowed'].append(network)
    return peers
