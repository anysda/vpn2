#!/usr/bin/env python3
"""Контролируемая цель 99-verify: эхо nonce и адрес, с которого пришёл запрос.

Ставится на время проверки на entry и на экзиты (через ssh, скрипт на stdin),
сам завершается через --lifetime. Отвечает только на POST /vpn2-verify/<nonce>
с тем же nonce в теле. Поле local — пришёл ли запрос с адреса самого узла:
так проверка отличает выход через этот узел от прихода с чужого адреса.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sys
import threading
import time


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa - HTTP handler
        nonce = self.path.rsplit('/', 1)[-1]
        if not re.fullmatch(r'/vpn2-verify/[a-f0-9]{32}', self.path):
            self.send_error(404)
            return
        self.connection.settimeout(5)
        if self.headers.get('Content-Length') != '32' or self.rfile.read(32) != nonce.encode():
            self.send_error(400)
            return
        peer = self.client_address[0]
        local = self.connection.getsockname()[0]
        body = json.dumps({'nonce': nonce, 'peer': peer, 'local': peer == local}).encode()
        # Соединение живёт заметное время: проверка успевает увидеть его в API sing-box.
        time.sleep(0.8)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--lifetime', type=int, default=600)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.daemon_threads = True  # noqa - read by ThreadingMixIn
    threading.Timer(args.lifetime, server.shutdown).start()
    print('ready', flush=True)
    sys.stdout.close()
    server.serve_forever()


if __name__ == '__main__':
    main()
