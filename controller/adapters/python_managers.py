"""
Adapters for the Python controllers (Ryu, OS-Ken). Both run the framework's
manager entry point in a chosen interpreter, so the PID is the controller
itself and the interpreter is explicit and recorded.
"""

import os
import platform
import subprocess
from pathlib import Path

from .base import ControllerAdapter


class PythonManagerAdapter(ControllerAdapter):
    manager_module = None    # e.g. "ryu.cmd.manager"
    dist_name = None         # e.g. "ryu" (for importlib.metadata)
    app_file = None          # relative to the project root
    python_env = None        # env var overriding the interpreter

    def python(self):
        override = os.environ.get(self.python_env) if self.python_env else None
        return override or self.default_python()

    def default_python(self):
        raise NotImplementedError

    def command(self, outdir, port):
        launcher = (f"import sys; from {self.manager_module} import main; "
                    "sys.exit(main())")
        return [self.python(), "-c", launcher,
                "--ofp-tcp-listen-port", str(port),
                str(self.root / self.app_file)]

    def version(self):
        probe = ("import importlib.metadata as m, platform, sys; "
                 f"print(m.version({self.dist_name!r})); "
                 "print(platform.python_version()); print(sys.executable)")
        out = subprocess.run([self.python(), "-c", probe], cwd="/",
                             capture_output=True, text=True, check=True).stdout.split("\n")
        return {"controller": self.name, "framework_version": out[0],
                "runtime": f"CPython {out[1]}",
                "python_version": out[1], "python_executable": out[2],
                "app_file": self.app_file}


class RyuAdapter(PythonManagerAdapter):
    name = "ryu"
    process_patterns = ("[r]yu-manager", "[r]yu.cmd.manager")
    manager_module = "ryu.cmd.manager"
    dist_name = "ryu"
    app_file = "controller/ryu_instrumented_switch.py"
    python_env = "RYU_PYTHON"

    def default_python(self):
        for venv in ("venv_py310", "venv"):
            p = self.root / venv / "bin" / "python"
            if p.exists():
                return str(p)
        raise FileNotFoundError("no venv_py310/ or venv/ for Ryu")


class OSKenAdapter(PythonManagerAdapter):
    name = "osken"
    process_patterns = ("[o]sken-manager", "[o]s_ken.cmd.manager")
    manager_module = "os_ken.cmd.manager"
    dist_name = "os-ken"
    app_file = "controller/osken_instrumented_switch.py"
    python_env = "OSKEN_PYTHON"

    def default_python(self):
        # venv_osken_py310 (requirements-osken.txt): os-ken 2.8.1 on the same
        # Python 3.10 / eventlet 0.33.3 as Ryu, so the two differ only in the
        # framework. Fallback: the apt package (python3-os-ken) on the system
        # Python. (venv_py310's os-ken 4.2.2 has no os_ken.cmd and cannot run.)
        p = self.root / "venv_osken_py310" / "bin" / "python"
        return str(p) if p.exists() else "/usr/bin/python3"
