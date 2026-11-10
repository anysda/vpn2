"""vpn2 verify [--target узел] [--traffic] - проверка узла после деплоя.

Код 0 - всё работает, 1 - отказ, 2 - часть не проверена. Узел и его
настройки приходят окружением стадии (HOST_TAG, MGMT_IP, EXIT_TAGS, ...).
"""

import argparse
import os

from vpn2.verify import stage


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 verify', description='Проверка узла после деплоя')
    parser.add_argument('--target', metavar='узел', help='тег узла вместо HOST_TAG: ru или тег экзита')
    parser.add_argument('--traffic', action='store_true', help='только настоящий трафик контрольным клиентом')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.target:
        env['HOST_TAG'] = args.target
    return stage.run(env, traffic_only=args.traffic)
