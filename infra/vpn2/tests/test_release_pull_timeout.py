"""Скачивание образа выпуска: медленный канал до реестра не роняет установку молча.

docker подменён программой на PATH: она ведёт счёт вызовов pull в файле и
«качает» столько секунд, сколько ей велено.
"""
import json
import subprocess
import sys

import pytest

from vpn2 import release

DIGEST = 'sha256:' + 'a' * 64
MANIFEST = {'images': {'panel': {'amd64': {'repository': 'example/panel', 'digest': DIGEST, 'config': DIGEST}}}}
REFERENCE = 'example/panel@' + DIGEST
DOCKER = r'''#!{python}
import json, os, sys, time
box = {box!r}


def read(name, default=''):
    try:
        return open(os.path.join(box, name)).read().strip()
    except OSError:
        return default


args = sys.argv[1:]
open(os.path.join(box, 'calls'), 'a').write(' '.join(args) + '\n')
if args[0] == 'pull':
    count = int(read('pulls', '0')) + 1
    open(os.path.join(box, 'pulls'), 'w').write(str(count))
    plan = read('plan').split()
    step = plan[min(count, len(plan)) - 1]
    if step == 'hang':
        time.sleep(60)
    if step.startswith('slow'):
        time.sleep(float(step[4:]))
    if step == 'refuse':
        sys.stderr.write('Error response from daemon: Get "https://user:secret@ghcr.io/v2/": net/http: TLS handshake timeout\n')
        sys.exit(1)
    open(os.path.join(box, 'image'), 'w').write('1')
elif args[:2] == ['image', 'inspect']:
    if not read('image'):
        sys.stderr.write('Error: No such image\n')
        sys.exit(1)
    print(json.dumps([{{'Id': {digest!r}, 'Architecture': 'amd64', 'RepoDigests': [{reference!r}]}}]))
'''


@pytest.fixture
def docker(tmp_path, monkeypatch):
    box = tmp_path / 'box'
    tools = tmp_path / 'bin'
    box.mkdir()
    tools.mkdir()
    tool = tools / 'docker'
    tool.write_text(DOCKER.format(python=sys.executable, box=str(box), digest=DIGEST, reference=REFERENCE))
    tool.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tools}:/usr/bin:/bin')
    monkeypatch.setattr(release, 'PULL_PAUSES', (0, 0))
    monkeypatch.setattr(release, 'PULL_TIMEOUT', 1)
    return box


def pulls(box):
    return int((box / 'pulls').read_text())


def test_pull_has_its_own_limit_far_above_the_command_limit():
    assert release.COMMAND_TIMEOUT == 300
    assert release.PULL_TIMEOUT >= 4 * release.COMMAND_TIMEOUT


def test_slow_but_alive_pull_is_not_cut_at_the_command_limit(docker, monkeypatch):
    monkeypatch.setattr(release, 'COMMAND_TIMEOUT', 0.3)
    monkeypatch.setattr(release, 'PULL_TIMEOUT', 20)
    (docker / 'plan').write_text('slow1.2')
    assert release.image(MANIFEST, 'panel', arch='amd64') == DIGEST
    assert pulls(docker) == 1


@pytest.mark.parametrize('plan', ['hang ok', 'hang hang ok', 'refuse hang ok', 'hang refuse ok'])
def test_pull_cut_by_time_is_tried_again(docker, plan, capsys):
    (docker / 'plan').write_text(plan)
    assert release.image(MANIFEST, 'panel', arch='amd64') == DIGEST
    assert pulls(docker) == len(plan.split())
    err = capsys.readouterr().err
    assert 'release: panel: docker pull failed (1/3), retry in 0 s' in err
    assert 'no answer in 1 s' in err and 'secret' not in err


def test_pull_cut_on_every_attempt_names_the_image_and_the_cause(docker):
    (docker / 'plan').write_text('hang')
    with pytest.raises(release.ReleaseError) as caught:
        release.image(MANIFEST, 'panel', arch='amd64')
    text = str(caught.value)
    assert pulls(docker) == 3
    assert f'panel: image {REFERENCE} not downloaded after 3 attempts' in text
    assert 'docker: no answer in 1 s' in text
    assert 'slow' in text and 'run ./deploy.sh again' in text
    assert 'docker image failed' not in text


def test_repeat_after_the_final_failure_needs_no_cleanup(docker):
    (docker / 'plan').write_text('hang hang hang ok')
    with pytest.raises(release.ReleaseError):
        release.image(MANIFEST, 'panel', arch='amd64')
    assert release.image(MANIFEST, 'panel', arch='amd64') == DIGEST
    assert pulls(docker) == 4


def test_pull_cut_by_time_accepts_a_matching_local_image(docker):
    (docker / 'plan').write_text('hang')
    (docker / 'image').write_text('1')
    assert release.image(MANIFEST, 'panel', arch='amd64') == DIGEST


def test_missing_local_image_is_named_without_pull(docker):
    with pytest.raises(release.ReleaseError) as caught:
        release.image(MANIFEST, 'panel', pull=False, arch='amd64')
    assert f'panel: image {REFERENCE} is not on this node' in str(caught.value)
    assert not (docker / 'pulls').exists()


def test_cli_prints_the_cause_not_the_bare_failure(docker, tmp_path, capsys):
    (docker / 'plan').write_text('hang')
    manifest = tmp_path / 'release.json'
    manifest.write_text(json.dumps({'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible',
                                    'binaries': {}, **MANIFEST}))
    assert release.main(['--manifest', str(manifest), 'image', 'panel']) != 0
    err = capsys.readouterr().err
    assert f'image {REFERENCE} not downloaded after 3 attempts, docker: no answer in 1 s' in err
    assert subprocess.TimeoutExpired.__name__ not in err
