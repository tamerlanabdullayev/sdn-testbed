#!/usr/bin/env python3
"""
warmup.py: exercise the controller's Packet-In path before the measured run
(e.g. so a JIT-compiled controller is warm). Run with sudo.

Each round builds a one-switch star with --hosts hosts and does an all-pairs
ping. Rounds use their own datapath IDs (ff:ff:..:<round>) and locally
administered MACs (02:77:00:00:<round>:<host>), so nothing they leave in the
controller's MAC table can match the measured network (dpids 1..64, tree
core 1000, MACs 00:00:00:00:00:xx). The controller is not restarted
afterwards.
"""

import argparse
import time

from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.topo import Topo

from of13_switch import OF13Switch

WARMUP_DPID_BASE = 0xFFFF000000000000


class WarmupStar(Topo):
    def build(self, rnd=0, n=16):
        sw = self.addSwitch("w1", dpid=f"{WARMUP_DPID_BASE + rnd:016x}")
        for i in range(1, n + 1):
            h = self.addHost(f"wh{i}", mac=f"02:77:00:00:{rnd:02x}:{i:02x}",
                             ip=f"10.77.{rnd}.{i}/16")
            self.addLink(h, sw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller_ip", default="127.0.0.1")
    ap.add_argument("--controller_port", type=int, default=6653)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--hosts", type=int, default=16)
    args = ap.parse_args()
    if not 1 <= args.hosts <= 254 or not 0 <= args.rounds <= 255:
        raise SystemExit("--hosts must be 1..254, --rounds 0..255")

    setLogLevel("warning")
    for rnd in range(args.rounds):
        t0 = time.time()
        net = Mininet(topo=WarmupStar(rnd=rnd, n=args.hosts), controller=None,
                      switch=OF13Switch,
                      autoStaticArp=True, build=True)
        net.addController("c0", controller=RemoteController,
                          ip=args.controller_ip, port=args.controller_port)
        net.start()
        loss = net.pingAll(timeout=1)
        net.stop()
        print(f"[WARMUP] round {rnd + 1}/{args.rounds}: {args.hosts} hosts, "
              f"ping loss {loss:.1f}%, {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
