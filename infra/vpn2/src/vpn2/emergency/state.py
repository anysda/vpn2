"""Bounded, crash-safe state; monotonic timestamps are scoped to a host boot."""

import json
import os
from pathlib import Path

MAX_STATE_BYTES = 262144


def boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class State:
    def __init__(self, directory, clock_boot=None):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / 'agent.json'
        self.data = {}
        if self.path.exists():
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError('agent state exceeds limit')
            self.data = json.loads(raw)
        current_boot = clock_boot or boot_id()
        self.same_boot = self.data.get('clock_boot') == current_boot
        self.data['clock_boot'] = current_boot

    def save(self):
        raw = json.dumps(self.data, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('agent state exceeds limit')
        temporary = self.directory / 'agent.json.new'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
