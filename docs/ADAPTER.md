# Controller adapter contract

This document describes what a controller must provide to be benchmarked by
the testbed. Three controllers implement it:

| Controller | App | Adapter |
|---|---|---|
| Ryu 4.34 | `controller/ryu_instrumented_switch.py` | `RyuAdapter` (`controller/adapters/python_managers.py`) |
| OS-Ken | `controller/osken_instrumented_switch.py` | `OSKenAdapter` (same file) |
| ONOS 2.7.0 | `controller/onos-instrumented-l2/` (Java OSGi app) | `OnosAdapter` (`controller/adapters/onos.py`); Part 4 |

One orchestrator, `scripts/run_experiment.py`, runs every controller; it looks
the adapter up by name (`--controller ryu|osken|onos`, or `CONTROLLER=` for the
`run_experiment.sh` wrapper); `scripts/sweep.py` runs the full matrix with
it. The responsibilities are split between the **controller app** (Part 1)
and the **adapter** (Part 2). Part 3 shows the Ryu → OS-Ken port as a worked
example; Part 4 covers ONOS (a Java controller).

---

## Part 1 — Controller app responsibilities

### 1.1 Forwarding logic (must be identical across controllers)

OpenFlow 1.3, reactive MAC-learning L2 switch (Ryu `simple_switch_13` logic):

- **On switch connect** (`EventOFPSwitchFeatures`): install a table-miss
  entry — priority 0, empty match, action `OUTPUT:CONTROLLER` with
  `OFPCML_NO_BUFFER`. This FlowMod is **not** counted in `flow_mod_count`.
- **On every Packet-In** (`EventOFPPacketIn`):
  1. Parse the Ethernet header. If there is none, return immediately
     (counted in `packet_in_count`, but no `flow_events.csv` row is written).
  2. Learn `mac_to_port[dpid][eth_src] = in_port`.
  3. If `eth_dst` is known on this switch → `out_port` = learned port;
     otherwise `out_port = OFPP_FLOOD`.
  4. If `out_port` is not FLOOD → install a flow entry:
     - priority **1**
     - match **`in_port`, `eth_src`, `eth_dst`** (exactly these three fields)
     - instruction `APPLY_ACTIONS: OUTPUT:out_port`
     - **no `idle_timeout` and no `hard_timeout`** (both 0 = permanent)
     - if the switch buffered the packet (`buffer_id != OFP_NO_BUFFER`), the
       FlowMod carries the `buffer_id`, which releases the packet; no
       PacketOut is sent and handling ends here.
  5. Otherwise send a PacketOut with `actions=[OUTPUT:out_port]` (the packet
     data is included when unbuffered). Flooding uses this path with no FlowMod.

Consequences that the paper and README rely on: with permanent entries and
static ARP (`autoStaticArp=True` in Mininet), a Packet-In happens only on the
first packet per directed MAC pair per switch; controller load is bounded by
unique pairs, not by flows per second. (`FLOW_IDLE_TIMEOUT` is planned but not
implemented; today timeouts are always 0.)

### 1.2 Timing point (`proc_time_ms`)

- **Start:** `time.perf_counter()` as the first statement of the Packet-In
  handler (handler entry). Time spent waiting in the framework's event queue
  is **excluded**.
- **End:** `time.perf_counter()` immediately after the last `send_msg` of the
  handler returns:
  - buffered + known destination: after the FlowMod;
  - unbuffered + known destination: after the FlowMod **and** the PacketOut;
  - unknown destination (flood): after the flooding PacketOut.
- `send_msg` serializes the message and puts it on the datapath's send queue
  (Ryu `Datapath.send` → `send_q.put`); the socket write happens later in the
  datapath's send loop and is **not** included.
- The CSV write and flush happen *after* the end timestamp, so logging is
  outside the measured interval (though it does run inside the handler).
- Flood events are included in the samples.

### 1.3 Output directory

- Read the output directory from the environment variable **`OUTDIR`**
  (absolute path set by the run script). Fallback when unset:
  `results/raw/devrun`, relative to the working directory.
- Create it if missing (`os.makedirs(..., exist_ok=True)`).
- Open both CSVs in **`"w"` mode** (truncate) at app start, write the header,
  and **flush after every row**. Files are never explicitly closed: the run
  script stops the controller with SIGTERM, so per-row flushing is what makes
  the data complete.

### 1.4 `flow_events.csv` — one row per handled Packet-In

Header (exact order):

```
ts_wall,dpid,src_mac,dst_mac,in_port,proc_time_ms,msg_len
```

| Column | Type | Meaning |
|---|---|---|
| `ts_wall` | float, Unix epoch seconds | `time.time()` taken when the row is written, i.e. *after* handling (not handler entry) |
| `dpid` | int | datapath ID of the switch that sent the Packet-In |
| `src_mac` | string `aa:bb:cc:dd:ee:ff` | Ethernet source |
| `dst_mac` | string `aa:bb:cc:dd:ee:ff` | Ethernet destination (may be broadcast) |
| `in_port` | int | `msg.match['in_port']`, or `-1` if absent |
| `proc_time_ms` | float, milliseconds | handler-entry → last `send_msg` returned (§1.2) |
| `msg_len` | int, bytes | `len(msg.data)` — length of the packet data in the Packet-In |

Used by the aggregator for `proc_latency_p50_ms` and `proc_latency_p95_ms`.

### 1.5 `controller_metrics.csv` — one row per ~1 s

Header (exact order):

```
ts_wall,packet_in_count,flow_mod_count
```

| Column | Type | Meaning |
|---|---|---|
| `ts_wall` | float, Unix epoch seconds | time the row is written |
| `packet_in_count` | int | Packet-Ins received since the previous row (all, including non-Ethernet) |
| `flow_mod_count` | int | priority-1 FlowMods sent since the previous row (table-miss excluded) |

The first row is written at app start and is always `0,0`. Both counters are
reset to 0 right after each row is written. The aggregator reports the mean of
each column over all rows as `avg_packet_in_rate` and `avg_flow_mod_rate`.

### 1.6 Per-second logger (must not block the event loop)

- Runs as a framework green thread: `hub.spawn(self._per_second_logger)` from
  `__init__`.
- Loop: write row → flush → reset counters → `hub.sleep(1.0)`.
- It **must** sleep with the framework's cooperative sleep (`hub.sleep`), never
  `time.sleep`, which would block the eventlet hub and stall Packet-In handling.
- Green threads are cooperative, so reading and resetting the counters cannot
  interleave with a running handler; no lock is needed.
- The period is 1 s *plus* the write time, so rows drift slightly from exact
  1 Hz; `ts_wall` records the actual times.

---

## Part 2 — Adapter responsibilities

`controller/adapters/base.py` defines `ControllerAdapter`; the orchestrator
(Mininet, traffic, monitor, aggregation, phase timing) is controller-independent.

| Member | Contract | Default in `ControllerAdapter` |
|---|---|---|
| `name` | registry key; also names `<name>_stdout.log`, `<name>_stderr.log`, `<name>_pid.txt` | — |
| `process_patterns` | `pkill -f` patterns for stale processes; the orchestrator kills those of **all** registered controllers before each run (they share port 6653) | `()` |
| `command(outdir, port)` | argv that runs the controller **in the foreground, without a wrapper** | must implement |
| `env(outdir, port)` | environment of the controller; must contain `OUTDIR` (§1.3) | copy of `os.environ` + `OUTDIR` |
| `start(outdir, port)` | launch in the background, cwd = `outdir`, logs into `outdir`; returns the PID | `Popen(command)` |
| `pid` | PID sampled by the monitor | `Popen.pid` |
| `wait_ready(timeout)` | return seconds until switches can connect; raise `ControllerNotReady` on timeout or early exit | poll until TCP `127.0.0.1:port` accepts a connection |
| `stop()` | stop the controller | SIGTERM, SIGKILL after 5 s |
| `version()` | dict merged into `controller_meta.json` | `{}` |

### 2.1 Run sequence (`run_experiment.py`)

1. **pre_cleanup**: `sudo mn -c`; `sudo pkill -f` every pattern of every adapter.
2. **controller_start**: `adapter.start(outdir, 6653, cpus)`;
   `adapter.wait_ready(READY_TIMEOUT)` (default 120 s; recorded as
   `ready_time_s`; the former shell script used a fixed `sleep 1`); then
   `log_system_metrics.py --pid <adapter.pid>`. The monitor starts after
   readiness so that `pid` can resolve to the working process (ONOS: the JVM,
   not its launcher shell); controller start-up is not sampled.
3. **warmup** (if `WARMUP_ROUNDS` > 0, §2.7), **capture_start** (§2.5),
   **mininet_run**: `sudo [taskset] /usr/bin/python3 scripts/run_mininet_iperf.py ...`.
4. **teardown** (always, also after errors): stop tcpdump, stop the monitor, `adapter.stop()`,
   `sleep 1`, `sudo mn -c`, `chown` the run directory, write `controller_meta.json`.
5. **wire_analysis** → **aggregation** → **cleanup** (only if the controller became ready), then
   `phase_times.py`. Exit code 1 if the controller never became ready; the
   matrix scripts log `[FAIL] <run_id>` and continue.

### 2.2 PID for the monitor

`pid` must be the **controller process itself**, not `sudo`, `sh -c` or a
launcher that forks: `log_system_metrics.py` samples exactly that PID with
psutil (children are not included). It writes `system_metrics.csv`:
`ts_wall,cpu_percent,rss_bytes,vms_bytes,num_threads` (CPU % of one core, so
it can exceed 100 for multi-threaded controllers). It sleeps one interval
before each sample, so every sample covers a full interval.

### 2.3 Port

The OpenFlow port (6653) is passed to `start()` and to Mininet
(`--controller_port`). The Python adapters pass it explicitly
(`--ofp-tcp-listen-port`).

### 2.4 Output paths

| Item | Path |
|---|---|
| run directory (`OUTDIR`) | `results_v4/raw/<controller>/<RUN_ID>/` (root: `--results_root` / `RESULTS_ROOT`) |
| summary | `results_v4/summary/summary.csv` (all controllers; column `controller`) |
| controller logs | `<name>_stdout.log`, `<name>_stderr.log` |
| PID files | `<name>_pid.txt`, `syslog_pid.txt` |
| controller metadata | `controller_meta.json`: `controller`, `port`, `ready_time_s`, plus `version()` |
| wire capture | `openflow.pcap` (deleted unless `KEEP_PCAP=1`), `tcpdump_stderr.log`, `tcpdump_pid.txt`, `wire_events.csv`, `wire_summary.json` |

Kept after a successful aggregation: `flow_events.csv`,
`controller_metrics.csv`, `system_metrics.csv`, `traffic_meta.json`,
`iperf_summary.json`, `controller_meta.json`, `phase_times.json`, PID files.
Logs and `iperf_json/` are deleted on success (`KEEP_IPERF_JSON=1` keeps the
JSONs, `KEEP_LOGS=1` the logs) and kept if aggregation fails.

### 2.5 Wire capture (controller-independent latency)

Between `controller_start` and `mininet_run` the orchestrator starts
`sudo tcpdump -i lo -s 0 -B 32768 --time-stamp-precision=nano -Z <user>
-w openflow.pcap tcp port 6653` and waits for "listening on"; it stops it with
SIGINT first in teardown. `analysis/openflow_latency.py` then writes
`wire_events.csv` and `wire_summary.json` (see the README, "Metric
definitions"). Requirements this puts on a controller:

- OpenFlow over **plain TCP on 127.0.0.1:6653** (no TLS);
- every handled Packet-In is answered on the **same switch connection** by a
  PacketOut carrying the **unchanged packet data**, or by a FlowMod with the
  Packet-In's `buffer_id` (§1.1 step 4–5);
- the FlowMod match carries `in_port`, `eth_src`, `eth_dst` as exact OXM fields;
  it may be sent before the PacketOut (Ryu, OS-Ken) or after it (ONOS), and
  `wire_last_ms` is the later of the two;
- OpenFlow 1.3: Mininet's switches are configured with `protocols=OpenFlow13`
  (`run_mininet_iperf.py`, `warmup.py`); `non_of13_messages` in
  `wire_summary.json` must be 0.

Validity checks per run (in `wire_summary.json`): `tcpdump_dropped_by_kernel`
= 0, `unmatched_packet_in` = 0, `unicast_without_flow_mod` = 0, and
`packet_ins` / `with_flow_mod` equal to the sums of `packet_in_count` /
`flow_mod_count` in `controller_metrics.csv` (rows after `measure_start_ts`).
`unmatched_flow_mod` counts FlowMods no Packet-In accounts for: 0 for Ryu and
OS-Ken; ONOS re-sends some rules on its own (about 7% extra in a tree N=64
smoke run), which are not part of any latency.

### 2.6 Interpreters (Python controllers)

The Python adapters run the framework's `manager.main()` with an explicit
interpreter (`python -c "from <pkg>.cmd.manager import main; ..."`), which is
recorded in `controller_meta.json` and the summary (`controller_runtime`):

- Ryu: `venv_py310/bin/python` (fallback `venv/`), Python 3.10, ryu 4.34.
  Override with `RYU_PYTHON`.
- OS-Ken: `venv_osken_py310/bin/python` (from `requirements-osken.txt`):
  os-ken 2.8.1 on Python 3.10 with Ryu's eventlet 0.33.3 / greenlet 3.3.1 /
  dnspython 2.1.0, so Ryu and OS-Ken differ only in the framework. Fallback
  when that venv is missing: `/usr/bin/python3` (Python 3.12, apt
  `python3-os-ken` 2.8.1, eventlet 0.35.2), which the earlier v2 OS-Ken data
  used; `results_v4` uses `venv_osken_py310`. (`venv_py310`'s os-ken 4.2.2 has no `os_ken.cmd`
  and cannot run.) Override with `OSKEN_PYTHON`.

### 2.7 CPU pinning and warm-up (same rules for every controller)

- `CONTROLLER_CPUS` (e.g. `3`): `start(..., cpus=)` sets the affinity in the
  child before exec (`sched_setaffinity`), so the PID is unchanged and every
  thread inherits it. A JVM sizes its thread pools from the CPUs it may use,
  so this also fixes how many cores a Java controller believes it has.
- `WORKLOAD_CPUS` (e.g. `0-2`): `taskset` for Mininet (and so every iperf3
  process in the hosts), the warm-up and tcpdump. `ovs-vswitchd` and the
  kernel datapath are system-wide and not pinned.
- `WARMUP_ROUNDS` (default 0), `WARMUP_HOSTS` (default 16): before the
  capture and the measured network, `scripts/warmup.py` runs that many
  one-switch networks with an all-pairs ping (about 550 Packet-Ins and 2.5 s
  per round at 16 hosts). They use datapath IDs `0xffff0000000000xx` and MACs
  `02:77:00:00:xx:yy`, so nothing they leave in the controller's MAC table can
  match the measured network. `measure_start_ts` (in `controller_meta.json`)
  is set 1.5 s after the warm-up; the aggregator drops `flow_events.csv`,
  `controller_metrics.csv` and `system_metrics.csv` rows before it.
- All three are recorded in `controller_meta.json` and the summary
  (`controller_cpus`, `workload_cpus`, `warmup_rounds`).

---

## Part 3 — Worked example: porting Ryu → OS-Ken

OS-Ken is a fork of Ryu, so the port is purely mechanical. These are the
**complete** differences today.

### 3.1 Controller app (`ryu_instrumented_switch.py` → `osken_instrumented_switch.py`)

Imports and base class only; logic, timing points, CSV schemas and `OUTDIR`
handling are byte-identical.

```diff
-from ryu.base import app_manager
-from ryu.controller import ofp_event
-from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
-from ryu.controller.handler import set_ev_cls
-from ryu.ofproto import ofproto_v1_3
-from ryu.lib.packet import packet, ethernet
-from ryu.lib import hub
+from os_ken.base import app_manager
+from os_ken.controller import ofp_event
+from os_ken.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
+from os_ken.controller.handler import set_ev_cls
+from os_ken.ofproto import ofproto_v1_3
+from os_ken.lib.packet import packet, ethernet
+from os_ken.lib import hub

-class InstrumentedSimpleSwitch13(app_manager.RyuApp):
+class InstrumentedSimpleSwitch13(app_manager.OSKenApp):
```

### 3.2 Adapter

```python
class OSKenAdapter(PythonManagerAdapter):
    name = "osken"
    process_patterns = ("osken-manager", "os_ken.cmd.manager")
    manager_module = "os_ken.cmd.manager"
    dist_name = "os-ken"
    app_file = "controller/osken_instrumented_switch.py"
    python_env = "OSKEN_PYTHON"

    def default_python(self):
        return "/usr/bin/python3"
```

registered in `controller/adapters/__init__.py` (`ADAPTERS`).

### 3.3 Sweeps

`scripts/sweep.py` is shared; the controller is chosen with
`--controllers osken` (default: all of `CONTROLLERS`). Run IDs carry no controller
prefix (the controller is in the directory and the `controller` column).
Seeds follow the same formula (`B_T + 10N + FPS + rep`), so all controllers
see identical src/dst flow sequences.

### 3.4 Checklist for adding another controller

1. Write the app so §1.1–§1.6 hold exactly (same forwarding logic, timing
   points, CSV schemas, `OUTDIR`, periodic counter logger). Disable anything
   in the framework that sends or triggers extra OpenFlow messages (e.g. LLDP
   link discovery, host tracking, stock forwarding apps).
2. Subclass `ControllerAdapter`: `name`, `process_patterns`, `command()`,
   `version()`; override `wait_ready()` if listening on 6653 is not enough
   (e.g. a Java controller whose app activates later; poll its REST API).
3. Register it in `ADAPTERS`.
4. Smoke test: `CONTROLLER=<name> scripts/run_experiment.sh 4 SMOKE_STAR_N4_F200 200 20 2 1241 star`;
   check that `flow_events.csv`, `controller_metrics.csv` and
   `system_metrics.csv` are non-empty and that the set of unique unicast
   `(dpid, src_mac, dst_mac)` in `flow_events.csv` equals that of a Ryu run
   with the same seed. (Raw Packet-In/FlowMod counts differ between runs:
   packets arriving before the FlowMod is installed cause repeat Packet-Ins,
   and hosts emit IPv6 ND/MLD multicast on their own schedule.)

---

## Part 4 — ONOS 2.7.0

### 4.1 Install

```bash
sudo apt install openjdk-11-jdk-headless maven
scripts/setup_onos.sh     # download + sha1 check, unpack, build app, install app
```

ONOS is unpacked to `ONOS_HOME` (default `~/.local/opt/onos-2.7.0`): its
launch scripts do not quote paths, so it cannot live inside a directory with
spaces. The tarball is kept in `third_party/`. Rerun `setup_onos.sh` after any
change to the app; the adapter refuses to start if the installed `.oar`
differs from the built one (sha256, recorded as `app_sha256`).

### 4.2 App (`controller/onos-instrumented-l2/`)

`InstrumentedL2.java` implements §1.1 with ONOS's northbound API, i.e. as an
ONOS app would:

| §1.1 step | ONOS |
|---|---|
| table-miss on connect | `DeviceListener` (`DEVICE_ADDED`) → `FlowRuleService.applyFlowRules`, priority 0, empty selector, `punt()` (not counted) |
| Packet-In | `PacketProcessor` registered as `director(1)`; `packet_in_count` counts every `process()` call |
| learn / lookup | `ConcurrentHashMap<DeviceId, Map<MacAddress, PortNumber>>` |
| FlowMod | `DefaultFlowRule`, priority 1, selector `in_port`/`eth_src`/`eth_dst`, `setOutput`, `makePermanent()`; `applyFlowRules` |
| PacketOut | `context.treatmentBuilder().setOutput(port or FLOOD)`, `context.send()`, `context.block()` |

`proc_time_ms`: `System.nanoTime()` at `process()` entry → after
`context.send()` returned. Both `applyFlowRules` and `send` hand the message
to ONOS's own queues/threads, so this is even less of the controller's work
than in Ryu: compare controllers with `wire_*`, not `proc_*`.

Differences inherent to ONOS (documented, not removed):
- `process()` runs on several OpenFlow I/O threads at once; CSV rows are
  written under a lock (after the end timestamp, as in Ryu).
- Flow rules go through ONOS's flow subsystem: each FlowMod is followed by a
  BarrierRequest, the FlowMod may leave after the PacketOut, and some rules
  are re-sent.
- ONOS polls flow and port statistics every 5 s (multipart requests).
- `msg_len` is the length of the packet ONOS received (`unparsed()`).

### 4.3 Adapter (`controller/adapters/onos.py`)

- **start**: checks the installed app, deletes Karaf's `data/` (no state
  carries over; `ONOS_FRESH_DATA=0` keeps it), runs `bin/onos-service server`
  with `ONOS_APPS=openflow-base,org.sdntestbed.instrumentedl2` (plus the
  system app `drivers`; no `fwd`, LLDP provider or host provider) and
  `JAVA_OPTS` = ONOS's defaults + `-Xms/-Xmx` = `ONOS_HEAP` (default `2g`) +
  `-Djdk.util.zip.disableZip64ExtraFieldValidation=true` (needed on JDK 11 ≥
  11.0.20). Karaf ignores `JAVA_MIN_MEM`/`JAVA_MAX_MEM` once `JAVA_OPTS` is set.
- **wait_ready**: REST `GET /onos/v1/applications/org.sdntestbed.instrumentedl2`
  reports `ACTIVE` **and** 6653 accepts connections (about 27 s on one pinned core).
- **pid**: the `java` child of Karaf's launcher shell.
- **stop**: SIGTERM to the JVM (Karaf shuts down; the app's `deactivate()`
  writes the last `controller_metrics.csv` row), about 6 s.
- **version()**: ONOS version, Java version (`runtime`), heap, `app_sha256`.
- Environment: `ONOS_HOME`, `ONOS_JAVA_HOME` (default
  `/usr/lib/jvm/java-11-openjdk-amd64`), `ONOS_HEAP`, `ONOS_FRESH_DATA`.
- Diagnostics (off by default, recorded in `controller_meta.json`; not for
  result runs): `ONOS_GC_LOG=1` writes the JVM GC + safepoint log to
  `onos_gc.log`; `ONOS_DEBUG_LOGGERS=1` (or a comma-separated list) sets DEBUG
  for the Packet-In path (`org.onosproject.openflow.controller.impl`,
  `…provider.of.packet.impl`, `…net.packet.impl`) via a marked block in
  Karaf's `etc/org.ops4j.pax.logging.cfg`, removed again in `stop()` and at
  every start; Karaf's `karaf.log*` is copied to `onos_karaf.log*`.

A run takes about 70–80 s (vs. about 30–45 s for Ryu), mostly start-up.
