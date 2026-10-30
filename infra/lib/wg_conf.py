"""wg0.conf так, как его читает wg (config.c wireguard-tools): секции, AllowedIPs, пиры.

Один разбор для сверки бэкапа (anysda-backup.sh, на узле берёт из /usr/local/lib/anysda)
и проверки трафика (verify-traffic.py, грузит рядом с собой). Файл, который wg отвергает, -
ValueError с «AllowedIPs», «PublicKey» или строкой, которую wg не распознаёт.
"""
import ipaddress
import re

# Имена, которые wg принимает в секции; в [Interface] ещё имена, которые wg-quick убирает до wg.
NAMES = {'[interface]': {'privatekey', 'listenport', 'fwmark', 'address', 'dns', 'mtu', 'table',
                         'preup', 'postup', 'predown', 'postdown', 'saveconfig'},
         '[peer]': {'publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'}}


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


def unrecognized(text):
    """Первая строка, которую wg-quick и wg не распознают (`Line unrecognized`), или None.

    Неизвестная секция, строка вне секции или без `=`, имя не из NAMES своей секции.
    Комментарии и пустые строки допустимы. В ответе номер строки и имя без значения;
    имя не из одних букв (скажем, ключ без `=`) не печатается.
    """
    section = None
    for number, line in enumerate(text.split('\n'), 1):
        clean = re.sub(r'[ \t\n\v\f\r]', '', line.split('#', 1)[0])
        if not clean:
            continue
        name, eq, _ = clean.partition('=')
        lower = name.encode().lower().decode()
        if clean.startswith('[') and lower in NAMES:
            section = lower
        elif not eq or lower not in NAMES.get(section, ()):
            shown = f' `{name}`' if re.fullmatch(r'\[?[^\W\d_]{1,32}\]?', name) else ''
            return f'строка {number}{shown}'
    return None


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
    только у него. Пир в ответе один раз, в порядке первой секции. Секция
    без PublicKey или с пустым, строка, которую wg не распознаёт, - ValueError:
    wg отвергает такой файл целиком. Пир без ключа называется раньше нераспознанной строки.
    """
    peers, by_key, owners = [], {}, {}
    for section, entries in wg_sections(text):
        if section != '[peer]':
            continue
        values = {name: value for name, value, _ in entries}
        keys = [value for name, value, _ in entries if name == 'publickey']
        if not keys or not all(keys):
            raise ValueError('wg0.conf: пир без PublicKey, wg отвергает файл')
        key = keys[-1]
        peer = by_key.get(key)
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
    line = unrecognized(text)
    if line:
        raise ValueError(f'wg0.conf: {line} не распознаётся, wg отвергает файл')
    for network, peer in owners.items():
        peer['allowed'].append(network)
    return peers
