"""Atomic file writes, JSON helpers and inter-process locks (stdlib only)."""
import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

_MISSING = object()
_POLL = 0.01


class LockBusy(Exception):
    """The lock is held by someone else (non-blocking, or timeout expired)."""


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path, body, mode=0o600, encoding=None):
    """Write body (str or bytes) so readers see the old or the new file, never half.

    The temporary file lives beside the target; its mode is set before the swap.
    A symbolic link at path is replaced by a regular file, its target is untouched.
    """
    path = Path(path)
    raw = body if isinstance(body, bytes) else body.encode(encoding or 'utf-8')
    descriptor, name = tempfile.mkstemp(prefix='.fsutil-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise
    _fsync_directory(path.parent)


def read_json(path, default=_MISSING):
    """Parse a JSON file; a missing file gives default if passed, bad JSON always raises."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    return json.loads(text)


def write_json(path, data, mode=0o600):
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    atomic(path, text + '\n', mode=mode, encoding='utf-8')


@contextlib.contextmanager
def lock(path, *, blocking=True, timeout=None):
    """Exclusive flock on path (created if missing); LockBusy if it cannot be taken."""
    with open(path, 'a') as handle:
        if blocking and timeout is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout if blocking else None
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is None or time.monotonic() >= deadline:
                        raise LockBusy(str(path)) from None
                    time.sleep(_POLL)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
