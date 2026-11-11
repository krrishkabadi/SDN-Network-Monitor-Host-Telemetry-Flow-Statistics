#!/usr/bin/env python3
"""Centralised monitoring server.

Thread 1 (receiver): collects UDP telemetry from agents, acks each datagram,
                     tracks rate / RTT / loss per host.
Main loop (analyser): every POLL_INTERVAL seconds, loads the SDN controller's
                     flow/port statistics, FUSES them with the application
                     telemetry, applies thresholds, raises/clears alerts and
                     renders a dashboard.
"""
import argparse
import csv
import json
import os
import socket
import sys
import threading
import time
from collections import deque

import config as C

os.makedirs(C.LOG_DIR, exist_ok=True)
ALERT_LOG = os.path.join(C.LOG_DIR, "alerts.log")
METRICS_CSV = os.path.join(C.LOG_DIR, "metrics.csv")
DASH_FILE = os.path.join(C.LOG_DIR, "dashboard.txt")

lock = threading.Lock()
hosts = {}                 # name -> state (filled by receiver thread)
active = {}                # (kind, subject) -> (severity, text)
events = deque(maxlen=8)   # recent alert/clear lines for the dashboard


# ---------------------------------------------------------------- receiver
def update_host(msg, now):
    name = msg["host"]
    h = hosts.setdefault(name, {"mac": msg.get("mac"), "received": 0, "last_seq": None,
                                "prev": None, "tx_mbps": 0.0, "rx_mbps": 0.0,
                                "rtt_ms": None, "win": deque(maxlen=10), "last_seen": now})
    seq = msg["seq"]
    gap = 0
    if h["last_seq"] is not None:
        if seq <= h["last_seq"]:          # agent restarted -> reset counters
            h["prev"] = None
        elif seq > h["last_seq"] + 1:     # missing sequence numbers = lost datagrams
            gap = seq - h["last_seq"] - 1
    h["last_seq"] = seq
    h["last_seen"] = now
    h["mac"] = msg.get("mac", h["mac"])
    h["received"] += 1
    h["win"].append((1, gap))
    if msg.get("rtt_ms") is not None:
        h["rtt_ms"] = msg["rtt_ms"]
    if h["prev"]:
        pts, ptx, prx = h["prev"]
        dt = msg["ts"] - pts
        if dt > 0:                         # rate = delta bytes * 8 / delta time
            h["tx_mbps"] = max(0, msg["tx_bytes"] - ptx) * 8 / dt / 1e6
            h["rx_mbps"] = max(0, msg["rx_bytes"] - prx) * 8 / dt / 1e6
    h["prev"] = (msg["ts"], msg["tx_bytes"], msg["rx_bytes"])


REQUIRED = ("host", "seq", "ts", "tx_bytes", "rx_bytes")


def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", C.SERVER_PORT))
    while True:
        data, addr = sock.recvfrom(4096)
        now = time.time()
        try:
            msg = json.loads(data.decode())
            if msg.get("type") != "telemetry" or any(k not in msg for k in REQUIRED):
                continue
            sock.sendto(json.dumps({"type": "ack", "seq": msg["seq"]}).encode(), addr)
            with lock:
                update_host(msg, now)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            # a malformed datagram must never kill the receiver thread
            print("bad datagram from %s: %s" % (addr, exc), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- analysis
def load_sdn():
    try:
        with open(C.STATS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def emit(level, sev, kind, subject, text):
    line = "%s [%s] %-4s %s/%s: %s" % (time.strftime("%H:%M:%S"), level, sev, kind, subject, text)
    events.append(line)
    with open(ALERT_LOG, "a") as f:
        f.write(line + "\n")


def update_alerts(findings):
    """Only speak when something CHANGES, otherwise the log fills with repeats."""
    for key, (sev, text) in findings.items():
        if active.get(key, (None,))[0] != sev:
            emit("ALERT", sev, key[0], key[1], text)
        active[key] = (sev, text)
    for key in list(active):
        if key not in findings:
            emit("CLEAR", "OK", key[0], key[1], "back to normal")
            del active[key]


def analyse(now):
    findings = {}

    def flag(kind, subject, sev, text):
        findings[(kind, subject)] = (sev, text)

    # ---- network side (from SDN controller) ----
    sdn = load_sdn()
    sdn_ok = sdn is not None and now - sdn.get("ts", 0) <= C.SDN_STALE_AFTER_S
    if not sdn_ok:
        flag("SDN", "controller", "WARN", "no fresh flow statistics from controller")
    ports, port_by_key = [], {}
    if sdn_ok:
        for key, p in sdn["ports"].items():
            util = max(p["rx_mbps"], p["tx_mbps"]) / C.LINK_CAPACITY_MBPS * 100
            row = {"key": key, "role": "trunk" if p["trunk"] else "host",
                   "rx": p["rx_mbps"], "tx": p["tx_mbps"], "util": util}
            ports.append(row)
            port_by_key[key] = row
            if util >= C.UTIL_CRIT_PCT:
                flag("UTIL", key, "CRIT", "%s link utilisation %.0f%%" % (row["role"], util))
            elif util >= C.UTIL_WARN_PCT:
                flag("UTIL", key, "WARN", "%s link utilisation %.0f%%" % (row["role"], util))
        ports.sort(key=lambda p: p["key"])

    # ---- application side (from UDP agents) ----
    hv = []
    with lock:
        for name, h in sorted(hosts.items()):
            recv = sum(r for r, _ in h["win"])
            lost = sum(l for _, l in h["win"])
            loss = 100.0 * lost / (recv + lost) if recv + lost else 0.0
            hv.append({"name": name, "mac": h["mac"], "tx": h["tx_mbps"], "rx": h["rx_mbps"],
                       "rtt": h["rtt_ms"], "loss": loss, "age": now - h["last_seen"]})
    for h in hv:
        n = h["name"]
        if h["age"] > C.SILENT_AFTER_S:
            flag("HOST", n, "CRIT", "no telemetry for %.0fs (host or agent down)" % h["age"])
            continue
        if h["tx"] > C.HOST_RATE_WARN_MBPS:
            flag("RATE", n, "WARN", "sending %.1f Mbps (limit %.1f)" % (h["tx"], C.HOST_RATE_WARN_MBPS))
        if h["rtt"] is not None and h["rtt"] > C.RTT_WARN_MS:
            flag("RTT", n, "WARN", "round-trip time %.0f ms" % h["rtt"])
        if h["loss"] > C.LOSS_WARN_PCT:
            flag("LOSS", n, "WARN", "telemetry loss %.0f%%" % h["loss"])

    # ---- FUSION: combine both views ----
    if sdn_ok:
        for h in hv:
            att = sdn["hosts"].get(h["mac"])
            port = port_by_key.get(att["key"]) if att else None
            if not port:
                continue
            if h["age"] > C.SILENT_AFTER_S and port["rx"] + port["tx"] > 0.5:
                flag("FUSED", h["name"], "CRIT",
                     "agent silent but switch port %s still carries %.1f Mbps" %
                     (port["key"], port["rx"] + port["tx"]))
            elif h["tx"] > C.HOST_RATE_WARN_MBPS and port["rx"] / C.LINK_CAPACITY_MBPS * 100 >= C.UTIL_WARN_PCT:
                flag("FUSED", h["name"], "CRIT",
                     "host reports %.1f Mbps and switch %s confirms %.1f Mbps in -> congestion source" %
                     (h["tx"], port["key"], port["rx"]))

    flows = []
    if sdn_ok:
        mac2name = {h["mac"]: h["name"] for h in hv}
        for fl in sdn["flows"]:
            flows.append((fl["mbps"], "s%d" % fl["dpid"], mac2name.get(fl["src"], fl["src"]),
                          mac2name.get(fl["dst"], fl["dst"])))
        flows.sort(reverse=True)
    return findings, hv, ports, flows, sdn_ok


def render(hv, ports, flows, sdn_ok):
    L = ["=" * 78,
         " NETWORK MONITOR  %s    SDN stats: %s" % (time.strftime("%H:%M:%S"), "live" if sdn_ok else "STALE"),
         "=" * 78, "",
         " HOSTS (application telemetry)",
         " %-6s %9s %9s %9s %8s  %s" % ("host", "tx Mbps", "rx Mbps", "rtt ms", "loss %", "status"),
         " " + "-" * 62]
    for h in hv:
        status = "DOWN" if h["age"] > C.SILENT_AFTER_S else "ok"
        rtt = "%9.1f" % h["rtt"] if h["rtt"] is not None else "%9s" % "-"
        L.append(" %-6s %9.2f %9.2f %s %8.0f  %s" % (h["name"], h["tx"], h["rx"], rtt, h["loss"], status))
    L += ["", " SWITCH PORTS (SDN statistics)",
          " %-8s %-6s %8s %8s %7s  %s" % ("port", "role", "rx Mbps", "tx Mbps", "util %", ""),
          " " + "-" * 62]
    for p in ports:
        L.append(" %-8s %-6s %8.2f %8.2f %7.0f  %s" % (p["key"], p["role"], p["rx"], p["tx"], p["util"],
                                                      "#" * int(min(p["util"], 100) / 5)))
    L += ["", " TOP FLOWS", " " + "-" * 62]
    for mbps, sw, src, dst in [f for f in flows if f[0] > 0.01][:5]:
        L.append(" %-3s %-6s -> %-6s %8.2f Mbps" % (sw, src, dst, mbps))
    L += ["", " ACTIVE ALERTS", " " + "-" * 62]
    if active:
        for (kind, subj), (sev, text) in sorted(active.items()):
            L.append(" [%-4s] %s/%s: %s" % (sev, kind, subj, text))
    else:
        L.append(" none - network looks healthy")
    L += ["", " RECENT EVENTS", " " + "-" * 62] + [" " + e for e in events]
    return "\n".join(L) + "\n"


def log_metrics(now, hv, ports):
    new = not os.path.exists(METRICS_CSV)
    with open(METRICS_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "kind", "name", "a", "b", "c"])   # host: tx,rtt,loss | port: rx,tx,util
        for h in hv:
            w.writerow(["%.2f" % now, "host", h["name"], "%.3f" % h["tx"],
                        "" if h["rtt"] is None else h["rtt"], "%.1f" % h["loss"]])
        for p in ports:
            w.writerow(["%.2f" % now, "port", p["key"], "%.3f" % p["rx"], "%.3f" % p["tx"], "%.1f" % p["util"]])


def main():
    ap = argparse.ArgumentParser(description="Centralised monitoring server")
    ap.add_argument("--port", type=int, default=C.SERVER_PORT, help="UDP listen port")
    C.SERVER_PORT = ap.parse_args().port
    threading.Thread(target=receiver, daemon=True).start()
    while True:
        time.sleep(C.POLL_INTERVAL)
        now = time.time()
        findings, hv, ports, flows, sdn_ok = analyse(now)
        update_alerts(findings)
        log_metrics(now, hv, ports)
        text = render(hv, ports, flows, sdn_ok)
        with open(DASH_FILE, "w") as f:
            f.write(text)
        if sys.stdout.isatty():
            print("\033[2J\033[H" + text, flush=True)


if __name__ == "__main__":
    main()
