# SDN Network Monitor: Host Telemetry + Flow Statistics

A distributed network-monitoring system built on Mininet, Open vSwitch and a Ryu SDN controller. Hosts send application-level telemetry over UDP sockets. The SDN controller collects network-level flow and port statistics. The system **combines both views** to detect abnormal conditions such as congestion, overloaded links, and silent hosts.

## Problem statement

> Develop a distributed network-monitoring system where hosts transmit telemetry through UDP sockets. The SDN controller should combine application telemetry with network-flow statistics to identify abnormal conditions.

## Basic idea

A network is normally seen from one side only. Here we use two independent views of the same traffic and cross-check them:

| View | Source | Examples |
|---|---|---|
| **Application telemetry** | `host_agent.py` on every host, sent via UDP | tx/rx rate, RTT, loss, CPU, memory |
| **Network statistics** | Ryu controller polling Open vSwitch over OpenFlow | per-port rx/tx rate, utilisation, per-flow rate |

If a host reports 9.7 Mbps and the switch port facing it confirms 9.7 Mbps, the two views agree and the host is identified as the source of congestion. If they disagree, or one side goes silent, an alert is raised.

## Architecture

```
  h1   h2   h3   h4          (each runs host_agent.py)
   \    |    |   /
   [s1]-[s2]-[s3]  <---- OpenFlow ----> SDN_Controller.py (Ryu)
              |                                |
             mon (10.0.0.100)                  | flow / port stats
     monitoring_server.py  <-------------------+
        |  receives UDP telemetry (port from config.py)
        v
   fuses both views -> alerts -> dashboard + CSV
```

## Files

| File | Role |
|---|---|
| `topology.py` | Mininet topology: 3 OVS switches, h1-h4, monitoring host `mon` (10.0.0.100), 10 Mbps links |
| `host_agent.py` | UDP telemetry agent on every host (CPU, memory, interface counters, RTT via acks) |
| `monitoring_server.py` | Receives telemetry, tracks rate / RTT / loss, fuses with SDN stats, alerts, dashboard, CSV |
| `SDN_Controller.py` | Ryu app: learning switch + flow/port statistics + anomaly detection + stats export |
| `config.py` | Shared addresses, ports and thresholds |
| `historical_data.csv` | **Sample (synthetic)** baseline used to learn per-host thresholds; replace with measured data |

## Requirements

- Linux (Ubuntu recommended) or a Linux VM
- Python 3
- [Mininet](http://mininet.org/) and Open vSwitch
- [Ryu](https://ryu-sdn.org/) SDN framework
- `iperf` (iperf2) for traffic generation

```bash
sudo apt install mininet openvswitch-switch iperf
pip install ryu
```

## How to run

Open **three terminals** in the project folder.

**Terminal 1: SDN controller**
```bash
ryu-manager SDN_Controller.py
```

**Terminal 2: network, agents and monitoring server**
```bash
sudo mn -c                      # clean any old Mininet state
sudo python3 topology.py        # builds the network and starts the host agents
```
Then, at the `mininet>` prompt, start the monitoring server on the monitoring host:
```
mon python3 monitoring_server.py &
```

**Terminal 3: live dashboard**
```bash
watch -n 1 cat logs/dashboard.txt
```

Wait a few seconds until the dashboard shows `SDN stats: live` and the hosts show `ok`.

## Dashboard

| Section | Source | Columns |
|---|---|---|
| **HOSTS** | UDP telemetry | tx Mbps, rx Mbps, rtt ms, loss %, status (`ok` / `DOWN`) |
| **SWITCH PORTS** | OpenFlow port stats | role (host / trunk), rx Mbps, tx Mbps, util % |
| **TOP FLOWS** | OpenFlow flow stats | switch, source host, destination, Mbps |
| **ACTIVE ALERTS** | Fused analysis | severity (WARN / CRIT), type, reason |
| **RECENT EVENTS** | Alert history | timestamp and message |

## Abnormal conditions detected

| Alert | Meaning |
|---|---|
| `UTIL` | Link utilisation above the WARN / CRIT threshold |
| `RATE` | A host sends above its allowed rate |
| `HOST` | A host has sent no telemetry (host or agent down) |
| `FUSED` | Host-reported rate confirmed by the switch port, so the host is the congestion source |

Thresholds are defined in `config.py`.

## Demo

At the `mininet>` prompt:

```
pingall
h3 iperf -s -u &
h1 iperf -c 10.0.0.3 -u -b 9M -t 30
h4 pkill -f "name h4"
```

What to watch on the dashboard: during the iperf run, h1 tx and h3 rx reach about 9.7 Mbps, the ports on the h1 to h3 path show about 97% utilisation, and a `FUSED/h1` alert appears. After the `pkill`, h4 is flagged `DOWN`.

### Full demo command list

| # | Command | What it does |
|---|---|---|
| 1 | `nodes` | Lists the controller, switches and hosts that Mininet created |
| 2 | `pingall` | Every host pings every other host, confirming full connectivity (expect 0% dropped) |
| 3 | `h3 iperf -s -u -i 1 &` | Starts a UDP iperf server on h3 (the receiver) that reports every second |
| 4 | `h1 iperf -c 10.0.0.3 -u -b 2M -t 20` | Sends 2 Mbit/s of UDP for 20 s: normal traffic, no alerts expected |
| 5 | `h1 iperf -c 10.0.0.3 -u -b 9M -t 30` | Sends 9 Mbit/s, near the 10 Mbps link limit: triggers utilisation and fused congestion alerts |
| 6 | `h1 iperf -c 10.0.0.3 -u -b 15M -t 30` | Exceeds the link capacity: heavy congestion and packet loss |
| 7 | `h2 pkill -f host_agent.py` | Kills h2's telemetry agent: h2 is flagged `DOWN` after a short timeout |
| 8 | restart the agent on h2 (same command `topology.py` uses) | Brings h2 back to `ok` and the alert clears |
| 9 | `sh tc qdisc add dev s1-eth3 root netem loss 10%` | Injects 10% packet loss on the s1 trunk port |
| 10 | `h1 iperf -c 10.0.0.3 -u -b 9M -t 30` | Repeats the 9M test with loss present: the receiver gets less than the sender reports |
| 11 | `sh tc qdisc del dev s1-eth3 root` | Removes the injected loss |
| 12 | `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | Prints the raw flow table and counters the controller reads |
| 13 | `sh ovs-ofctl -O OpenFlow13 dump-ports s1` | Prints raw per-port byte and packet counters, the source of the port statistics |

## Notes

- `historical_data.csv` is **synthetic sample data**. Replace it with measurements from your own runs for realistic per-host thresholds.
- Run `sudo mn -c` between runs to clear stale Mininet and Open vSwitch state.
- Remove injected loss (command 11) before running further tests.
