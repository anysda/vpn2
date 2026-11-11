"""`@as_root` for the wg0.conf tests: lives with the package tests, next to the wg oracle."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vpn2/tests'))
from wg_oracle import REASON, as_root  # noqa: E402,F401
