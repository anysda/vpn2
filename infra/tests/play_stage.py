"""site.yml с плеями стадий в каталоге проверки: deploy.sh без узлов."""
import os
from pathlib import Path


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def site_tree(root, stages, hosts='all'):
    """site.yml с плеями стадий и пакет vpn2 там, где его ищет deploy.sh ($DEPLOY_ROOT/vpn2/src)."""
    root = Path(root)
    (root / 'ansible').mkdir(parents=True, exist_ok=True)
    site = root / 'ansible/site.yml'
    known = site.read_text() if site.exists() else ''
    site.write_text(known + ''.join(f'- hosts: {hosts}\n  tags: [{stage}]\n' for stage in stages))
    package = root / 'vpn2/src'
    if package.is_dir() and not package.is_symlink():
        # Пакет в каталоге проверки подставной: стадии читает настоящий модуль.
        (package / 'vpn2').mkdir(exist_ok=True)
        (package / 'vpn2/stages.py').write_text((ROOT / 'infra/vpn2/src/vpn2/stages.py').read_text())
    elif not package.exists():
        (root / 'vpn2').mkdir(exist_ok=True)
        package.symlink_to(ROOT / 'infra/vpn2/src')


def real_site_tree(root):
    """Настоящий site.yml продукта в каталоге проверки."""
    root = Path(root)
    (root / 'ansible').mkdir(parents=True, exist_ok=True)
    (root / 'ansible/site.yml').write_text((ROOT / 'infra/ansible/site.yml').read_text())
    site_tree(root, [])


def site_play(stage):
    """Плей стадии из site.yml продукта."""
    import yaml
    play, = [play for play in yaml.safe_load((ROOT / 'infra/ansible/site.yml').read_text())
             if play['tags'] == [stage]]
    return play
