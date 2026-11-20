import os
from pathlib import Path
import re
import sys

import pytest

from vpn2 import creds

# Hooks and the autouse fixture are called by pytest by name.
__all__ = ['creds_store', 'pytest_collection_modifyitems', 'pytest_configure', 'umask_restored']

# Tests on a fresh wg0 in a netns share the host: one worker runs them all.
HOST = re.compile(r'wg_oracle|/run/netns')


def pytest_configure(config):
    config.addinivalue_line('markers', 'xdist_group(name): tests of one group run in one worker')


def pytest_collection_modifyitems(items):
    host = {}
    for item in items:
        path = Path(str(item.fspath))
        if path not in host:
            host[path] = bool(HOST.search(path.read_text(errors='replace')))
        if host[path]:
            item.add_marker(pytest.mark.xdist_group('host'))


@pytest.fixture(autouse=True)
def umask_restored():
    """A command's main() sets the umask of the process for good: the next test must not inherit it."""
    before = os.umask(0)
    os.umask(before)
    yield
    os.umask(before)


# systemd-creds без ключа сервера: шифр - base64 имени и значения; расшифровка сверяет имя, как настоящий.
FAKE_CREDS = r'''#!PYTHON
import base64, sys
args = sys.argv[1:]
name = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--name='))
if args[0] == 'encrypt':
    sys.stdout.buffer.write(b'CRED:' + base64.b64encode(name.encode() + b'\0' + sys.stdin.buffer.read()))
else:
    stored, _, value = base64.b64decode(open(args[2], 'rb').read()[5:]).partition(b'\0')
    if stored.decode() != name:
        sys.exit('имя секрета не совпало')
    sys.stdout.buffer.write(value)
'''


@pytest.fixture(autouse=True)
def creds_store(tmp_path_factory, monkeypatch):
    """Хранилище секретов каждого теста - во временном каталоге, systemd-creds - заглушка на PATH."""
    root = tmp_path_factory.mktemp('creds')
    (root / 'bin').mkdir()
    (root / 'bin/systemd-creds').write_text(FAKE_CREDS.replace('PYTHON', sys.executable))
    (root / 'bin/systemd-creds').chmod(0o755)
    monkeypatch.setenv('PATH', f"{root / 'bin'}:{os.environ['PATH']}")
    for name, attr in (('store', 'STORE'), ('run', 'VIEWS')):
        monkeypatch.setenv(f'VPN2_CREDS_{attr}', str(root / name))
        monkeypatch.setattr(creds, attr, root / name)
    return root
