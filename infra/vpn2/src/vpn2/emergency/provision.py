"""Render private observer configs on the deploy host, before parallel upload."""

import argparse
import fcntl
import json
from pathlib import Path
import secrets
import uuid

from .. import fsutil


def provision(root):
    import yaml  # только на хосте деплоя: агент на экзите обходится без PyYAML

    root = Path(root)
    config = yaml.safe_load((root / 'config.yaml').read_text())
    directory = root / 'infra/secrets/emergency'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = directory / 'identity.json'
        if identity.exists():
            shared = json.loads(identity.read_text())
        else:
            shared = {'installation_id': str(uuid.uuid4()), 'key': secrets.token_hex(32)}
            fsutil.atomic(identity, json.dumps(shared))
        # Underscores cannot occur in user exit tags; "entry" is a valid tag.
        entry_id = '__entry__'
        nodes = {entry_id: {'host': config['entry']['host'], 'port': 51821}}
        for item in config['exits']:
            nodes[item['tag']] = {'host': item['host'], 'port': 51821}
        if len(nodes) != len(config['exits']) + 1 or len(nodes) > 65:
            raise ValueError('invalid observer membership')
        for node in nodes:
            fsutil.atomic(directory / (('ru' if node == entry_id else node) + '.json'),
                          json.dumps(dict(shared, node=node, entry_id=entry_id, nodes=nodes)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    provision(parser.parse_args(argv).root)


if __name__ == '__main__':
    main()
