"""
of13_switch.py
Open vSwitch switch class used by every Mininet network of the testbed.

A subclass, not functools.partial(OVSSwitch, protocols=...): Mininet enables
batch startup only when the switch class has `batchStartup`, which a partial
object lacks. Without it each switch sends its single ovs-vsctl command
through its shell's pty, where lines are cut at 4095 characters, so a switch
with many ports (star N=64: ~4600 characters) silently never gets created.
"""

from mininet.node import OVSSwitch

# Every controller is spoken to in OpenFlow 1.3 (the apps and the wire
# analysis are written for it; Ryu/OS-Ken offer only 1.3 anyway, ONOS would
# otherwise negotiate a newer version).
OF_PROTOCOLS = "OpenFlow13"


class OF13Switch(OVSSwitch):
    def __init__(self, name, **params):
        params.setdefault("protocols", OF_PROTOCOLS)
        super().__init__(name, **params)
