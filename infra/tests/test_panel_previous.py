"""The retained previous panel stays stopped across a Docker or host restart."""
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_deploy import ROOT

DOCKER = '''
declare -A policy=([anysda-vpn2]=always)
docker() {
  local last=${*: -1} arg
  case $1 in
    inspect) [[ -n ${policy[$last]+set} ]] || return 1; [[ $2 != --format ]] || echo sha256:old ;;
    rm) unset "policy[$last]" ;;
    rename) policy[$3]=${policy[$2]}; unset "policy[$2]" ;;
    update) for arg; do [[ $arg != --restart=* ]] || policy[$last]=${arg#--restart=}; done ;;
  esac
  return 0
}
python3() { return 0; }
panel_health() { return 0; }
'''


class PanelPreviousTests(unittest.TestCase):
    def run_transaction(self, steps):
        library = ROOT / 'infra/lib/panel-transaction.sh'
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'previous-schema.json').write_text('{}')
            script = f'source {shlex.quote(str(library))}\npanel_state={shlex.quote(directory)}\nPANEL_IMAGE=new\n'
            script += DOCKER + steps + '\nfor name in "${!policy[@]}"; do echo "$name=${policy[$name]}"; done\n'
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        return dict(line.split('=', 1) for line in run.stdout.splitlines() if '=' in line)

    def test_previous_panel_is_not_restarted_by_docker(self):
        self.assertEqual(self.run_transaction('panel_begin; trap - EXIT'), {'anysda-vpn2-previous': 'no'})

    def test_rolled_back_panel_restarts_with_docker_again(self):
        policy = self.run_transaction('panel_begin; trap - EXIT; panel_rollback >/dev/null')
        self.assertEqual(policy, {'anysda-vpn2': 'always'})


if __name__ == '__main__':
    unittest.main()
