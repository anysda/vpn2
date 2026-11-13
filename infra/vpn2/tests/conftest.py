import os
from pathlib import Path
import re

import pytest

# Hooks and the autouse fixture are called by pytest by name.
__all__ = ['pytest_collection_modifyitems', 'pytest_configure', 'umask_restored']

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
