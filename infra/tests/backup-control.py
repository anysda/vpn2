"""Real Unix API for backup tests; VPN daemons are deliberately absent."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import Mock

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'infra/control'))
from backend import Backend  # noqa: E402
from server import serve  # noqa: E402

pools = json.loads((root / 'app/shared/vpn-pools.json').read_text())
with tempfile.TemporaryDirectory() as work, socket.socket(socket.AF_UNIX) as listener:
    backend = Backend(state=Path(work), pools=pools)
    backend.command = Mock(return_value='')
    listener.bind(sys.argv[2])
    listener.listen(16)
    print('ready', flush=True)
    serve(listener, backend, {os.getuid()}, pools)
