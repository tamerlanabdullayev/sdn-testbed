#!/usr/bin/env python3
"""
openflow_latency.py
Usage: python3 openflow_latency.py --rundir <path/to/run> [--pcap openflow.pcap] [--port 6653]

Controller-independent response latency from a capture of the OpenFlow
control channel (tcpdump on lo, written by run_experiment.py).

For every Packet-In (switch -> controller) the reply on the same switch
connection is found:
  - the PacketOut whose packet data equals the Packet-In data (every handled
    Packet-In gets one, since the table-miss entry uses OFPCML_NO_BUFFER),
  - for a non-flooding PacketOut, the priority>0 FlowMod whose match has the
    same in_port/eth_src/eth_dst. It may come before the PacketOut (Ryu,
    OS-Ken) or after it (ONOS installs flow rules asynchronously).
Duplicates are paired FIFO. A message's time is the capture timestamp of the
TCP segment that completes it.

  wire_first_ms   Packet-In -> first reply (FlowMod or PacketOut)
  wire_last_ms    Packet-In -> last reply (end of the response)

Unlike proc_time_ms (handler entry -> send_msg returned), these include the
framework's socket read, parsing, event-queue wait and socket write.

Writes <rundir>/wire_events.csv and <rundir>/wire_summary.json.
Standard library only (no tshark / scapy).
"""

import argparse
import csv
import json
import struct
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

OFPT_FEATURES_REPLY = 6
OFPT_PACKET_IN = 10
OFPT_PACKET_OUT = 13
OFPT_FLOW_MOD = 14
OFP_NO_BUFFER = 0xFFFFFFFF
OFPP_FLOOD = 0xFFFFFFFB
OF_TYPE_NAMES = {0: "hello", 1: "error", 2: "echo_request", 3: "echo_reply",
                 5: "features_request", 6: "features_reply", 7: "get_config_request",
                 8: "get_config_reply", 9: "set_config", 10: "packet_in",
                 12: "port_status", 13: "packet_out", 14: "flow_mod",
                 18: "multipart_request", 19: "multipart_reply",
                 20: "barrier_request", 21: "barrier_reply", 24: "role_request",
                 25: "role_reply", 26: "get_async_request", 27: "get_async_reply"}

WIRE_EVENT_COLUMNS = ["ts_packet_in", "dpid", "in_port", "src_mac", "dst_mac",
                      "kind", "flow_mod", "wire_first_ms", "wire_last_ms"]


# ---------------------------------------------------------------------------
# pcap -> TCP payload segments
# ---------------------------------------------------------------------------

def read_pcap(path):
    """Yields (ts, linktype, frame_bytes)."""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D):
            endian = "<"
        else:
            endian = ">"
            magic = struct.unpack(">I", gh[:4])[0]
        if magic not in (0xA1B2C3D4, 0xA1B23C4D):
            raise ValueError(f"{path}: not a pcap file (pcapng is not supported)")
        frac = 1e-9 if magic == 0xA1B23C4D else 1e-6
        linktype = struct.unpack(endian + "I", gh[20:24])[0] & 0x0FFFFFFF
        rec = struct.Struct(endian + "IIII")
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            sec, sub, incl, _orig = rec.unpack(h)
            data = f.read(incl)
            if len(data) < incl:
                return
            yield sec + sub * frac, linktype, data


def ip_offset(linktype, frame):
    if linktype == 1:                       # Ethernet (Linux lo)
        off, ethertype = 14, struct.unpack("!H", frame[12:14])[0]
        while ethertype == 0x8100:          # VLAN
            ethertype = struct.unpack("!H", frame[off + 2:off + 4])[0]
            off += 4
        return off if ethertype == 0x0800 else None
    if linktype == 113:                     # Linux cooked (-i any)
        return 16 if struct.unpack("!H", frame[14:16])[0] == 0x0800 else None
    if linktype == 276:                     # Linux cooked v2
        return 20 if struct.unpack("!H", frame[0:2])[0] == 0x0800 else None
    if linktype in (0, 108):                # loopback (BSD)
        return 4
    if linktype in (12, 101, 228):          # raw IPv4
        return 0
    return None


def tcp_segments(path, stats):
    """Yields (ts, flow, seq, flags, payload); flow = (src, sport, dst, dport)."""
    for ts, lt, frame in read_pcap(path):
        stats["frames"] += 1
        off = ip_offset(lt, frame)
        if off is None or len(frame) < off + 20:
            continue
        vihl = frame[off]
        if vihl >> 4 != 4 or frame[off + 9] != 6:
            continue
        ihl = (vihl & 0x0F) * 4
        total_len = struct.unpack("!H", frame[off + 2:off + 4])[0]
        src, dst = frame[off + 12:off + 16], frame[off + 16:off + 20]
        t = off + ihl
        if len(frame) < t + 20:
            continue
        sport, dport, seq = struct.unpack("!HHI", frame[t:t + 8])
        doff = (frame[t + 12] >> 4) * 4
        flags = frame[t + 13]
        end = min(off + total_len, len(frame)) if total_len else len(frame)
        yield ts, (src, sport, dst, dport), seq, flags, frame[t + doff:end]


class Stream:
    """In-order reassembly of one TCP direction, split into OpenFlow messages."""

    def __init__(self):
        self.next_seq = None
        self.buf = bytearray()
        self.ooo = {}

    def feed(self, seq, flags, payload, stats):
        if flags & 0x02:                    # SYN
            self.next_seq = (seq + 1) & 0xFFFFFFFF
            return
        if not payload:
            return
        if self.next_seq is None:           # capture started mid-stream
            self.next_seq = seq
        delta = (seq - self.next_seq + 2**31) % 2**32 - 2**31
        if delta < 0:                       # retransmission / overlap
            if -delta >= len(payload):
                stats["tcp_retransmitted"] += 1
                return
            payload, seq = payload[-delta:], self.next_seq
        elif delta > 0:
            self.ooo[seq] = payload
            stats["tcp_out_of_order"] += 1
            return
        self.buf += payload
        self.next_seq = (self.next_seq + len(payload)) & 0xFFFFFFFF
        while self.next_seq in self.ooo:
            p = self.ooo.pop(self.next_seq)
            self.buf += p
            self.next_seq = (self.next_seq + len(p)) & 0xFFFFFFFF

    def messages(self):
        while len(self.buf) >= 8:
            length = struct.unpack("!H", self.buf[2:4])[0]
            if length < 8:                  # corrupt; drop the stream buffer
                self.buf.clear()
                return
            if len(self.buf) < length:
                return
            msg = bytes(self.buf[:length])
            del self.buf[:length]
            yield msg


# ---------------------------------------------------------------------------
# OpenFlow 1.3 decoding (only what the matcher needs)
# ---------------------------------------------------------------------------

def parse_oxm_match(buf, off):
    """ofp_match at buf[off:]; returns (fields, offset after padded match)."""
    mtype, mlen = struct.unpack("!HH", buf[off:off + 4])
    fields = {}
    p, end = off + 4, off + mlen
    while p + 4 <= end:
        cls, fh, flen = struct.unpack("!HBB", buf[p:p + 4])
        field, hasmask = fh >> 1, fh & 1
        val = buf[p + 4:p + 4 + flen]
        if cls == 0x8000 and not hasmask:
            if field == 0:
                fields["in_port"] = struct.unpack("!I", val)[0]
            elif field == 3:
                fields["eth_dst"] = val
            elif field == 4:
                fields["eth_src"] = val
        p += 4 + flen
    return fields, off + (mlen + 7) // 8 * 8


def mac(b):
    return ":".join(f"{x:02x}" for x in b)


def decode(msg):
    """Returns (type, info dict or None)."""
    mtype = msg[1]
    try:
        if mtype == OFPT_FEATURES_REPLY:
            return mtype, {"dpid": struct.unpack("!Q", msg[8:16])[0]}
        if mtype == OFPT_PACKET_IN:
            buffer_id = struct.unpack("!I", msg[8:12])[0]
            fields, p = parse_oxm_match(msg, 24)
            data = msg[p + 2:]
            return mtype, {"buffer_id": buffer_id, "in_port": fields.get("in_port", -1),
                           "data": data}
        if mtype == OFPT_PACKET_OUT:
            buffer_id, in_port, alen = struct.unpack("!IIH", msg[8:18])
            ports, p = [], 24
            while p + 8 <= 24 + alen:
                atype, al = struct.unpack("!HH", msg[p:p + 4])
                if atype == 0:
                    ports.append(struct.unpack("!I", msg[p + 4:p + 8])[0])
                p += max(al, 8)
            return mtype, {"buffer_id": buffer_id, "in_port": in_port,
                           "ports": ports, "data": msg[24 + alen:]}
        if mtype == OFPT_FLOW_MOD:
            # header(8) cookie(8) cookie_mask(8) table_id command idle hard
            # priority(@30) buffer_id(@32) out_port out_group flags pad match(@48)
            priority, buffer_id = struct.unpack("!HI", msg[30:36])
            fields, _ = parse_oxm_match(msg, 48)
            return mtype, {"priority": priority, "buffer_id": buffer_id, **fields}
    except (struct.error, IndexError):
        return mtype, None
    return mtype, None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def analyze(pcap, port):
    stats = Counter()
    of_types = Counter()
    streams = defaultdict(Stream)
    dpid_of = {}                 # switch-side (ip, port) -> dpid
    events = []                  # one dict per Packet-In
    by_data = defaultdict(deque)     # (conn, data) -> pending event indices
    by_key = defaultdict(deque)      # (conn, in_port, src, dst) -> awaiting FlowMod

    for ts, flow, seq, flags, payload in tcp_segments(pcap, stats):
        src, sport, dst, dport = flow
        if dport == port:
            to_ctrl, conn = True, (src, sport)
        elif sport == port:
            to_ctrl, conn = False, (dst, dport)
        else:
            continue
        st = streams[flow]
        st.feed(seq, flags, payload, stats)
        for msg in st.messages():
            if msg[0] != 0x04:              # decoder is OpenFlow 1.3 only
                stats["non_of13_messages"] += 1
                if msg[1] != 0:             # HELLO may carry another version
                    continue
            mtype, info = decode(msg)
            of_types[("sw->ctl " if to_ctrl else "ctl->sw ") + OF_TYPE_NAMES.get(mtype, str(mtype))] += 1
            if info is None:
                continue
            if to_ctrl and mtype == OFPT_FEATURES_REPLY:
                dpid_of[conn] = info["dpid"]
            elif to_ctrl and mtype == OFPT_PACKET_IN:
                data = info["data"]
                ev = {"ts": ts, "conn": conn, "in_port": info["in_port"],
                      "dst": data[0:6], "src": data[6:12], "t_flow_mod": None,
                      "t_packet_out": None, "flood": False, "no_eth": False}
                idx = len(events)
                events.append(ev)
                if len(data) < 14:
                    ev["no_eth"] = True
                    stats["packet_in_no_ethernet"] += 1
                    continue
                ev["data_key"] = (conn, data)
                ev["key"] = (conn, ev["in_port"], ev["src"], ev["dst"])
                by_data[ev["data_key"]].append(idx)
                by_key[ev["key"]].append(idx)
            elif not to_ctrl and mtype == OFPT_FLOW_MOD:
                if info["priority"] == 0:
                    continue
                q = by_key.get((conn, info.get("in_port"), info.get("eth_src"), info.get("eth_dst")))
                if q:
                    idx = q.popleft()
                    ev = events[idx]
                    ev["t_flow_mod"] = ts
                    if info["buffer_id"] != OFP_NO_BUFFER:   # releases the packet: done
                        ev["t_packet_out"] = ts
                        by_data[ev["data_key"]].remove(idx)
                else:
                    stats["unmatched_flow_mod"] += 1
            elif not to_ctrl and mtype == OFPT_PACKET_OUT:
                q = by_data.get((conn, info["data"]))
                if q:
                    idx = q.popleft()
                    ev = events[idx]
                    ev["t_packet_out"] = ts
                    ev["flood"] = OFPP_FLOOD in info["ports"]
                    pending = by_key.get(ev["key"])
                    if ev["flood"] and pending and idx in pending:   # no FlowMod coming
                        pending.remove(idx)
                else:
                    stats["unmatched_packet_out"] += 1

    for ev in events:
        ev["dpid"] = dpid_of.get(ev["conn"], -1)
    stats["streams_with_leftover_bytes"] = sum(1 for s in streams.values() if s.buf)
    return events, stats, of_types


def pct(values, q):
    """Same convention as aggregate_results.read_flow_events."""
    v = sorted(values)
    return v[min(int(len(v) * q), len(v) - 1)] if v else None


def parse_tcpdump_stats(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            key = " ".join(parts[1:]).replace("packets ", "").replace(" ", "_")
            out["tcpdump_" + key] = int(parts[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rundir", required=True)
    ap.add_argument("--pcap", default="openflow.pcap")
    ap.add_argument("--port", type=int, default=6653)
    args = ap.parse_args()
    rundir = Path(args.rundir)
    pcap = rundir / args.pcap
    if not pcap.exists():
        print(f"[WIRE] no {pcap}", file=sys.stderr)
        return 1

    events, stats, of_types = analyze(pcap, args.port)

    rows, first, last = [], [], []
    for ev in events:
        replies = [t for t in (ev["t_flow_mod"], ev["t_packet_out"]) if t is not None]
        t_first = min(replies) if replies else None
        # No PacketOut = not handled (unmatched), even if a FlowMod was seen.
        t_end = max(replies) if ev["t_packet_out"] is not None else None
        if t_end is not None and not ev["flood"] and ev["t_flow_mod"] is None:
            stats["unicast_without_flow_mod"] += 1
        kind = ("no_ethernet" if ev["no_eth"] else
                "unmatched" if t_end is None else
                "flood" if ev["flood"] else "unicast")
        f_ms = (t_first - ev["ts"]) * 1000 if t_first is not None else None
        l_ms = (t_end - ev["ts"]) * 1000 if t_end is not None else None
        if l_ms is not None:
            first.append(f_ms)
            last.append(l_ms)
        rows.append([f"{ev['ts']:.9f}", ev["dpid"], ev["in_port"], mac(ev["src"]),
                     mac(ev["dst"]), kind, int(ev["t_flow_mod"] is not None),
                     "" if f_ms is None else f"{f_ms:.6f}",
                     "" if l_ms is None else f"{l_ms:.6f}"])

    with open(rundir / "wire_events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(WIRE_EVENT_COLUMNS)
        w.writerows(rows)

    kinds = Counter(r[5] for r in rows)
    summary = {
        "packet_ins": len(events),
        "matched": len(last),
        "unmatched_packet_in": kinds["unmatched"],
        "unicast": kinds["unicast"],
        "flood": kinds["flood"],
        "with_flow_mod": sum(1 for r in rows if r[6]),
        "wire_last_p50_ms": pct(last, 0.50),
        "wire_last_p95_ms": pct(last, 0.95),
        "wire_first_p50_ms": pct(first, 0.50),
        "wire_first_p95_ms": pct(first, 0.95),
        **{k: v for k, v in stats.items()},
        **parse_tcpdump_stats(rundir / "tcpdump_stderr.log"),
        "of_messages": dict(sorted(of_types.items())),
    }
    with open(rundir / "wire_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    p50, p95 = summary["wire_last_p50_ms"], summary["wire_last_p95_ms"]
    print(f"[WIRE] {len(events)} Packet-Ins, {len(last)} matched, "
          f"{kinds['unmatched']} unmatched; wire_last p50="
          f"{p50 if p50 is None else round(p50, 4)} ms p95="
          f"{p95 if p95 is None else round(p95, 4)} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
