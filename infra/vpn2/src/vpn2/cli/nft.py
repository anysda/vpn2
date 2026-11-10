"""vpn2 nft yt-quic <goog.json> - таблица nft блокировки QUIC к Google в stdout."""

import sys

from vpn2 import firewall


def main(argv):
    if len(argv) != 2 or argv[0] != 'yt-quic':
        sys.stderr.write('Использование: vpn2 nft yt-quic <goog.json>\n')
        return 2
    try:
        v4, v6 = firewall.load_google_prefixes(argv[1])
    except (OSError, ValueError) as error:
        sys.stderr.write(f'ERROR: {error}\n')
        return 1
    sys.stdout.write(firewall.google_quic_table(v4, v6))
    return 0
