# Measurement environment

Software and machine used for `results_v4` (1200 runs: Ryu, OS-Ken and ONOS,
star and two-tier tree; sweep 2026-10-29/30). Machine, OS and tool versions are
from the sweep record `results_v4/sweeps/sweep_20261029_141522.json`
(`environment`); controller versions are also recorded per run in
`controller_meta.json` and the summary (`controller_version`,
`controller_runtime`).

## Host

| Component | Version |
|---|---|
| Machine | VirtualBox VM, 4 vCPUs (host CPU: 13th Gen Intel Core i7-13700F), 16 GB RAM |
| OS | Ubuntu 24.04.2 LTS |
| Kernel | 7.0.0-38-generic |
| Open vSwitch | 3.3.4 |
| Mininet | 2.3.0, run with system `/usr/bin/python3` (3.12.3) under sudo |
| iperf3 | 3.16 (cJSON 1.7.15) |
| tcpdump | 4.99.4 |
| Pinning | controller on CPU 3; Mininet, iperf3, warm-up and tcpdump on CPUs 0–2; one warm-up round (16 hosts) |

## Controllers

| | Ryu | OS-Ken | ONOS |
|---|---|---|---|
| Framework | ryu 4.34 (source install, patched; see README) | os-ken 2.8.1 | ONOS 2.7.0 (release tarball, Karaf 4.2.9; `scripts/setup_onos.sh`) |
| Runtime | CPython 3.10.19 (`venv_py310`) | CPython 3.10.19 (`venv_osken_py310`) | OpenJDK 11.0.32.1, `-Xms2g -Xmx2g -XX:+UseG1GC -XX:MaxGCPauseMillis=200` |
| Python packages | `requirements.txt` | `requirements-osken.txt` | — |
| eventlet / greenlet / dnspython | 0.33.3 / 3.3.1 / 2.1.0 | 0.33.3 / 3.3.1 / 2.1.0 | — |
| App | `controller/ryu_instrumented_switch.py` | `controller/osken_instrumented_switch.py` | `controller/onos-instrumented-l2` 1.0.0, built with Maven 3.8.7 against `onos-dependencies` 2.7.0; OAR SHA-256 `b62f0685b0d468415ed503e1d67aef2f728d55d8fdfce7555579eb68149c0720` (`app_sha256`) |
| Active apps | — | — | `drivers`, `openflow-base`, `optical-model`, `org.sdntestbed.instrumentedl2` (no LLDP/BDDP link discovery, no host provider) |

Ryu and OS-Ken run on the same interpreter and the same eventlet, greenlet and
dnspython versions, so they differ only in the framework.

The orchestrator, monitor, aggregator and plotting scripts run in
`venv_py310` (pandas 2.3.3, numpy 2.2.6, matplotlib 3.10.8, psutil 7.2.2;
full list in `requirements.txt`).

## Code

All 1200 runs: `code_hash` =
`b5ac8d4b7a5dac7316dfaff9e8aa07ecc7e55286fc7065d4b1335e38ae75091c`
(see README, "Paper results").
