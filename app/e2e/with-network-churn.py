#!/usr/bin/env python3
"""Run a command while adding/removing an active host veth pair (Linux).

E2E_IP="sudo -n ip" python3 e2e/with-network-churn.py bash e2e/run-local.sh
Only the pair created here is removed; no default routes are changed.
"""

import os
import shlex
import signal
import subprocess
import sys
import threading


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: with-network-churn.py command [args...]")
    ip = shlex.split(os.environ.get("E2E_IP", "ip"))
    suffix = f"{os.getpid() % 65536:04x}"
    name, peer = f"vpn2-e2e-a{suffix}", f"vpn2-e2e-b{suffix}"
    stop = threading.Event()
    errors = []
    cycles = 0

    def terminate(signum, _frame):
        # timeout и отмена CI должны пройти через finally и убрать veth.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)

    def run_ip(*args):
        subprocess.run([*ip, *args], check=True, stdout=subprocess.DEVNULL)

    def churn():
        nonlocal cycles
        try:
            while not stop.is_set():
                run_ip("link", "add", name, "type", "veth", "peer", "name", peer)
                try:
                    run_ip("addr", "add", "198.18.0.1/32", "dev", name)
                    run_ip("link", "set", name, "up")
                    run_ip("link", "set", peer, "up")
                    stop.wait(0.3)
                finally:
                    run_ip("link", "del", name)
                cycles += 1
                stop.wait(0.3)
        except (OSError, subprocess.CalledProcessError) as error:
            errors.append(str(error))

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        with subprocess.Popen(sys.argv[1:], start_new_session=True) as command:
            try:
                result = command.wait()
            except BaseException:
                os.killpg(command.pid, signal.SIGTERM)
                command.wait()
                raise
    finally:
        stop.set()
        worker.join()
        print(f"network churn: cycles={cycles}, errors={errors}", flush=True)
    return result or int(bool(errors) or cycles == 0)


if __name__ == "__main__":
    sys.exit(main())
