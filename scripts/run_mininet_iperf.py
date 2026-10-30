import argparse
import json
import os
import random
import subprocess
import time
from pathlib import Path
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from mininet.net import Mininet
from mininet.node import Host, RemoteController
from mininet.log import setLogLevel
from mininet.link import TCLink

from topology.star_topo import StarTopo
from topology.fat_tree_topo import FatTreeTopo
from topology.mesh_topo import MeshTopo

from iperf_pool import PORT_BASE, PortPool, pool_size
from of13_switch import OF13Switch, OF_PROTOCOLS


# "tree" is the documented name of the two-tier tree; data keep recording
# "fattree" so existing run IDs, summaries and plots stay consistent.
TOPO_ALIASES = {"tree": "fattree"}

# A run whose switches never reach the controller measures nothing; abort it
# (exit code below) so no summary row is written and a sweep can retry it.
SWITCH_CONNECT_EXIT = 3


def canonical_topo(topo_name):
    topo_name = topo_name.lower()
    return TOPO_ALIASES.get(topo_name, topo_name)


def get_topo(topo_name, n):
    topo_name = canonical_topo(topo_name)
    if topo_name == "star":
        return StarTopo(n=n)
    elif topo_name == "fattree":
        return FatTreeTopo(n=n)
    elif topo_name == "mesh":
        return MeshTopo(n=n)
    else:
        raise ValueError(f"Unknown topology: {topo_name}. "
                         "Choose from: star, tree (alias: fattree), mesh")


def limit_host_links(net, bw_mbit):
    """Apply a TCLink (tc HTB) bandwidth limit to both ends of every
    host-switch link; switch-switch links are left unlimited.
    Returns the number of links limited."""
    if not bw_mbit:
        return 0
    n = 0
    for link in net.links:
        if isinstance(link.intf1.node, Host) or isinstance(link.intf2.node, Host):
            link.intf1.config(bw=bw_mbit)
            link.intf2.config(bw=bw_mbit)
            n += 1
    return n


class PhaseLog:
    """Appends phase start/end epoch timestamps to the run's phase_times.tsv
    (shared with run_experiment.sh, which merges it into phase_times.json)."""

    def __init__(self, path):
        self.path = path
        self._t0 = {}

    def _write(self, name, event, ts):
        with open(self.path, "a") as f:
            f.write(f"{name}\t{event}\t{ts:.6f}\n")

    def start(self, name):
        ts = time.time()
        self._t0[name] = ts
        self._write(name, "start", ts)
        print(f"[PHASE] {name} start", flush=True)

    def end(self, name):
        ts = time.time()
        self._write(name, "end", ts)
        print(f"[PHASE] {name} end ({ts - self._t0.get(name, ts):.2f}s)", flush=True)


def wait_servers_listening(hosts, pool, timeout):
    """Poll `ss` in each host namespace until all pool ports are listening."""
    pending = {i: set(pool.host_ports(i)) for i in range(len(hosts))}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        for i in list(pending):
            out = hosts[i].cmd("ss -ltnH")
            listening = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        listening.add(int(parts[3].rsplit(":", 1)[1]))
                    except (IndexError, ValueError):
                        pass
            pending[i] -= listening
            if not pending[i]:
                del pending[i]
        if pending:
            time.sleep(0.5)
    return not pending


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--controller_ip", type=str, default="127.0.0.1")
    ap.add_argument("--controller_port", type=int, default=6653)
    ap.add_argument("--flows_per_sec", type=int, required=True)
    ap.add_argument("--flow_duration", type=int, default=2)
    ap.add_argument("--total_time", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--outdir", type=str, required=True)
    ap.add_argument("--topo", type=str, default="star",
                    help="Topology type: star | tree (alias: fattree) | mesh (experimental)")
    # iperf3 server pool (see scripts/iperf_pool.py)
    ap.add_argument("--port_guard", type=float, default=1.0,
                    help="Seconds a port stays reserved after flow_duration")
    ap.add_argument("--server_safety", type=float, default=1.5)
    ap.add_argument("--server_z", type=float, default=3.0)
    ap.add_argument("--server_min", type=int, default=4)
    ap.add_argument("--server_cap", type=int, default=320)
    ap.add_argument("--server_ready_timeout", type=float, default=30.0)
    ap.add_argument("--switch_connect_timeout", type=float, default=30.0,
                    help="Seconds to wait for every switch to connect to the "
                         "controller before aborting the run")
    ap.add_argument("--host_bw", type=float, default=0.0,
                    help="TCLink bandwidth (Mbit/s) on host-switch links, "
                         "enforced by kernel tc/HTB; 0 = no limit, max 1000")
    ap.add_argument("--fq_rate", type=str, default="2M",
                    help="Per-flow iperf3 --fq-rate (kernel socket pacing), "
                         "e.g. 2M; 0 = unpaced")
    ap.add_argument("--code_hash", default="",
                    help="scripts/code_hash.py value of the run (recorded only)")
    args = ap.parse_args()

    random.seed(args.seed)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    iperf_dir = outdir / "iperf_json"
    iperf_dir.mkdir(exist_ok=True)

    meta = {
        "n": args.n,
        "topology": canonical_topo(args.topo),
        "controller_ip": args.controller_ip,
        "controller_port": args.controller_port,
        "flows_per_sec": args.flows_per_sec,
        "flow_duration": args.flow_duration,
        "total_time": args.total_time,
        "total_flows": args.flows_per_sec * args.total_time,
        "seed": args.seed,
    }

    hold_s = args.flow_duration + args.port_guard
    servers_per_host = pool_size(
        args.flows_per_sec, hold_s, args.n,
        safety=args.server_safety, z=args.server_z,
        min_servers=args.server_min, cap=args.server_cap,
    )
    pool = PortPool(args.n, servers_per_host, busy_s=hold_s, base=PORT_BASE)
    meta.update({
        "servers_per_host": servers_per_host,
        "server_port_base": PORT_BASE,
        "port_hold_s": hold_s,
        "host_bw_mbit": args.host_bw,
        "fq_rate": args.fq_rate,
        "of_protocols": OF_PROTOCOLS,
        "code_hash": args.code_hash,
    })
    with open(outdir / "traffic_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    setLogLevel("warning")

    phase = PhaseLog(outdir / "phase_times.tsv")

    phase.start("mininet_start")
    topo = get_topo(args.topo, args.n)

    net = Mininet(
        topo=topo,
        controller=None,
        switch=OF13Switch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True,
        build=True,
    )
    meta["host_links_limited"] = limit_host_links(net, args.host_bw)
    net.addController(
        "c0",
        controller=RemoteController,
        ip=args.controller_ip,
        port=args.controller_port,
    )
    net.start()
    switches_connected = net.waitConnected(timeout=args.switch_connect_timeout)
    meta["switches_connected"] = switches_connected
    phase.end("mininet_start")
    if not switches_connected:
        missing = [s.name for s in net.switches if not s.connected()]
        meta["switches_not_connected"] = missing
        with open(outdir / "traffic_meta.json", "w") as f:
            json.dump(meta, f, indent=2)
        print(f"[ERROR] switches not connected to the controller after "
              f"{args.switch_connect_timeout}s: {', '.join(missing)}; aborting run",
              file=sys.stderr, flush=True)
        net.stop()
        sys.exit(SWITCH_CONNECT_EXIT)

    hosts = [net.get(f"h{i}") for i in range(1, args.n + 1)]

    # Start a pool of iperf3 servers per host. Output goes to DEVNULL: an
    # undrained PIPE (Mininet's popen default) fills after a few dozen tests
    # and blocks the server.
    phase.start("server_pool_start")
    server_procs = []
    for i, h in enumerate(hosts):
        for port in pool.host_ports(i):
            p = h.popen(["iperf3", "-s", "-p", str(port)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server_procs.append(p)

    phase.end("server_pool_start")

    # Wait until every server is listening (replaces the fixed 1 s sleep)
    phase.start("server_ready_wait")
    time.sleep(1.0)
    servers_ready = wait_servers_listening(
        hosts, pool, args.server_ready_timeout)
    meta["servers_ready"] = servers_ready
    if not servers_ready:
        print("[WARN] not all iperf3 servers listening after "
              f"{args.server_ready_timeout}s", file=sys.stderr)
    phase.end("server_ready_wait")

    total_flows = args.flows_per_sec * args.total_time
    interval = 1.0 / float(args.flows_per_sec)

    active = []  # launched client procs not yet reaped
    launched = 0
    reap_every = max(1, args.flows_per_sec // 4)  # ~4 sweeps per second
    pacing = [] if args.fq_rate in ("", "0") else ["--fq-rate", args.fq_rate]
    phase.start("traffic_launch")
    start_ts = time.time()
    next_launch = start_ts

    for idx in range(total_flows):
        src = random.choice(hosts)
        dst = random.choice([h for h in hosts if h != src])
        dst_i = int(dst.name[1:]) - 1

        now = time.time()
        if now < next_launch:
            time.sleep(next_launch - now)
        port = pool.acquire(dst_i, time.time())

        outpath = iperf_dir / f"flow_{idx:06d}_{src.name}_to_{dst.name}.json"
        errpath = iperf_dir / f"flow_{idx:06d}_{src.name}_to_{dst.name}.stderr.txt"
        out_f = open(outpath, "w")
        err_f = open(errpath, "w")

        p = src.popen(
            ["iperf3", "-c", dst.IP(), "-p", str(port), "-t", str(args.flow_duration),
             *pacing, "-J"],
            stdout=out_f,
            stderr=err_f,
        )
        # The child holds its own copies; closing ours keeps the parent's
        # open-file count bounded at high FPS.
        out_f.close()
        err_f.close()
        active.append(p)
        launched += 1
        # Reap finished clients (poll() = non-blocking waitpid) so zombies
        # don't accumulate during the launch loop.
        if launched % reap_every == 0:
            active = [q for q in active if q.poll() is None]
        next_launch += interval
    launch_end = time.time()
    phase.end("traffic_launch")

    # Wait for all clients
    phase.start("client_drain")
    for p in active:
        p.wait()
    phase.end("client_drain")

    launch_s = launch_end - start_ts
    meta.update({
        "port_collisions": pool.collisions,
        "actual_launch_duration_s": round(launch_s, 3),
        "launched_flows": launched,
        "achieved_fps": round(launched / launch_s, 3) if launch_s > 0 else None,
    })
    with open(outdir / "traffic_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # Stop servers
    phase.start("mininet_stop")
    for p in server_procs:
        try:
            p.terminate()
        except Exception:
            pass

    net.stop()
    phase.end("mininet_stop")


if __name__ == "__main__":
    main()
