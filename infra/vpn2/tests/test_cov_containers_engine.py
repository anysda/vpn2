"""Docker на entry через подменную команду docker на PATH: образ разработки, запуск службы, мелкие шаги."""
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vpn2.containers import compose, engine

DOCKER = r'''#!{python}
import json, sys
path = {state!r}
state = json.load(open(path))
args = sys.argv[1:]
state['calls'].append(' '.join(args))


def leave(code=0, err=''):
    json.dump(state, open(path, 'w'))
    sys.stderr.write(err)
    sys.exit(code)


if args[0] == 'pull':
    leave(state['pull_rc'], 'Error response from daemon: manifest unknown\n' if state['pull_rc'] else '')
if args[:2] == ['image', 'inspect']:
    image = state['images'].get(args[-1])
    if image is None:
        leave(1, 'Error: No such image\n')
    print(image['id'] if '.Id' in args[3] else image.get('revision', ''))
    leave()
if args[0] == 'inspect':
    container = state['containers'].get(args[-1])
    if container is None:
        leave(1, 'Error: No such container\n')
    print(json.dumps([container]))
    leave()
if args[0] == 'rm':
    state['containers'].pop(args[-1], None)
    leave()
if args[0] == 'compose':
    if state['up'] == 'fail':
        leave(1, 'pulling\nError response from daemon: port is already allocated\n')
    if state['up'] == 'ok' and args[-1] not in state['containers']:
        state['serial'] += 1
        state['containers'][args[-1]] = {{'Id': 'new%d' % state['serial'],
                                         'Config': {{'Labels': {{{label!r}: {project!r}}}}}}}
    leave()
if args[0] == 'logs':
    print('line of stdout')
    leave(0, 'line of stderr\n')
leave(64, 'unknown docker command\n')
'''


class Stub:
    def __init__(self, path):
        self.path = path / 'docker.json'
        self.write({'calls': [], 'pull_rc': 0, 'images': {}, 'containers': {}, 'up': 'ok', 'serial': 0})

    def read(self):
        return json.loads(self.path.read_text())

    def write(self, state):
        self.path.write_text(json.dumps(state))

    def set(self, **values):
        self.write({**self.read(), **values})

    def calls(self):
        state = self.read()
        self.write({**state, 'calls': []})
        return state['calls']


@pytest.fixture
def stub(tmp_path, monkeypatch):
    box = Stub(tmp_path)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    script = bin_dir / 'docker'
    script.write_text(DOCKER.format(python=sys.executable, state=str(box.path),
                                    label=engine.PROJECT_LABEL, project=compose.PROJECT))
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir))
    return box


DEV = {'VPN2_DEVELOPMENT': 'true'}
REF = 'ghcr.io/anysda/vpn2/tgbot:dev'


def test_development_image_is_pulled_and_named_with_its_revision(stub):
    stub.set(images={REF: {'id': 'sha256:aaa', 'revision': 'c0ffee'}})
    err = io.StringIO()
    for _ in range(2):
        assert engine.release_image(DEV, 'tgbot', err=err) == 'sha256:aaa'
    assert err.getvalue() == f'[development] tgbot: {REF} revision c0ffee (pulled)\n' * 2
    assert stub.calls()[0] == f'pull {REF}'


def test_failed_pull_falls_back_to_the_local_image_with_a_warning(stub):
    stub.set(pull_rc=1, images={REF: {'id': 'sha256:bbb'}})
    err = io.StringIO()
    assert engine.release_image(DEV, 'tgbot', err=err) == 'sha256:bbb'
    assert err.getvalue() == (f'WARNING: [development] tgbot: pull {REF} failed, using the local image\n'
                              f'[development] tgbot: {REF} revision unknown (local, pull failed)\n')


def test_pull_can_be_switched_off_for_a_requested_image(stub):
    stub.set(images={'local/panel:test': {'id': 'sha256:ccc', 'revision': 'r1'}})
    err = io.StringIO()
    assert engine.release_image(DEV, 'panel', 'local/panel:test', pull=False, err=err) == 'sha256:ccc'
    assert err.getvalue() == '[development] panel: local/panel:test revision r1 (local, pull disabled)\n'
    assert not any(call.startswith('pull') for call in stub.calls())


@pytest.mark.parametrize('pull_rc', [0, 1])
def test_absent_image_is_an_error_that_names_it(stub, pull_rc):
    stub.set(pull_rc=pull_rc)
    with pytest.raises(engine.EngineError, match=f'tgbot: образа {REF} нет'):
        engine.release_image(DEV, 'tgbot', err=io.StringIO())


def test_release_without_a_manifest_is_refused_before_docker(stub, tmp_path):
    environ = {'VPN2_RELEASE_MANIFEST': str(tmp_path / 'absent.json')}
    with pytest.raises(Exception) as caught:  # noqa: PT011 - класс отказа задаёт vpn2.release
        engine.release_image(environ, 'tgbot')
    assert 'absent.json' in str(caught.value)
    assert stub.calls() == []


@pytest.mark.usefixtures('stub')
def test_absent_container_and_image_read_as_nothing():
    docker = engine.Docker()
    assert docker.inspect('anysda-tgbot') is None
    assert docker.image_id('no/such:image') == ''


def test_logs_join_both_streams(stub):
    docker = engine.Docker()
    assert docker.logs('bot') == 'line of stdout\nline of stderr\n'
    assert docker.logs('bot', tail=6) == 'line of stdout\nline of stderr\n'
    assert stub.calls() == ['logs bot', 'logs --tail 6 bot']


def test_up_creates_once_and_then_keeps_the_container(stub, tmp_path):
    docker = engine.Docker(compose_file=tmp_path / 'compose.yaml')
    assert docker.up('bot') is True
    assert docker.up('bot') is False
    assert stub.read()['containers']['bot']['Id'] == 'new1'


def test_up_replaces_a_container_started_outside_compose(stub, tmp_path):
    stub.set(containers={'bot': {'Id': 'old', 'Config': {'Labels': None}}})
    docker = engine.Docker(compose_file=tmp_path / 'compose.yaml')
    assert docker.up('bot') is True
    calls = stub.calls()
    assert 'rm -f bot' in calls and calls.index('rm -f bot') < calls.index(
        f'compose -f {tmp_path}/compose.yaml up -d --no-deps bot')
    assert stub.read()['containers']['bot']['Id'] == 'new1'


def test_failed_up_shows_the_last_line_of_docker(stub, tmp_path):
    stub.set(up='fail')
    docker = engine.Docker(compose_file=tmp_path / 'compose.yaml')
    with pytest.raises(engine.EngineError) as caught:
        docker.up('bot')
    assert str(caught.value) == ('docker compose -f: код 1 '
                                 'Error response from daemon: port is already allocated')


def test_up_without_a_container_afterwards_is_an_error(stub, tmp_path):
    stub.set(up='nothing')
    with pytest.raises(engine.EngineError, match='bot: контейнера нет после compose up'):
        engine.Docker(compose_file=tmp_path / 'compose.yaml').up('bot')


def test_missing_docker_program_is_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(FileNotFoundError):
        engine.Docker().inspect('bot')


def test_services_of_an_absent_compose_file_are_empty(tmp_path):
    assert engine.Docker(compose_file=tmp_path / 'absent/compose.yaml').services() == {}


class Answer(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - имя задаёт http.server
        self.send_response(int(self.path.strip('/')))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def web():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()
    thread.join(5)


@pytest.mark.parametrize(('code', 'ok'), [(200, True), (204, True), (404, False), (500, False)])
def test_http_ok_is_true_only_for_success(web, code, ok):
    assert engine.http_ok(f'{web}/{code}') is ok


@pytest.mark.parametrize('url', ['http://127.0.0.1:1/', 'not a url', '', 'file:///no/such/file'])
def test_http_ok_is_false_for_anything_unreachable(url):
    assert engine.http_ok(url, timeout=1) is False


def test_wait_stops_at_the_first_success_and_sleeps_between_tries():
    answers = iter([False, False, True, True])
    pauses = []
    assert engine.wait(lambda: next(answers), 5, 0.5, sleep=pauses.append) is True
    assert pauses == [0.5, 0.5]


def test_wait_gives_up_without_a_trailing_sleep():
    pauses = []
    assert engine.wait(lambda: False, 3, 2, sleep=pauses.append) is False
    assert pauses == [2, 2]
    assert engine.wait(lambda: True, 0, 2, sleep=pauses.append) is False


def test_timezone_directory_left_by_docker_is_removed(tmp_path):
    path = tmp_path / 'timezone'
    path.mkdir()
    engine.drop_timezone_dir(str(path))
    engine.drop_timezone_dir(str(path))
    assert not path.exists()


def test_timezone_file_and_a_busy_directory_are_left_alone(tmp_path):
    file = tmp_path / 'timezone'
    file.write_text('Europe/Moscow\n')
    engine.drop_timezone_dir(str(file))
    assert file.read_text() == 'Europe/Moscow\n'
    busy = tmp_path / 'busy'
    busy.mkdir()
    (busy / 'inside').write_text('')
    engine.drop_timezone_dir(str(busy))
    assert os.listdir(busy) == ['inside']


def test_stamp_creates_its_directory_and_repeats(tmp_path):
    directory = tmp_path / 'var/.stamps'
    engine.stamp('35-telegram', str(directory))
    engine.stamp('35-telegram', str(directory))
    assert os.listdir(directory) == ['35-telegram']
