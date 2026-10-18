"""Wave contract. Subscribers attach before the agent starts its event loop.

Callbacks must be idempotent by (emergency_id, generation). Restarting an
existing leader restores its state without emitting a new leader event.
Step-down immediately stops issuance; connection retirement is a separate
emergency-end event, after the replacement bundle is ready (at most 600 s).
"""

from collections import defaultdict
from collections.abc import Callable

_listeners: dict[str, list[Callable]] = defaultdict(list)


def subscribe(event: str, callback: Callable) -> None:
    if event not in {"on_emergency_leader", "on_step_down",
                     "on_entry_recovered", "on_emergency_end"}:
        raise ValueError("unknown emergency event")
    if callback not in _listeners[event]:
        _listeners[event].append(callback)


def _emit(event: str, *args) -> None:
    for callback in tuple(_listeners[event]):
        callback(*args)


def on_emergency_leader(emergency_id: str, generation: int, autonomous: bool) -> None:
    _emit("on_emergency_leader", emergency_id, generation, autonomous)


def on_step_down(new_leader_exit_id: str | None) -> None:
    _emit("on_step_down", new_leader_exit_id)


def on_entry_recovered(deadline_monotonic: float) -> None:
    _emit("on_entry_recovered", deadline_monotonic)


def on_emergency_end() -> None:
    _emit("on_emergency_end")
