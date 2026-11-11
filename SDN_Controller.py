#!/usr/bin/env python3
"""
SDN_Controller.py - Ryu controller for the Network Monitoring and Alert System.

* Forwards traffic like a learning switch (SimpleSwitch13).
* Every SDN_POLL_INTERVAL seconds asks each switch for flow and port statistics.
* Computes per-port and per-flow rates (Mbps) from the counter deltas.
* Detects anomalies against thresholds learned from historical_data.csv
  (median + 3 scaled MAD per host, with fallbacks from config.py):
      - flow traffic spike, too many flows for one host, port drop bursts.
* Exports everything to logs/sdn_stats.json, which monitoring_server.py reads
  and FUSES with the UDP telemetry coming from the host agents.

Run:  ryu-manager SDN_Controller.py
"""
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from statistics import median

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import config as C  # noqa: E402

from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls  # noqa: E402
from ryu.ofproto import ofproto_v1_3  # noqa: E402
from ryu.lib import hub  # noqa: E402
from ryu.app.simple_switch_13 import SimpleSwitch13  # noqa: E402


# ==================================================
# HISTORICAL CSV PROCESSING
# ==================================================

def load_historical_data(filename):
    """Load valid historical measurements (columns: host + metrics)."""
    rows = []
    try:
        with open(filename, "r", newline="") as file:
            for row in csv.DictReader(file):
                host = (row.get("host") or "").strip()
                if not host:
                    continue
                parsed = {"host": host}
                for field in C.DEFAULT_THRESHOLDS:
                    try:
                        value = float(row[field])
                        if not math.isfinite(value) or value < 0:
                            raise ValueError
                        parsed[field] = value
                    except (ValueError, TypeError, KeyError):
                        parsed[field] = None
                rows.append(parsed)
    except FileNotFoundError:
        print(f"[WARNING] Historical file not found: {filename} - using default thresholds")
    return rows


def calculate_threshold(values, fallback):
    """Robust upper threshold: median + 3 * 1.4826 * MAD (needs >= 5 samples)."""
    values = [float(v) for v in values
              if v is not None and math.isfinite(float(v)) and float(v) >= 0]
    if len(values) < 5:
        return fallback
    centre = median(values)
    mad = median([abs(v - centre) for v in values])
    if mad == 0:                      # avoid an over-sensitive threshold
        return max(fallback, centre * 1.5, centre + 1.0)
    return centre + 3 * 1.4826 * mad


def build_baselines(rows):
    """Global thresholds and per-host thresholds."""
    by_host = defaultdict(list)
    for row in rows:
        by_host[row["host"]].append(row)

    global_thresholds = {m: calculate_threshold([r[m] for r in rows], fb)
                         for m, fb in C.DEFAULT_THRESHOLDS.items()}
    host_thresholds = {
        host: {m: calculate_threshold([r[m] for r in host_rows], global_thresholds[m])
               for m in C.DEFAULT_THRESHOLDS}
        for host, host_rows in by_host.items()
    }
    return global_thresholds, host_thresholds


# ==================================================
# SDN CONTROLLER
# ==================================================

class SDNAnomalyController(SimpleSwitch13):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        historical = load_historical_data(C.HISTORICAL_FILE)
        self.global_thresholds, self.host_thresholds = build_baselines(historical)
        self.logger.info("Loaded %s historical measurements", len(historical))
        self.logger.info("Global thresholds: %s", self.global_thresholds)
        self.logger.info("Per-host thresholds: %s", self.host_thresholds)

        self.datapaths = {}
        self.previous_flows = {}     # (dpid, match, prio) -> (bytes, time)
        self.previous_ports = {}     # (dpid, port) -> ((tx, rx, txdrop, rxdrop), time)
        self.port_rates = {}         # "s1:1" -> {rx_mbps, tx_mbps, trunk}
        self.flow_rates = {}         # (dpid, match, prio) -> {dpid, src, dst, mbps}
        self.active_alerts = set()
        self.alert_log = os.path.join(C.LOG_DIR, "controller_alerts.log")

        self.monitor_thread = hub.spawn(self.monitor_switches)

    # ------------------------------------------------
    # THRESHOLDS AND ALERTS
    # ------------------------------------------------

    def threshold(self, host, metric):
        return self.host_thresholds.get(host, {}).get(metric, self.global_thresholds[metric])

    def alert(self, key, message, active=True):
        """Log a new alert once; log the recovery when it clears."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if active and key not in self.active_alerts:
            self.active_alerts.add(key)
            self.logger.warning("ANOMALY: %s", message)
            self._append_log(f"{stamp} [ANOMALY] {message}")
        elif not active and key in self.active_alerts:
            self.active_alerts.remove(key)
            self.logger.info("RECOVERED: %s", message)
            self._append_log(f"{stamp} [RECOVERED] {message}")

    def _append_log(self, line):
        try:
            with open(self.alert_log, "a") as f:
                f.write(line + "\n")
        except OSError as exc:
            self.logger.error("Could not write alert log: %s", exc)

    @staticmethod
    def is_trunk(dpid, port):
        return port in C.TRUNK_PORTS.get(dpid, ())

    # ------------------------------------------------
    # EXPORT FOR THE MONITORING SERVER
    # ------------------------------------------------

    def write_stats(self):
        hosts = {}
        for dpid, table in list(self.mac_to_port.items()):
            for mac, port in list(table.items()):
                if not self.is_trunk(dpid, port):          # attachment = host-facing port
                    hosts[mac] = {"key": "s%d:%d" % (dpid, port)}
        data = {
            "ts": time.time(),
            "ports": self.port_rates,
            "hosts": hosts,
            "flows": list(self.flow_rates.values()),
        }
        tmp = C.STATS_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, C.STATS_FILE)                  # atomic: server never sees half a file
        except OSError as exc:
            self.logger.error("Could not write %s: %s", C.STATS_FILE, exc)

    # ------------------------------------------------
    # SWITCH CONNECTION MANAGEMENT
    # ------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def switch_state_changed(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("Switch connected: %s", dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.info("Switch disconnected: %s", dp.id)

    def monitor_switches(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser, ofproto = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPFlowStatsRequest(dp))
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY))
            hub.sleep(C.SDN_POLL_INTERVAL)

    # ------------------------------------------------
    # FLOW STATISTICS: per-flow rate, flow counts
    # ------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_received(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        counts = defaultdict(int)
        seen = set()

        for stat in ev.msg.body:
            if stat.priority == 0:                         # table-miss entry
                continue
            src = stat.match.get("eth_src")                # learning-switch flows match on L2
            dst = stat.match.get("eth_dst")
            if not src or not dst:
                continue

            key = (dpid, str(stat.match), stat.priority)
            seen.add(key)
            src_name = C.HOSTS.get(src, src)
            dst_name = C.HOSTS.get(dst, dst)
            counts[src_name] += 1

            previous = self.previous_flows.get(key)
            self.previous_flows[key] = (stat.byte_count, now)
            if previous is None:                           # first sight = baseline
                continue

            old_bytes, old_time = previous
            elapsed = now - old_time
            delta = stat.byte_count - old_bytes
            if elapsed <= 0 or delta < 0:                  # counter reset
                continue

            rate = delta * 8 / elapsed / 1e6
            self.flow_rates[key] = {"dpid": dpid, "src": src, "dst": dst, "mbps": round(rate, 3)}

            limit = self.threshold(src_name, "traffic_mbps")
            self.alert(("flow_traffic_spike", dpid, src_name, dst_name),
                       f"Switch {dpid}: {src_name} -> {dst_name}, "
                       f"{rate:.2f} Mbps (threshold {limit:.2f})",
                       rate > limit)

        for key in list(self.previous_flows):              # forget flows that disappeared
            if key[0] == dpid and key not in seen:
                del self.previous_flows[key]
                self.flow_rates.pop(key, None)

        for host, count in counts.items():
            limit = self.threshold(host, "active_flows")
            self.alert(("switch_flow_count", dpid, host),
                       f"Switch {dpid}: {host} has {count} flows (threshold {limit:.0f})",
                       count > limit)

        self.write_stats()

    # ------------------------------------------------
    # PORT STATISTICS: utilisation + drops
    # ------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_received(self, ev):
        dp = ev.msg.datapath
        now = time.time()

        for stat in ev.msg.body:
            port = stat.port_no
            if port >= dp.ofproto.OFPP_MAX:                # skip LOCAL etc.
                continue

            key = (dp.id, port)
            current = (stat.tx_bytes, stat.rx_bytes, stat.tx_dropped, stat.rx_dropped)
            previous = self.previous_ports.get(key)
            self.previous_ports[key] = (current, now)
            if previous is None:
                continue

            old, old_time = previous
            elapsed = now - old_time
            if elapsed <= 0 or any(c < o for c, o in zip(current, old)):
                continue                                   # reset counters

            tx_rate = (current[0] - old[0]) * 8 / elapsed / 1e6
            rx_rate = (current[1] - old[1]) * 8 / elapsed / 1e6
            drops = (current[2] - old[2]) + (current[3] - old[3])

            self.port_rates["s%d:%d" % (dp.id, port)] = {
                "rx_mbps": round(rx_rate, 3),
                "tx_mbps": round(tx_rate, 3),
                "trunk": self.is_trunk(dp.id, port),
            }

            if tx_rate > 0.01 or rx_rate > 0.01 or drops:
                self.logger.info("Switch %s port %s: TX %.2f Mbps, RX %.2f Mbps, new drops %s",
                                 dp.id, port, tx_rate, rx_rate, drops)

            self.alert(("port_drops", dp.id, port),
                       f"Switch {dp.id} port {port}: {drops} new packet drops",
                       drops > C.MAX_NEW_PORT_DROPS)

        self.write_stats()
