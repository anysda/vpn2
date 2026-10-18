"""Only the root-owned installation is importable alongside the stdlib."""
import sys

sys.path.insert(0, '/usr/local/lib/vpn2-control')
from server import main  # noqa: E402

main()
