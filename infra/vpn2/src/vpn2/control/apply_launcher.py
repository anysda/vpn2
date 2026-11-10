#!/usr/bin/python3 -I
"""/usr/local/sbin/vpn2-apply: only the root-owned installation is importable."""
import sys

sys.path.insert(0, '/usr/local/lib/vpn2-control')
from vpn2.cli.control import main  # noqa: E402

sys.exit(main(['apply', *sys.argv[1:]]))
