"""Render through the real service adapter, replacing only host daemons."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'infra/control'))
from backend import Backend  # noqa: E402

params = json.load(sys.stdin)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    state = root / 'state'
    state.mkdir()
    config = root / 'etc/swanctl/conf.d/anysda-clients.conf'
    config.parent.mkdir(parents=True)
    config.write_text('')
    backend = Backend(root, state)
    backend.command = Mock(return_value='')
    backend.observe = Mock(return_value={'devices': [item['device_id'] for item in params['clients']], 'sas': []})
    backend.execute('ikev2.apply', dict(params, version=1))
    sys.stdout.write(config.read_text())
