"""Only the root-owned installation is importable alongside the stdlib."""
import sys

sys.path.insert(0, '/usr/local/lib/vpn2-control')
from vpn2.control.server import main  # noqa: E402

main()
