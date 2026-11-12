"""vpn2 emergency: один диспетчер у команды и у служб, действие endpoint."""
import json
import subprocess
import sys

import pytest

import vpn2.cli as cli
from vpn2.emergency import __main__ as dispatcher, hy2


def test_cli_and_services_share_one_dispatcher():
    assert cli._resolve(dict(cli.COMMANDS)['emergency']) is dispatcher.main


@pytest.mark.parametrize('argv', ([], ['unknown']))
def test_wrong_command_prints_usage(argv, capsys):
    assert dispatcher.main(argv) == 2
    assert 'snapshot-sender' in capsys.readouterr().err


def test_every_command_has_a_main():
    import importlib
    for name in dispatcher.COMMANDS:
        assert callable(importlib.import_module(f"vpn2.emergency.{name.replace('-', '_')}").main), name


def test_module_runs_without_installed_package(tmp_path):
    # Экзит получает пакет каталогом рядом с рабочим каталогом службы.
    src = str(__import__('pathlib').Path(hy2.__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, '-B', '-I', '-c',
                             'import sys; sys.path.insert(0, sys.argv[1]); '
                             'from vpn2.emergency.__main__ import main; sys.exit(main(["hy2", "--help"]))',
                             src], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'endpoint' in result.stdout


def test_endpoint_writes_and_keeps_settings_of_a_running_emergency(tmp_path):
    path = tmp_path / 'emergency-hy2.json'
    hy2.endpoint('192.0.2.7', '4443', False, path)
    assert json.loads(path.read_text()) == {'server_ip': '192.0.2.7', 'port': 4443}
    hy2.endpoint('192.0.2.7', 4443, True, path)
    with pytest.raises(SystemExit, match='stop emergency role'):
        hy2.endpoint('192.0.2.7', 4444, True, path)
    hy2.endpoint('192.0.2.8', 4444, False, path)
    assert json.loads(path.read_text()) == {'server_ip': '192.0.2.8', 'port': 4444}


@pytest.mark.parametrize('ip, port', (('not-an-ip', 4443), ('192.0.2.7', 80), ('192.0.2.7', 70000)))
def test_endpoint_rejects_bad_values(tmp_path, ip, port):
    with pytest.raises((SystemExit, ValueError)):
        hy2.endpoint(ip, port, False, tmp_path / 'x.json')
    assert not (tmp_path / 'x.json').exists()
