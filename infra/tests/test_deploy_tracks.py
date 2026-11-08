"""Порядок полного прогона: ru до 20 идёт параллельно экзитам, ключи - один раз до цепочек."""
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def function(source, name):
    return name + '() {' + source.split('\n' + name + '() {', 1)[1].split('\n}', 1)[0] + '\n}\n'


class DeployTracksTests(unittest.TestCase):
    def run_all(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        script = function(source, 'do_all') + function(source, 'run_stage').split('\n  local hosts', 1)[0] + '\n}\n'
        script += '''
for name in print_banner pause_entry_auto_updates install_prereqs do_check preflight_ssh verify_and_rotate_ports print_summary announce_deploy_done release_complete prep_ru_router warn_insecure_exits; do
  eval "$name() { :; }"
done
prep_orchestrator_key() { echo prep-orchestrator; }
prep_entry_mgmt_key() { echo prep-entry; }
expand_hosts() { case "$1" in all) echo 'ru nl';; foreign) echo nl;; *) echo "$1";; esac; }
eval "$(declare -f run_stage | sed '1s/run_stage/_stage_prep/')"
run_stage() { [[ $1 != 00-bootstrap ]] || _stage_prep "$@"; echo "stage $1 $2"; }
run_tracks() { local t; for t in "$@"; do echo "track $t"; for s in $t; do run_stage "${s%%:*}" "${s#*:}"; done; done; }
prefetch_images() { true & _PREFETCH_PID=$!; }
do_all
'''
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.splitlines()

    def test_keys_prepared_once_before_parallel_bootstraps(self):
        lines = self.run_all()
        self.assertEqual(lines.count('prep-orchestrator'), 1, lines)
        self.assertEqual(lines.count('prep-entry'), 1, lines)
        first_track = next(i for i, line in enumerate(lines) if line.startswith('track '))
        self.assertLess(lines.index('prep-entry'), first_track, lines)

    def test_ru_bootstrap_runs_beside_exit_track_before_router(self):
        lines = self.run_all()
        tracks = [line.split(' ', 1)[1].split() for line in lines if line.startswith('track ')]
        self.assertIn(['00-bootstrap:nl', '05-mgmt-mesh:nl', '10-foreign:nl'], tracks)
        self.assertIn(['00-bootstrap:ru', '05-mgmt-mesh:ru', '28-wireguard:ru', '29-openvpn:ru', '19-yt-zapret:ru'],
                      tracks)
        self.assertLess(max(i for i, line in enumerate(lines) if line.startswith('stage ') and line.endswith(' nl')
                            and '99-verify' not in line and '23-emergency' not in line),
                        lines.index('stage 20-ru-router ru'), lines)

    def test_single_bootstrap_stage_still_prepares_keys(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        script = function(source, 'run_stage').split('\n  local hosts', 1)[0] + '\n}\n' + '''
prep_orchestrator_key() { echo prep-orchestrator; }
prep_entry_mgmt_key() { echo prep-entry; }
run_stage 00-bootstrap ru
'''
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.splitlines(), ['prep-orchestrator', 'prep-entry'])


if __name__ == '__main__':
    unittest.main()
