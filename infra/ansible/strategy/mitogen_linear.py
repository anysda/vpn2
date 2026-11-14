"""Стратегия linear на Mitogen: на узле один python за плей вместо нового процесса на каждую задачу.

Ключ хоста - как accept-new у ssh: незнакомый принимается и пишется в known_hosts, сменившийся - отказ.
"""
import ansible_mitogen.connection
import ansible_mitogen.loaders
import ansible_mitogen.strategy

_ssh = ansible_mitogen.connection.CONNECTION_METHOD['ssh']


def _accept_new(spec):
    found = _ssh(spec)
    if found['kwargs']['check_host_keys'] == 'enforce':
        found['kwargs']['check_host_keys'] = 'accept'
    return found


ansible_mitogen.connection.CONNECTION_METHOD['ssh'] = _accept_new


class StrategyModule(ansible_mitogen.strategy.StrategyMixin,  # noqa: V102 - класс находит ansible по имени
                     ansible_mitogen.loaders.strategy_loader.get('linear', class_only=True)):
    pass
