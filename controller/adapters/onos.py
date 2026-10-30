"""
ONOS 2.7.0 adapter. Install with scripts/setup_onos.sh (ONOS goes to
ONOS_HOME, default ~/.local/opt/onos-2.7.0: ONOS's launch scripts do not
support paths with spaces). The app is controller/onos-instrumented-l2.

Per run: Karaf's data/ is wiped (no state carries over between runs), then
bin/onos-service starts with only drivers + openflow-base + the app active
(no fwd, no LLDP link discovery, no host provider). Ready = the app is ACTIVE
(REST) and port 6653 accepts connections. PID = the JVM (child of Karaf's
launcher shell).

Diagnostics (off by default; they are recorded by version()):
- ONOS_GC_LOG=1: JVM GC and safepoint (stop-the-world) log, copied to
  <OUTDIR>/onos_gc.log after the run.
- ONOS_DEBUG_LOGGERS=1 (or a comma-separated list of logger names): DEBUG
  level for those loggers (default DEBUG_LOGGERS), set in Karaf's
  etc/org.ops4j.pax.logging.cfg for this run only; Karaf's karaf.log* is
  copied to <OUTDIR>/onos_karaf.log*. DEBUG logging slows ONOS down: such
  runs are for diagnosis, not for results.
"""

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from base64 import b64encode
from pathlib import Path

from .base import ControllerAdapter, ControllerNotReady

APP_NAME = "org.sdntestbed.instrumentedl2"
REST = "http://127.0.0.1:8181/onos/v1"
AUTH = "Basic " + b64encode(b"onos:rocks").decode()

# Packet-In path: OpenFlow channel/dispatch queues, packet provider, PacketManager
DEBUG_LOGGERS = ("org.onosproject.openflow.controller.impl",
                 "org.onosproject.provider.of.packet.impl",
                 "org.onosproject.net.packet.impl")
LOG_CFG = "org.ops4j.pax.logging.cfg"
DEBUG_BEGIN = "# >>> sdn-testbed ONOS_DEBUG_LOGGERS (removed after the run)"
DEBUG_END = "# <<< sdn-testbed ONOS_DEBUG_LOGGERS"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rest_get(path, timeout=1.0):
    req = urllib.request.Request(REST + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


class OnosAdapter(ControllerAdapter):
    name = "onos"
    # [x]... so the pattern does not match the pkill/sudo command line itself
    process_patterns = ("[o]rg.apache.karaf.main.Main", "[o]nos-service server")

    def __init__(self, root):
        super().__init__(root)
        self.home = Path(os.environ.get("ONOS_HOME", Path.home() / ".local/opt/onos-2.7.0"))
        self.java_home = os.environ.get("ONOS_JAVA_HOME", "/usr/lib/jvm/java-11-openjdk-amd64")
        self.heap = os.environ.get("ONOS_HEAP", "2g")
        self.fresh_data = os.environ.get("ONOS_FRESH_DATA", "1") == "1"
        self.gc_log = os.environ.get("ONOS_GC_LOG", "0") == "1"
        loggers = os.environ.get("ONOS_DEBUG_LOGGERS", "0").strip()
        self.debug_loggers = (list(DEBUG_LOGGERS) if loggers == "1" else
                              [] if loggers in ("", "0") else
                              [x.strip() for x in loggers.split(",") if x.strip()])
        self._outdir = None
        self.oar = self.root / "controller/onos-instrumented-l2/target/instrumented-l2-1.0.0.oar"
        self.installed_oar = self.home / "apps" / APP_NAME / f"{APP_NAME}.oar"
        self._java_pid = None

    def karaf_dir(self):
        dirs = sorted(self.home.glob("apache-karaf-*"))
        if not dirs:
            raise FileNotFoundError(f"no apache-karaf-* in {self.home}; run scripts/setup_onos.sh")
        return dirs[0]

    def java_opts(self):
        # onos-service's defaults plus a fixed heap (Karaf's JAVA_MIN/MAX_MEM
        # are ignored once JAVA_OPTS is set) and the JDK >= 11.0.20 ZIP
        # validation switch that ONOS 2.7's bundles need.
        opts = (f"-Xms{self.heap} -Xmx{self.heap} -XX:+UseG1GC -XX:MaxGCPauseMillis=200 "
                "-Dkaraf.log.console=INFO -Dds.lock.timeout.milliseconds=10000 "
                "-Djdk.util.zip.disableZip64ExtraFieldValidation=true")
        if self.gc_log:
            # JAVA_OPTS is word-split unquoted, so the log goes to Karaf's
            # data/ (ONOS_HOME has no spaces) and is copied to OUTDIR in stop().
            opts += f" -Xlog:gc,safepoint:file={self.gc_log_path()}:uptime,time,level,tags"
        return opts

    def gc_log_path(self):
        return self.karaf_dir() / "data" / "log" / "gc.log"

    def _set_debug_loggers(self, loggers):
        """Rewrite the marked block in Karaf's logging config (empty = remove it)."""
        cfg = self.karaf_dir() / "etc" / LOG_CFG
        lines, skip = [], False
        for line in cfg.read_text().splitlines():
            if line == DEBUG_BEGIN:
                skip = True
            elif line == DEBUG_END:
                skip = False
            elif not skip:
                lines.append(line)
        if loggers:
            lines.append(DEBUG_BEGIN)
            for i, name in enumerate(loggers):
                lines += [f"log4j2.logger.sdntestbed{i}.name = {name}",
                          f"log4j2.logger.sdntestbed{i}.level = DEBUG"]
            lines.append(DEBUG_END)
        text = "\n".join(lines) + "\n"
        if text != cfg.read_text():
            cfg.write_text(text)

    def _collect_logs(self):
        if not self._outdir:
            return
        log_dir = self.karaf_dir() / "data" / "log"
        if self.gc_log and self.gc_log_path().exists():
            shutil.copy2(self.gc_log_path(), self._outdir / "onos_gc.log")
        if self.debug_loggers:
            for f in sorted(log_dir.glob("karaf.log*")):
                shutil.copy2(f, self._outdir / f.name.replace("karaf.log", "onos_karaf.log"))

    def env(self, outdir, port):
        env = super().env(outdir, port)
        env.update({"JAVA_HOME": self.java_home,
                    "PATH": f"{self.java_home}/bin:{env.get('PATH', '')}",
                    "ONOS_APPS": f"openflow-base,{APP_NAME}",
                    "JAVA_OPTS": self.java_opts()})
        return env

    def command(self, outdir, port):
        if port != 6653:
            raise ValueError("ONOS adapter: OpenFlow port is fixed at 6653")
        return [str(self.home / "bin" / "onos-service"), "server"]

    def start(self, outdir, port, cpus=None):
        if not self.installed_oar.exists():
            raise FileNotFoundError(f"{APP_NAME} not installed in {self.home}; run scripts/setup_onos.sh")
        if self.oar.exists() and sha256(self.oar) != sha256(self.installed_oar):
            raise RuntimeError(f"installed {APP_NAME} differs from {self.oar}; "
                               "rerun scripts/setup_onos.sh")
        if self.fresh_data:
            shutil.rmtree(self.karaf_dir() / "data", ignore_errors=True)
        if self.gc_log:
            self.gc_log_path().parent.mkdir(parents=True, exist_ok=True)
        # Always rewritten, so a block left by an aborted debug run is removed.
        self._set_debug_loggers(self.debug_loggers)
        self._outdir = Path(outdir)
        self._java_pid = None
        return super().start(outdir, port, cpus)

    def _find_java(self):
        try:
            import psutil
        except ImportError:
            return None
        try:
            for c in psutil.Process(self.proc.pid).children(recursive=True):
                if c.name() == "java":
                    return c.pid
        except psutil.NoSuchProcess:
            pass
        return None

    @property
    def pid(self):
        if self._java_pid is None and self.proc:
            self._java_pid = self._find_java()
        return self._java_pid or (self.proc.pid if self.proc else None)

    def wait_ready(self, timeout):
        t0 = time.time()
        deadline = t0 + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ControllerNotReady(f"onos-service exited with code {self.proc.returncode}")
            try:
                if '"state":"ACTIVE"' in rest_get(f"/applications/{APP_NAME}"):
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                        self._java_pid = self._find_java()
                        return time.time() - t0
            except OSError:
                pass
            time.sleep(0.25)
        raise ControllerNotReady(f"ONOS app {APP_NAME} not active after {timeout}s")

    def stop(self, grace=30.0):
        # SIGTERM to the JVM: Karaf shuts down cleanly and the app's
        # deactivate() writes the last controller_metrics row; the launcher
        # shell then exits by itself.
        java = self.pid if self._java_pid else self._find_java()
        if java:
            try:
                os.kill(java, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            for p in (java, self.proc.pid):
                try:
                    os.kill(p, signal.SIGKILL)
                except (ProcessLookupError, TypeError):
                    pass
            self.proc.wait()
        super().stop()
        self._collect_logs()
        if self.debug_loggers:
            self._set_debug_loggers([])

    def version(self):
        out = subprocess.run([f"{self.java_home}/bin/java", "-version"],
                             capture_output=True, text=True).stderr.splitlines()
        return {"controller": self.name,
                "framework_version": (self.home / "VERSION").read_text().strip()
                if (self.home / "VERSION").exists() else "",
                "java_version": out[0] if out else "",
                "runtime": "OpenJDK " + out[0].split('"')[1] if out and '"' in out[0] else "",
                "jvm_heap": self.heap,
                "onos_home": str(self.home),
                "fresh_data": self.fresh_data,
                "gc_log": self.gc_log,
                "debug_loggers": ",".join(self.debug_loggers),
                "app_file": "controller/onos-instrumented-l2",
                "app_sha256": sha256(self.installed_oar) if self.installed_oar.exists() else ""}
