"""Emergency panel with a real Hysteria2 profile for the QR size test; never deployed.

URI and PNG come from hy2.profile and hy2.materials, the same code the exit
runs. The worst case: device name at the panel limit (64 Cyrillic letters,
each one six bytes after percent-encoding) and a pinSHA256 of the fallback
certificate.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'emergency'))
from vpn2_emergency import hy2
from vpn2_emergency.web import make_server

NAME = 'Ж' * 64
DEVICE = 11


def main():
    directory = Path(sys.argv[1])
    options = {'server_ip': '45.137.198.213', 'port': 443}
    uri = hy2.profile(options, secrets.token_urlsafe(32), NAME, secrets.token_hex(32))
    profiles = hy2.materials(directory, {'devices': [{'id': DEVICE, 'uri': uri}]})
    snapshot = dict(created_at=datetime.now(timezone.utc).isoformat(), bot_token='fixture-only',
                    exits=[dict(id='one', name='Первый exit', ip='127.0.0.1')],
                    clients=[dict(id=1, name='e2e-QR', telegram_chat_id=None, allowed=True, expires_at=None,
                                  devices=[dict(id=DEVICE, name=NAME)])])
    server = make_server(directory, exit_id='one', public_ip='127.0.0.1', address=('127.0.0.1', 0),
                         cert_dir=directory / 'missing', snapshot_loader=lambda _p: snapshot,
                         profiles=lambda _g: profiles, connections=lambda _g: {DEVICE: 'never'},
                         delivery=lambda _g: [], broadcast=lambda _message, _generation: None)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        payload = server.panel.on_emergency_leader('fixture', 3)
        print(json.dumps(dict(port=server.server_port, password=payload['panel_password'], uri=uri)), flush=True)
        # The test closes stdin when it is done.
        sys.stdin.read()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == '__main__':
    main()
