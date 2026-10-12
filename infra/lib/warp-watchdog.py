#!/usr/bin/env python3
"""Exit WARP recovery. Timer and deployment share a persistent restart budget."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import syslog
import time
import urllib.error
import urllib.parse
import urllib.request


DEAD_AFTER = 180
BACKOFF = (300, 600, 1200, 1800)
MAX_PER_HOUR = 3
HEALTHY_RESET = 300
BOOTSTRAP_SECONDS = 540
BOOTSTRAP_RESTARTS = 2
PROBE_URLS = ('https://www.gstatic.com/generate_204', 'https://cp.cloudflare.com/generate_204')


def log(message, journal=False):
    print(message, flush=True)
    if journal:
        syslog.openlog('anysda-warp-watchdog')
        syslog.syslog(message)


def enabled(config):
    # sing-box 1.11+ keeps WireGuard in endpoints; the disabled exit renders
    # a placeholder peer on the loopback address.
    for endpoint in config.get('endpoints', []):
        if endpoint.get('tag') != 'warp' or endpoint.get('type') != 'wireguard':
            continue
        peers = endpoint.get('peers') or [{}]
        server = peers[0].get('address', '')
        try:
            address = ipaddress.ip_address(server)
            return not (address.is_loopback or address.is_unspecified)
        except ValueError:
            return bool(server) and server.lower().rstrip('.') != 'localhost'
    return False


def probe(config):
    # v1.10.3 /proxies/warp/delay dials this outbound directly, ignoring route
    # selectors. Use HTTPS: this API replaces an http:// URL with its default.
    # Two independent sites reduce false failures of the destination itself.
    api = config['experimental']['clash_api']
    controller = api['external_controller']
    if controller not in ('127.0.0.1:9090', 'localhost:9090', '[::1]:9090'):
        raise ValueError('WARP probe requires the local Clash API on port 9090')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in PROBE_URLS:
        query = urllib.parse.urlencode({'url': url, 'timeout': 5000})
        request = urllib.request.Request(
            f'http://{controller}/proxies/warp/delay?{query}',
            headers={'Authorization': f"Bearer {api['secret']}"})
        try:
            with opener.open(request, timeout=7) as response:
                delay = json.load(response).get('delay')
                if type(delay) in (int, float) and delay > 0:
                    return True
        except (OSError, urllib.error.URLError, ValueError, AttributeError):
            pass
    return False


def decision(state, now, healthy):
    """Mutate the budget BEFORE systemctl, including failed restart attempts."""
    # A backward wall-clock correction must never replenish the hourly budget.
    now = max(now, state.get('last_seen', now))
    state['last_seen'] = now
    state['restarts'] = [stamp for stamp in state.get('restarts', []) if now - stamp < 3600]
    if healthy:
        recovered = state.pop('bad_since', None) is not None
        state.setdefault('healthy_since', now)
        if now - state['healthy_since'] >= HEALTHY_RESET:
            state['level'] = 0
        return 'recovered' if recovered else 'healthy'
    state.pop('healthy_since', None)
    state.setdefault('bad_since', now)
    if now - state['bad_since'] < DEAD_AFTER:
        return 'waiting'
    if len(state['restarts']) >= MAX_PER_HOUR or now < state.get('next_restart', 0):
        return 'limited'
    level = min(state.get('level', 0), len(BACKOFF) - 1)
    state['next_restart'] = now + BACKOFF[level]
    state['level'] = min(level + 1, len(BACKOFF) - 1)
    state['restarts'].append(now)
    state['bad_since'] = now
    return 'restart'


def save_state(path, state):
    candidate = path.with_suffix('.tmp')
    with candidate.open('w') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    candidate.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def tick(config, path, journal=False):
    # Corrupt/unreadable state fails closed: losing it must not reset the cap.
    state = json.loads(path.read_text()) if path.exists() else {}
    healthy = probe(config)
    action = decision(state, time.time(), healthy)
    save_state(path, state)
    if action == 'restart':
        log(f'WARP restart: sing-box; attempts/hour={len(state["restarts"])}; '
            f'backoff={state["next_restart"] - state["last_seen"]:.0f}s', journal)
        try:
            subprocess.run(['systemctl', 'restart', 'sing-box'], check=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            log(f'WARNING WARP restart failed: {type(error).__name__}', journal)
    elif action == 'recovered':
        log('WARP recovered: HTTPS response through warp', journal)
    elif not healthy:
        log(f'WARP unavailable: {action}; attempts/hour={len(state["restarts"])}', journal)
    return action


def run(config, path, bootstrap=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('WARNING WARP check already running; next timer will retry', bootstrap)
            return
        deadline = time.monotonic() + BOOTSTRAP_SECONDS
        attempts = 0
        while True:
            action = tick(config, path, bootstrap)
            if not bootstrap or action in ('healthy', 'recovered'):
                return
            attempts += action == 'restart'
            if attempts >= BOOTSTRAP_RESTARTS:
                # Give the last restart a chance before the warning.
                time.sleep(10)
                if probe(config):
                    log('WARP recovered after deployment restart', bootstrap)
                    return
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(10)
        log('WARNING WARP still unavailable; exit continues without WARP; timer will retry', True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/sing-box/config.json'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/anysda-warp-watchdog/state.json'))
    parser.add_argument('--enabled', action='store_true')
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.enabled:
        return 0 if enabled(config) else 1
    if not enabled(config):
        return 0
    try:
        run(config, args.state, args.bootstrap)
    except (OSError, ValueError, TypeError, KeyError) as error:
        log(f'WARNING WARP watchdog failed closed: {type(error).__name__}', args.bootstrap)
        return 0 if args.bootstrap else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
