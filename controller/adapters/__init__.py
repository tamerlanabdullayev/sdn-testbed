"""Controller adapter registry, looked up by name (like the topology factory)."""

from .base import ControllerAdapter, ControllerNotReady
from .onos import OnosAdapter
from .python_managers import OSKenAdapter, RyuAdapter

ADAPTERS = {cls.name: cls for cls in (RyuAdapter, OSKenAdapter, OnosAdapter)}
ALIASES = {"os-ken": "osken", "os_ken": "osken"}


def canonical_controller(name):
    name = name.lower()
    return ALIASES.get(name, name)


def get_adapter(name, root):
    name = canonical_controller(name)
    if name not in ADAPTERS:
        raise ValueError(f"Unknown controller: {name}. "
                         f"Choose from: {', '.join(sorted(ADAPTERS))}")
    return ADAPTERS[name](root)


def all_process_patterns():
    return [p for cls in ADAPTERS.values() for p in cls.process_patterns]
