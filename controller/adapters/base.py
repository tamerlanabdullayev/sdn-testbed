"""
ControllerAdapter: what the orchestrator needs from a controller.

A controller app must follow the output contract in docs/ADAPTER.md (Part 1:
forwarding logic, OUTDIR, flow_events.csv, controller_metrics.csv). The
adapter only covers the process lifecycle:

    start(outdir, port, cpus=None)
                          launch in the background; logs go to outdir; cpus
                          (set of CPU ids) pins the process and its threads
    wait_ready(timeout)   block until switches can connect; returns seconds
    stop()                SIGTERM, then SIGKILL after a grace period
    pid                   PID of the process that does the work (psutil target)
    version()             dict recorded in controller_meta.json
"""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path


class ControllerNotReady(RuntimeError):
    pass


class ControllerAdapter:
    name = None
    # pkill -f patterns for stale processes left by an aborted run; write
    # them as "[x]yz" so they do not match the pkill/sudo command line itself
    process_patterns = ()

    def __init__(self, root):
        self.root = Path(root)
        self.proc = None
        self.port = None
        self._stdout = None
        self._stderr = None

    # ---- to implement -------------------------------------------------------

    def command(self, outdir, port):
        """argv that runs the controller in the foreground (no wrapper process)."""
        raise NotImplementedError

    def version(self):
        return {}

    # ---- lifecycle ----------------------------------------------------------

    def env(self, outdir, port):
        env = dict(os.environ)
        env["OUTDIR"] = str(outdir)
        return env

    def start(self, outdir, port, cpus=None):
        outdir = Path(outdir)
        self.port = port
        self._stdout = open(outdir / f"{self.name}_stdout.log", "w")
        self._stderr = open(outdir / f"{self.name}_stderr.log", "w")
        # cwd=outdir: keeps the project root (which holds a ryu/ source tree)
        # off sys.path, so the installed framework is the one imported.
        # Affinity is set in the child before exec, so the PID stays the
        # controller's own (no taskset wrapper) and every thread inherits it.
        pin = (lambda: os.sched_setaffinity(0, cpus)) if cpus else None
        self.proc = subprocess.Popen(
            self.command(outdir, port), cwd=outdir, env=self.env(outdir, port),
            stdout=self._stdout, stderr=self._stderr, preexec_fn=pin)
        (outdir / f"{self.name}_pid.txt").write_text(f"{self.proc.pid}\n")
        return self.proc.pid

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def wait_ready(self, timeout):
        """Default: poll until the OpenFlow port accepts TCP connections."""
        t0 = time.time()
        deadline = t0 + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ControllerNotReady(
                    f"{self.name} exited with code {self.proc.returncode} before ready")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return time.time() - t0
            except OSError:
                time.sleep(0.1)
        raise ControllerNotReady(f"{self.name} not listening on {self.port} after {timeout}s")

    def stop(self, grace=5.0):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for f in (self._stdout, self._stderr):
            if f:
                f.close()
