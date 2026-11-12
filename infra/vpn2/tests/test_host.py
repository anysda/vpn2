"""Шаги узла из ролей: числа и строки сверены со стадией 00-bootstrap.sh и anysda-svc.sh."""
import hashlib
from pathlib import Path
import subprocess
import types

import pytest
import yaml

from vpn2 import svc, swap
from vpn2.cli import host

MIN_FREE = 2147483648


@pytest.mark.parametrize('mem_kb, avail, size, text', [
    (2014000, 20000000000, 2062336000, 'swap: /swapfile 1966 МиБ (RAM 1966 МиБ; свободно 19073 МиБ)'),
    (961000, 20000000000, 984064000, 'swap: /swapfile 938 МиБ (RAM 938 МиБ; свободно 19073 МиБ)'),
    (2014000, 4000000000, 1314914304,
     'swap: /swapfile 1254 МиБ (урезан, деплою оставлено 2048 + 512; свободно 3814 МиБ)'),
    (2014000, 2900000000, 0,
     'swap пропущен: свободно 2765 МиБ, деплою нужно 2048 + 512, swap меньше 256 МиБ не создаю'),
    (2014000, 1000000000, 0,
     'swap пропущен: свободно 953 МиБ, деплою нужно 2048 + 512, swap меньше 256 МиБ не создаю'),
])
def test_swap_plan_matches_stage(mem_kb, avail, size, text):
    assert swap.plan(mem_kb, avail, MIN_FREE) == (size, text)


MIB = 1024 ** 2
ENTRY_INSTALL = swap.ENTRY_INSTALL  # base_swap_install_bytes роли base для entry
ENTRY_MEASURED = 5556          # МиБ занято на entry 2 ГБ / 10 ГБ после установки, без swap
CLEAN_OS = 2000                # МиБ занято чистой Ubuntu до первого шага
# Диск entry: МиБ всего на ФС и резерв ext4, которого нет в «свободно».
DISK_10 = (9978, 527)
DISK_8 = (7950, 420)
DISK_40 = (40200, 2010)


def disk(layout, used):
    total, reserved = layout
    return types.SimpleNamespace(free=(total - reserved - used) * MIB, used=used * MIB)


def test_role_gives_the_entry_install_size_this_file_counts_with():
    assert ENTRY_INSTALL == 6 * 1024 ** 3
    defaults = (Path(__file__).parents[2] / 'ansible/roles/base/defaults/main.yml').read_text()
    assert f'base_swap_install_bytes: "{{{{ {ENTRY_INSTALL} if base_entry else 0 }}}}"' in defaults
    tasks = (Path(__file__).parents[2] / 'ansible/roles/base/tasks/main.yml').read_text()
    assert "'--install-bytes', base_swap_install_bytes | string]" in tasks


def test_role_work_directory_is_not_reported_as_node_state():
    # The deploy removes it on an exit after every stage: making it again is not a change.
    tasks = yaml.safe_load((Path(__file__).parents[2] / 'ansible/roles/base/tasks/main.yml').read_text())
    work = [task for task in tasks if '{{ base_tmp }}' in str(task.get('ansible.builtin.file', {}))
            or '{{ base_tmp }}' in str(task.get('loop', ''))]
    assert [task['name'] for task in work] == ['Рабочий каталог стадий']
    assert work[0]['changed_when'] is False
    assert work[0]['ansible.builtin.file'] == {'path': '{{ base_tmp }}', 'state': 'directory',
                                               'owner': 'root', 'group': 'root', 'mode': '0755'}
    assert tasks.index(work[0]) < next(index for index, task in enumerate(tasks)
                                       if '{{ base_tmp }}' in str(task.get('ansible.builtin.copy', {})))


@pytest.mark.parametrize('layout, size_mib, text', [
    (DISK_10, 747, 'swap: /swapfile 747 МиБ (урезан, деплою оставлено 2048 + 512, установке 4144; '
                   'свободно 7451 МиБ)'),
    (DISK_8, 0, 'swap пропущен: свободно 5530 МиБ, деплою нужно 2048 + 512, установке 4144, '
                'swap меньше 256 МиБ не создаю'),
    (DISK_40, 1966, 'swap: /swapfile 1966 МиБ (RAM 1966 МиБ; свободно 36190 МиБ)'),
])
def test_swap_plan_on_a_clean_entry_keeps_room_for_the_install(layout, size_mib, text):
    clean = disk(layout, CLEAN_OS)
    size, said = swap.plan(2014000, clean.free, MIN_FREE, swap.pending(ENTRY_INSTALL, clean.used))
    assert (size // MIB, said) == (size_mib, text)
    # the install lands after swap: what is free then is what the next deploy sees
    after = disk(layout, ENTRY_MEASURED).free - size
    if size:
        assert after >= MIN_FREE + 512 * MIB


def test_swap_plan_without_install_size_is_the_old_one_and_starves_the_next_deploy():
    clean = disk(DISK_10, CLEAN_OS)
    size, _ = swap.plan(2014000, clean.free, MIN_FREE)
    assert size // MIB == 1966
    assert disk(DISK_10, ENTRY_MEASURED).free - size < MIN_FREE


def test_swap_plan_after_the_install_counts_only_what_is_left_of_it():
    # swap removed by hand on an installed 10 GB entry: the next deploy sizes it to the same room
    installed = disk(DISK_10, ENTRY_MEASURED)
    size, _ = swap.plan(2014000, installed.free, MIN_FREE, swap.pending(ENTRY_INSTALL, installed.used))
    assert size // MIB == 747
    # a node that holds more than the install size: nothing more is kept for it
    assert swap.pending(ENTRY_INSTALL, 7000 * MIB) == 0


def test_min_free_falls_back_without_manifest(tmp_path):
    assert swap.min_free_bytes(tmp_path / 'none.json') == MIN_FREE
    (tmp_path / 'm.json').write_text('{"min_free_bytes": 5}')
    assert swap.min_free_bytes(tmp_path / 'm.json') == 5

def test_f2b_ignore_matches_stage():
    resolve = lambda _name: ['203.0.113.7', '203.0.113.7', '2001:db8::1']  # noqa: E731
    assert host.f2b_ignore('198.51.100.9 50000 22', 'entry.example', resolve) == [
        '127.0.0.1/8', '::1', '198.51.100.9', '203.0.113.7', '2001:db8::1']
    assert host.f2b_ignore('127.0.0.1 1 22', '198.51.100.9') == ['127.0.0.1/8', '::1', '198.51.100.9']
    assert host.f2b_ignore('', '') == ['127.0.0.1/8', '::1']


def test_fingerprint_is_sha256_of_concatenation(tmp_path):
    (tmp_path / 'a').write_bytes(b'one\n')
    (tmp_path / 'b').write_bytes(b'two')
    paths = [tmp_path / 'a', tmp_path / 'missing', tmp_path / 'b']
    assert svc.fingerprint(paths) == hashlib.sha256(b'one\ntwo').hexdigest()


def test_restart_if_changed(tmp_path, monkeypatch):
    (tmp_path / 'conf').write_text('1')
    calls = []

    def run(argv, **kwargs):
        del kwargs
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(svc.subprocess, 'run', run)
    applied = tmp_path / 'applied'
    assert svc.restart_if_changed('x', [tmp_path / 'conf'], applied) is True
    assert (applied / 'x.sha256').read_text() == hashlib.sha256(b'1').hexdigest() + '\n'
    assert svc.restart_if_changed('x', [tmp_path / 'conf'], applied) is False
    (tmp_path / 'conf').write_text('2')
    assert svc.restart_if_changed('x', [tmp_path / 'conf'], applied) is True
    assert calls.count('restart') == 2
    # Экзит держит отпечаток sing-box в прежнем файле стадии: первый прогон роли
    # на живом узле не рвёт туннели лишним перезапуском.
    record = tmp_path / 'sing-box' / 'applied.sha256'
    record.parent.mkdir()
    record.write_text(hashlib.sha256(b'2').hexdigest() + '\n')
    assert svc.restart_if_changed('x', [tmp_path / 'conf'], record=record) is False
    (tmp_path / 'conf').write_text('3')
    assert svc.restart_if_changed('x', [tmp_path / 'conf'], record=record) is True
    assert record.read_text() == hashlib.sha256(b'3').hexdigest() + '\n'


SWAPS_HEAD = 'Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n'


class SwapNode:
    """Узел 2 ГБ без swap: swapon не берёт файл с дырами, пока его не записал dd."""

    def __init__(self, tmp_path, monkeypatch, *, virt='none', usage=None, holes=True):
        self.root, self.calls, self.virt, self.holes, self.sizes = tmp_path, [], virt, holes, []
        self.usage = usage or types.SimpleNamespace(free=20 * 1024 ** 3, used=2 * 1024 ** 3)
        self.file = tmp_path / 'swapfile'
        self.swapoff_works = True
        for name, text in (('proc/meminfo', 'MemTotal: 2014000 kB\nMemAvailable: 1228800 kB\n'),
                           ('proc/swaps', SWAPS_HEAD),
                           ('etc/fstab', '/dev/vda1 / ext4 defaults 0 1\n'), ('etc/sysctl.d/.keep', '')):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(text)
        monkeypatch.setattr(swap, 'SWAPFILE', str(self.file))
        monkeypatch.setattr(swap, '_ok', self.ok)
        monkeypatch.setattr(swap.shutil, 'which', lambda _name: '/usr/bin/systemd-detect-virt')
        monkeypatch.setattr(swap.shutil, 'disk_usage', lambda _path: self.disk_usage())
        monkeypatch.setattr(swap.subprocess, 'run', self.run)
        self.written, self.counted = False, 0

    def disk_usage(self):
        # the leftover file is part of `usage`; once it is removed the disk gets its room back
        gone = 0 if self.file.exists() and self.file.stat().st_size == self.counted else self.counted
        return types.SimpleNamespace(free=self.usage.free + gone, used=self.usage.used - gone)

    def run(self, *_, **__):
        return types.SimpleNamespace(stdout=self.virt + '\n', returncode=0)

    def ok(self, argv):
        self.calls.append(argv[0])
        if argv[0] == 'fallocate':
            self.file.write_bytes(b'')
            self.sizes.append(int(argv[2]) // MIB)
        if argv[0] == 'dd':
            self.file.write_bytes(b'')
            self.written = True
        if argv[0] == 'swapoff':
            if self.swapoff_works:
                (self.root / 'proc/swaps').write_text(SWAPS_HEAD)
            return self.swapoff_works
        return argv[0] != 'swapon' or self.written or not self.holes

    def leftover(self, size_mib):
        """A swap file of that size that is off; the disk usage of the node counts it."""
        with self.file.open('wb') as output:
            output.truncate(size_mib * MIB)
        self.counted = size_mib * MIB

    def active(self, size_mib, used_mib=0, extra=''):
        """The same file switched on, used_mib of it holding pages."""
        self.leftover(size_mib)
        (self.root / 'proc/swaps').write_text(
            f'{SWAPS_HEAD}{self.file} file {size_mib * 1024 - 4} {used_mib * 1024} -2\n{extra}')

    def available(self, mib):
        (self.root / 'proc/meminfo').write_text(f'MemTotal: 2014000 kB\nMemAvailable: {mib * 1024} kB\n')


def test_swap_holey_file_recreated_with_dd(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch), []
    node.file.write_bytes(b'')
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is True
    assert lines == [f'swap: включаю имеющийся {node.file}', f'swap: {node.file} не включается, пересоздаю',
                     f'swap: {node.file} 1966 МиБ (RAM 1966 МиБ; свободно 20480 МиБ)']
    assert node.calls == ['swapon', 'mkswap', 'swapon', 'dd', 'mkswap', 'swapon']
    assert (tmp_path / 'etc/fstab').read_text().endswith(f'{node.file} none swap sw 0 0\n')
    assert (tmp_path / 'etc/sysctl.d/60-anysda-swap.conf').read_text() == 'vm.swappiness = 10\n'


def test_swap_fresh_node_falls_back_from_fallocate_to_dd(tmp_path, monkeypatch):
    node = SwapNode(tmp_path, monkeypatch)
    assert swap.ensure(tmp_path / 'none.json', lambda _text: None, root=tmp_path) is True
    assert node.calls == ['fallocate', 'mkswap', 'swapon', 'dd', 'mkswap', 'swapon']


def test_swap_skipped_in_container_and_with_active_swap(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, virt='lxc'), []
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is False
    assert lines == ['swap пропущен: контейнер (lxc)']
    node.virt = 'none'
    (tmp_path / 'proc/swaps').write_text('Filename Type\n/swapfile file 1 0 -2\n')
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path) is False
    assert node.calls == []


def test_swap_on_a_clean_10gb_entry_is_sized_below_the_install(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_10, CLEAN_OS), holes=False), []
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL) is True
    assert node.sizes == [747] and node.calls == ['fallocate', 'mkswap', 'swapon']
    # the repeated deploy after the full install: swap is on, nothing is touched
    (tmp_path / 'proc/swaps').write_text('Filename Type\n/swapfile file 764928 0 -2\n')
    node.usage, node.calls = disk(DISK_10, ENTRY_MEASURED + 747), []
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL) is False
    assert node.calls == [] and len(lines) == 1


def test_swap_leftover_file_larger_than_its_room_is_recreated(tmp_path, monkeypatch):
    # the 1962 MiB file of an older deploy, switched off by hand and left on a 10 GB entry
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_10, ENTRY_MEASURED + 1962), holes=False), []
    node.leftover(1962)
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL) is True
    assert lines[0] == f'swap: имеющийся {node.file} 1962 МиБ не по месту, пересоздаю'
    assert node.sizes == [747] and node.calls == ['fallocate', 'mkswap', 'swapon']


def test_swap_leftover_file_that_fits_is_switched_on_as_is(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_40, ENTRY_MEASURED + 1966), holes=False), []
    node.leftover(1966)
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL) is True
    assert lines == [f'swap: включаю имеющийся {node.file}'] and node.calls == ['swapon']


def test_swap_on_a_clean_8gb_entry_is_skipped_and_node_stays_as_is(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_8, CLEAN_OS)), []
    assert swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL) is False
    assert node.calls == [] and 'установке 4144' in lines[0]
    assert 'swapfile' not in (tmp_path / 'etc/fstab').read_text()


def test_fstab_line_of_a_swap_file_that_is_gone_for_good_is_dropped(tmp_path, monkeypatch):
    # switched off and removed by the hint of the older refusal, and the disk has no room for a new one
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_8, ENTRY_MEASURED)), []
    (tmp_path / 'etc/fstab').write_text(f'/dev/vda1 / ext4 defaults 0 1\n{node.file} none swap sw 0 0\n')
    assert heal(tmp_path, lines) is True
    assert (tmp_path / 'etc/fstab').read_text() == '/dev/vda1 / ext4 defaults 0 1\n'
    assert heal(tmp_path, lines) is False and node.calls == []


OLD_SWAP = 1962       # МиБ: /swapfile, который ставил v1.1.5 на узле 2 ГБ
UPDATED = 5682        # МиБ занято без swap на узле из карточки после обновления на dev (свободно 1807)


def old_node(tmp_path, monkeypatch, layout=DISK_10, used=UPDATED, **active):
    node = SwapNode(tmp_path, monkeypatch, usage=disk(layout, used + OLD_SWAP), holes=False)
    node.active(OLD_SWAP, **active)
    (tmp_path / 'etc/fstab').write_text(f'/dev/vda1 / ext4 defaults 0 1\n{node.file} none swap sw 0 0\n')
    return node, []


def heal(tmp_path, lines):
    return swap.ensure(tmp_path / 'none.json', lines.append, root=tmp_path, install_bytes=ENTRY_INSTALL)


@pytest.mark.parametrize('used', [5386, UPDATED])  # перед обновлением с v1.1.5 (свободно 2103) и после него
def test_swap_of_an_older_deploy_is_brought_to_the_planned_size(tmp_path, monkeypatch, used):
    node, lines = old_node(tmp_path, monkeypatch, used=used)
    assert disk(DISK_10, used + OLD_SWAP).free < MIN_FREE + 512 * MIB
    assert heal(tmp_path, lines) is True
    assert node.calls == ['swapoff', 'fallocate', 'mkswap', 'swapon'] and node.sizes == [747]
    assert lines[0] == (f'swap: включённый {node.file} {OLD_SWAP} МиБ занимает место деплоя, '
                        'пересоздаю в 747 МиБ')
    assert (tmp_path / 'etc/fstab').read_text().count(str(node.file)) == 1
    # what the next deploy sees: room for its check, and the healed swap is left alone
    node.active(747)
    node.usage, node.calls, said = disk(DISK_10, used + 747), [], len(lines)
    assert node.usage.free >= MIN_FREE + 512 * MIB
    assert heal(tmp_path, lines) is False
    assert node.calls == [] and len(lines) == said


def test_swap_in_use_above_free_memory_is_left_on(tmp_path, monkeypatch):
    node, lines = old_node(tmp_path, monkeypatch, used_mib=900)
    node.available(500)
    assert heal(tmp_path, lines) is False
    assert node.calls == [] and node.file.stat().st_size == OLD_SWAP * MIB
    assert lines == [f'swap: {node.file} {OLD_SWAP} МиБ занимает место деплоя, но выключить его нельзя: '
                     'в нём 900 МиБ, свободной памяти 500 МиБ; оставлен как есть']
    # the same pages fit into memory: healed
    node.available(1200)
    assert heal(tmp_path, lines) is True and node.sizes == [747]


def test_swap_that_does_not_switch_off_is_left_on(tmp_path, monkeypatch):
    node, lines = old_node(tmp_path, monkeypatch)
    node.swapoff_works = False
    assert heal(tmp_path, lines) is False
    assert node.calls == ['swapoff'] and node.file.stat().st_size == OLD_SWAP * MIB
    assert lines[-1] == f'swap: swapoff {node.file} не прошёл, оставлен как есть'


@pytest.mark.parametrize('swaps', [
    '/dev/vda2 partition 2009084 0 -2\n',
    '/swap.img file 2009084 0 -2\n',
])
def test_swap_that_is_not_our_file_is_not_touched(tmp_path, monkeypatch, swaps):
    node, lines = old_node(tmp_path, monkeypatch)
    # alone, and next to our file: a node somebody set up by hand stays as it is
    for text in (SWAPS_HEAD + swaps, (tmp_path / 'proc/swaps').read_text() + swaps):
        (tmp_path / 'proc/swaps').write_text(text)
        assert heal(tmp_path, lines) is False
    assert node.calls == [] and lines == [] and node.file.stat().st_size == OLD_SWAP * MIB


def test_old_swap_on_a_40gb_entry_is_left_alone(tmp_path, monkeypatch):
    node, lines = old_node(tmp_path, monkeypatch, layout=DISK_40)
    assert heal(tmp_path, lines) is False
    assert node.calls == [] and lines == []


def test_old_swap_with_no_room_for_any_is_removed_with_its_fstab_line(tmp_path, monkeypatch):
    node, lines = old_node(tmp_path, monkeypatch, layout=DISK_8, used=ENTRY_MEASURED)
    assert heal(tmp_path, lines) is True
    assert node.calls == ['swapoff'] and not node.file.exists()
    assert 'пересоздаю' not in lines[0] and 'убираю' in lines[0] and lines[1].startswith('swap пропущен')
    assert (tmp_path / 'etc/fstab').read_text() == '/dev/vda1 / ext4 defaults 0 1\n'
    node.calls = []
    assert heal(tmp_path, lines) is False and node.calls == []


def test_healed_swap_is_not_reshaped_for_a_few_megabytes(tmp_path, monkeypatch):
    # the disk fills up slowly: swap is rebuilt in steps of 256 MiB, not on every deploy
    node = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_10, 6244 + 747), holes=False)
    node.active(747)
    assert MIN_FREE + 512 * MIB - node.usage.free == 100 * MIB
    assert heal(tmp_path, []) is False and node.calls == []
    node.usage = disk(DISK_10, 6444 + 747)
    assert heal(tmp_path, []) is True and node.sizes == [447]


def release(tmp_path, lines, install=ENTRY_INSTALL):
    return swap.release(MIN_FREE, lines.append, proc=tmp_path / 'proc', install_bytes=install)


def test_release_before_the_backup_check_heals_the_node_stuck_after_update(tmp_path, monkeypatch):
    node, lines = old_node(tmp_path, monkeypatch)
    assert node.usage.free == 1807 * MIB < MIN_FREE
    assert release(tmp_path, lines) is True
    assert node.calls == ['swapoff', 'fallocate', 'mkswap', 'swapon'] and node.sizes == [747]
    assert node.disk_usage().free - 747 * MIB >= MIN_FREE + 512 * MIB


def test_release_removes_a_switched_off_file_and_leaves_the_rest(tmp_path, monkeypatch):
    node, lines = SwapNode(tmp_path, monkeypatch, usage=disk(DISK_10, UPDATED + OLD_SWAP), holes=False), []
    node.leftover(OLD_SWAP)
    assert release(tmp_path, lines) is True
    assert node.calls == [] and not node.file.exists()
    assert lines == [f'swap: выключенный {node.file} {OLD_SWAP} МиБ занимает место деплоя, убираю']
    # no file, a container, somebody's partition, a host without /proc/swaps: nothing to give back
    assert release(tmp_path, lines) is False
    node.active(OLD_SWAP)
    node.virt = 'lxc'
    assert release(tmp_path, lines) is False
    node.virt = 'none'
    (tmp_path / 'proc/swaps').write_text(SWAPS_HEAD + '/dev/vda2 partition 2009084 0 -2\n')
    assert release(tmp_path, lines) is False
    (tmp_path / 'proc/swaps').unlink()
    assert release(tmp_path, lines) is False
    assert node.calls == [] and len(lines) == 1 and node.file.exists()


def test_swap_step_passes_the_install_size(monkeypatch):
    seen = {}

    def ensure(manifest, say, **kwargs):
        say('swap: stub')
        seen.update(kwargs, manifest=manifest)

    monkeypatch.setattr(swap, 'ensure', ensure)
    assert host.main(['swap', '--manifest', 'm.json', '--install-bytes', str(ENTRY_INSTALL)]) == 0
    assert seen == {'manifest': 'm.json', 'install_bytes': ENTRY_INSTALL}
    assert host.main(['swap']) == 0 and seen['install_bytes'] == 0


def test_f2b_ignore_skips_garbage():
    assert host.f2b_ignore('127.0.0.1 1 22', 'entry.example', lambda _name: ['not-an-ip;rm']) == ['127.0.0.1/8', '::1']


def exit_venv(tmp_path, wheel_bytes, fail=''):
    """`vpn2 host venv` with a stub python: `-m venv` leaves bin/vpn2, a call with `fail` in it fails."""
    stub = tmp_path / 'python3'
    stub.write_text('#!/bin/sh\n'
                    f'echo "$*" >> {tmp_path}/calls\n'
                    f'case "$*" in *"{fail or "no such call"}"*) exit 1;; esac\n'
                    '[ "$1" = "-m" ] || exit 0\n'
                    'for dir; do :; done\n'
                    'mkdir -p "$dir/bin" && printf \'#!/bin/sh\\necho vpn2\\n\' > "$dir/bin/vpn2" '
                    '&& chmod +x "$dir/bin/vpn2"\n')
    stub.chmod(0o755)
    (tmp_path / 'pip').mkdir(exist_ok=True)
    (tmp_path / 'pip/pip-1-py3-none-any.whl').write_bytes(b'')
    wheel = tmp_path / 'vpn2-1-py3-none-any.whl'
    wheel.write_bytes(wheel_bytes)
    return host.main(['venv', str(wheel), '--root', str(tmp_path / 'root'),
                      '--pip-dir', str(tmp_path / 'pip'), '--python', str(stub)])


def exit_venv_runs(tmp_path):
    return subprocess.run([tmp_path / 'root/venv/bin/vpn2'], capture_output=True, text=True).stdout


def test_exit_venv_is_built_aside_and_switched_by_link(tmp_path, capsys):
    link = tmp_path / 'root/venv'
    assert exit_venv(tmp_path, b'one') == 0
    assert capsys.readouterr().out.endswith(host.CHANGED + '\n')
    first = link.resolve()
    assert link.is_symlink() and first.parent == (tmp_path / 'root/venvs').resolve()
    assert (link / '.vpn2-wheel').read_text() == hashlib.sha256(b'one').hexdigest() + '\n'
    calls = (tmp_path / 'calls').read_text()
    assert '--clear' not in calls and '--system-site-packages' in calls and '--no-index' in calls
    # the same wheel: nothing is called
    assert exit_venv(tmp_path, b'one') == 0
    assert host.CHANGED not in capsys.readouterr().out
    assert (tmp_path / 'calls').read_text() == calls and link.resolve() == first
    # another wheel: a new build, the old one is gone
    assert exit_venv(tmp_path, b'two') == 0
    assert link.resolve() != first and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert [item.name for item in (tmp_path / 'root/venvs').iterdir()] == [link.resolve().name]


def test_exit_venv_aborted_build_leaves_the_current_one(tmp_path):
    link = tmp_path / 'root/venv'
    assert exit_venv(tmp_path, b'one') == 0
    first = link.resolve()
    assert exit_venv(tmp_path, b'two', fail=' install ') == 1
    assert link.resolve() == first and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert (link / '.vpn2-wheel').read_text() == hashlib.sha256(b'one').hexdigest() + '\n'
    assert exit_venv(tmp_path, b'two') == 0
    assert [item.name for item in (tmp_path / 'root/venvs').iterdir()] == [link.resolve().name]


@pytest.mark.parametrize('exchange', [True, False])
def test_exit_venv_directory_of_old_release_becomes_link(tmp_path, monkeypatch, exchange):
    # One rename syscall does the swap, whatever coreutils the host has; two
    # renames are the path only where the syscall is refused.
    seen = []
    real = host.pki._exchange

    def swap_names(source, target):
        seen.append(target.name)
        if not exchange:
            raise OSError(38, 'no renameat2')
        real(source, target)

    monkeypatch.setattr(host.pki, '_exchange', swap_names)
    old = tmp_path / 'root/venv'
    old.mkdir(parents=True)
    (old / '.vpn2-wheel').write_text('old\n')
    assert exit_venv(tmp_path, b'one') == 0
    assert seen == ['venv']
    assert old.is_symlink() and exit_venv_runs(tmp_path) == 'vpn2\n'
    assert sorted(item.name for item in (tmp_path / 'root').iterdir()) == ['venv', 'venvs']
    assert len(list((tmp_path / 'root/venvs').iterdir())) == 1


def test_exit_venv_build_of_running_process_is_kept(tmp_path):
    assert exit_venv(tmp_path, b'one') == 0
    first = (tmp_path / 'root/venv').resolve()
    service = subprocess.Popen(['python3', '-c', 'import time; time.sleep(120)', f'{first}/bin/vpn2'])
    try:
        assert exit_venv(tmp_path, b'two') == 0
        assert first.is_dir()
    finally:
        service.kill()
        service.wait()
    assert exit_venv(tmp_path, b'three') == 0
    assert not first.exists()


def test_exit_venv_build_of_the_same_name_elsewhere_is_not_ours(tmp_path):
    # The build name is the wheel hash: another directory can hold the same one.
    assert exit_venv(tmp_path, b'one') == 0
    first = (tmp_path / 'root/venv').resolve()
    foreign = subprocess.Popen(['python3', '-c', 'import time; time.sleep(120)',
                                f'{tmp_path}/elsewhere/venvs/{first.name}/bin/vpn2'])
    try:
        assert host._venv_in_use(first) is False
        assert exit_venv(tmp_path, b'two') == 0
        assert not first.exists()
    finally:
        foreign.kill()
        foreign.wait()
