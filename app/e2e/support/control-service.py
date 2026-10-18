"""Run the production Unix server in the disposable e2e network namespace."""
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, '/usr/local/lib/vpn2-control')
from backend import Backend  # noqa: E402
from contract import load_pools  # noqa: E402
from server import serve  # noqa: E402

state = Path('/var/lib/vpn2-control')
state.mkdir(mode=0o700, exist_ok=True)
path = Path('/run/vpn2-control/control.sock')
path.parent.mkdir(exist_ok=True)
path.unlink(missing_ok=True)
with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(str(path))
    os.chown(path, 10001, 10001)
    os.chmod(path, 0o600)
    listener.listen(16)
    serve(listener, Backend(), {0, 10001}, load_pools())
