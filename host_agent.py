#!/usr/bin/env python3
"""
host_agent.py - UDP telemetry agent (runs on every monitored Mininet host).

Every INTERVAL seconds it reads local statistics and sends ONE JSON datagram to
the monitoring server. The server acknowledges each datagram; the agent uses
the ack to measure the round-trip time (reported in the next datagram).

Datagram fields (contract with monitoring_server.py):
    type, host, mac, ip, seq, ts, interval, iface,
    tx_bytes, rx_bytes (cumulative interface counters), tx_bps, rx_bps,
    cpu_percent, mem_percent, rtt_ms (RTT of previous datagram, or null)

Usage (normally started by topology.py):
    python3 host_agent.py --server 10.0.0.100 --port 9999 --interval 2 --name h1
"""
import argparse
import fcntl
import json
import os
import socket
import struct
import time

SIOCGIFADDR = 0x8915
SIOCGIFHWADDR = 0x8927


def read_cpu_times():
    """(idle, total) jiffies from /proc/stat."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    return idle, sum(vals)


def read_mem_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            info[key] = int(val.split()[0])
    total = info.get("MemTotal", 1)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (total - avail) / total, 2)


def read_iface_counters():
    """{iface: (rx_bytes, tx_bytes)} from /proc/net/dev.
    /proc/net is network-namespace aware, so inside a Mininet host this lists
    only that host's own interfaces (e.g. h1-eth0)."""
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":")
            name = name.strip()
            if name == "lo":
                continue
            c = data.split()
            out[name] = (int(c[0]), int(c[8]))
    return out


def pick_iface(counters, wanted=None):
    if wanted:
        return wanted
    for name in counters:
        if "-eth" in name:          # Mininet: h1-eth0
            return name
    if "eth0" in counters:
        return "eth0"
    return next(iter(counters), None)


def _ioctl(iface, request):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return fcntl.ioctl(s.fileno(), request, struct.pack("256s", iface[:15].encode()))
    finally:
        s.close()


def get_mac(iface):
    # ioctl (not /sys/class/net) because /sys is NOT namespace aware in Mininet
    try:
        return ":".join("%02x" % b for b in _ioctl(iface, SIOCGIFHWADDR)[18:24])
    except OSError:
        return None


def get_ip(iface):
    try:
        return socket.inet_ntoa(_ioctl(iface, SIOCGIFADDR)[20:24])
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="UDP telemetry agent")
    ap.add_argument("--server", required=True, help="monitoring server IP")
    ap.add_argument("--port", type=int, default=9999, help="monitoring server UDP port")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between reports")
    ap.add_argument("--name", default=socket.gethostname(), help="host name to report (e.g. h1)")
    ap.add_argument("--iface", default=None, help="interface to monitor (default: auto)")
    ap.add_argument("--count", type=int, default=0, help="stop after N reports (0 = forever)")
    args = ap.parse_args()

    counters = read_iface_counters()
    iface = pick_iface(counters, args.iface)
    if iface is None or iface not in counters:
        raise SystemExit("no usable network interface found")
    mac, ip = get_mac(iface), get_ip(iface)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    addr = (args.server, args.port)

    seq = 0
    last_rtt = None
    prev_idle, prev_total = read_cpu_times()
    prev_rx, prev_tx = counters[iface]
    prev_t = time.time()
    next_t = prev_t

    print(f"[agent {args.name}] iface={iface} mac={mac} ip={ip} -> {addr} every {args.interval}s",
          flush=True)
    try:
        while True:
            next_t += args.interval
            time.sleep(max(0.0, next_t - time.time()))

            now = time.time()
            dt = max(now - prev_t, 1e-6)
            idle, total = read_cpu_times()
            d_total = total - prev_total
            cpu = round(100.0 * (1 - (idle - prev_idle) / d_total), 2) if d_total > 0 else 0.0
            rx, tx = read_iface_counters().get(iface, (prev_rx, prev_tx))

            seq += 1
            report = {
                "type": "telemetry",
                "host": args.name,
                "mac": mac,
                "ip": ip,
                "seq": seq,
                "ts": round(now, 3),
                "interval": args.interval,
                "iface": iface,
                "tx_bytes": tx,
                "rx_bytes": rx,
                "tx_bps": round(max(0, tx - prev_tx) * 8 / dt, 2),
                "rx_bps": round(max(0, rx - prev_rx) * 8 / dt, 2),
                "cpu_percent": cpu,
                "mem_percent": read_mem_percent(),
                "rtt_ms": last_rtt,
            }

            # send, then wait for the matching ack to measure RTT
            last_rtt = None
            t0 = time.time()
            try:
                sock.sendto(json.dumps(report).encode(), addr)
                while True:
                    data, _ = sock.recvfrom(4096)
                    try:
                        ack = json.loads(data.decode())
                    except ValueError:
                        continue
                    if ack.get("type") == "ack" and ack.get("seq") == seq:
                        last_rtt = round((time.time() - t0) * 1000, 2)
                        break
            except socket.timeout:
                pass                       # lost datagram or ack: server detects the seq gap
            except OSError as exc:
                print(f"[agent {args.name}] send failed: {exc}", flush=True)

            prev_idle, prev_total, prev_rx, prev_tx, prev_t = idle, total, rx, tx, now
            if args.count and seq >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
