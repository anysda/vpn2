"""Формат манифеста выпуска: сборка на маленьком дереве против эталона.

Эталон fixtures/release/manifest.json - договор с узлами прошлых выпусков
(deploy_state.py читает манифест на узле). Меняется формат - меняется эталон,
и это видно в диффе.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vpn2 import release
from vpn2.release import build

FIXTURE = Path(__file__).parent / 'fixtures/release/manifest.json'
IMAGE = {'amd64': {'repository': 'ghcr.io/anysda/vpn2/x', 'digest': 'sha256:' + 'a' * 64,
                   'config': 'sha256:' + 'b' * 64}}
BINARY = {'caddy': {'version': '1.0.0', 'architecture': 'amd64', 'url': 'https://example.invalid/caddy.deb',
                    'sha256': 'c' * 64}}
FILES = {
    'deploy.sh': 'root\n',
    'infra/deploy.sh': 'deploy\n',
    'infra/versions.yml': 'caddy: "1.0.0"\n',
    'infra/scripts/00-bootstrap.sh': 'stage\n',
    'infra/lib/ssh.sh': 'lib\n',
    'infra/lib/deploy_state.py': 'state\n',
    'infra/configs/a/b.tpl': 'config\n',
    'infra/vpn2/pyproject.toml': 'project\n',
    'infra/vpn2/src/vpn2/__init__.py': '',
    'infra/vpn2/src/vpn2/release/__init__.py': 'package\n',
    'infra/ansible/ansible.cfg': 'cfg\n',
    'infra/ansible/roles/backup/tasks/main.yml': 'role\n',
    'infra/ansible/plays/26-backup.yml': 'play\n',
    'app/package.json': '{"version": "9.9.9"}\n',
    'app/server/database/migrations/0000_init.sql': 'create\n',
    'app/server/database/migrations/meta/_journal.json': '{"entries": [{"when": 1, "tag": "0000_init"}]}\n',
    # вне выпуска: не стадии и не код деплоя
    'infra/vpn2/src/vpn2/__pycache__/x.pyc': 'cache\n',
    'infra/vpn2/tests/test_x.py': 'test\n',
    'infra/ansible/.ansible/tmp/x': 'scratch\n',
    'infra/tests/test_y.py': 'test\n',
    'README.md': 'docs\n',
}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / 'repo'
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git = ['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(git + ['add', '-A'], check=True)
    subprocess.run(git + ['commit', '-qm', 'tree'], check=True)
    return root


def manifest_of(root, tmp_path):
    digests = tmp_path / 'digests'
    digests.mkdir()
    for name in ('panel', 'tgbot', 'emergency'):
        (digests / f'{name}.txt').write_text('sha256:' + 'a' * 64)
    output = tmp_path / 'release-manifest.json'
    with patch.object(build, 'resolve_image', return_value=IMAGE), \
            patch.object(build, 'binary_specs', return_value=BINARY):
        assert release.main(['build-manifest', '--root', str(root), '--digests', str(digests),
                             '--output', str(output)]) == 0
    return output


def test_format_matches_fixture(tree, tmp_path):
    output = manifest_of(tree, tmp_path)
    manifest = json.loads(output.read_text())
    assert len(manifest.pop('commit')) == 40
    assert manifest == json.loads(FIXTURE.read_text())


def test_built_manifest_loads_and_verifies_tree(tree, tmp_path, capsys):
    output = manifest_of(tree, tmp_path)
    assert release.load(output)['format'] == 1
    assert release.main(['--manifest', str(output), 'verify-tree', str(tree)]) in (0, None)
    (tree / 'infra/ansible/roles/backup/tasks/main.yml').write_text('changed\n')
    assert release.main(['--manifest', str(output), 'verify-tree', str(tree)]) not in (0, None)
    assert 'infra/ansible/roles/backup/tasks/main.yml' in capsys.readouterr().err
