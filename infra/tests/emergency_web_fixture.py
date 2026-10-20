"""Private stdin/stdout fixture protocol for Playwright; never deployed."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'emergency'))
from vpn2_emergency.web import make_server


def main():
    directory = Path(sys.argv[1])
    snapshot = dict(created_at=datetime.now(timezone.utc).isoformat(), bot_token='fixture-only',
                    exits=[dict(id='one', name='Первый exit', ip='127.0.0.1')],
                    clients=[dict(id=1, name='e2e-Семён', telegram_chat_id=None, allowed=True, expires_at=None,
                                  devices=[dict(id=11, name='телефон'), dict(id=12, name='планшет')]),
                             dict(id=2, name='e2e-Анна', telegram_chat_id=42, allowed=True, expires_at=None,
                                  devices=[dict(id=21, name='ноутбук')])])
    uri = 'hysteria2://fixture-only@203.0.113.2:443/'
    file = directory / 'profile.txt'
    file.write_text(uri)
    profiles = {d: dict(uri=uri, file=str(file), qr_png=(directory / 'qr.png').read_bytes()) for d in (11, 12, 21)}
    connections = {11: 'connected', 12: 'never', 21: 'seen'}
    row = dict(emergency='fixture', generation=3, client=1, version='v', event='initial', reason='')
    delivery = [row | dict(state='sent'), row | dict(state='retry', reason='Telegram 503'),
                row | dict(state='pending'), row | dict(state='blocked', reason='нет канала доставки')]
    server = make_server(directory, exit_id='one', public_ip='127.0.0.1', address=('127.0.0.1', 0),
                         cert_dir=directory / 'missing', snapshot_loader=lambda _p: snapshot,
                         profiles=lambda _g: profiles, connections=lambda _g: connections,
                         delivery=lambda _g: delivery,
                         broadcast=lambda _message, _generation: delivery.append(row | dict(state='pending')))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        # Generations count across emergencies: the first leader of this one is not number 1.
        payload = server.panel.on_emergency_leader('fixture', 3)
        print(json.dumps(dict(port=server.server_port, password=payload['panel_password'])), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            action = command['action']
            if action == 'recovery':
                server.panel.on_entry_recovered(time.monotonic() + 600)
            elif action == 'cancel':
                server.panel.on_return_cancelled()
            elif action == 'partition':
                server.panel.on_emergency_leader('fixture', 3, True)
            elif action == 'end':
                server.panel.on_emergency_end()
            elif action == 'fresh':
                snapshot['created_at'] = datetime.now(timezone.utc).isoformat()
            elif action == 'never':
                connections[21] = 'never'
            elif action == 'unknown':
                connections.clear()
            elif action == 'refused':
                delivery.append(row | dict(client=2, state='failed', reason='Telegram 400: чат не найден'))
            elif action == 'denied':
                snapshot['clients'] += [
                    dict(id=3, name='e2e-Блок', telegram_chat_id=43, allowed=False, expires_at=None,
                         devices=[dict(id=31, name='телефон')]),
                    dict(id=4, name='e2e-Истёк', telegram_chat_id=44, allowed=False,
                         expires_at='2026-01-01T00:00:00+00:00', devices=[dict(id=41, name='телефон')])]
            elif action == 'rotate':
                payload = server.panel.on_emergency_leader('fixture', 4, False, 'leader_changed')
            print(json.dumps(dict(password=payload['panel_password'])), flush=True)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == '__main__':
    main()
