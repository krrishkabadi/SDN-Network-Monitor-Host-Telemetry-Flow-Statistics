#!/usr/bin/env python3
r"""
topology.py - Mininet topology for the Network Monitoring and Alert System.

Default (loop-free) layout, 3 OpenFlow 1.3 switches, 4 monitored hosts and the
monitoring-server host 'mon':

    h1, h2 -- s1 ------- s2 ------- s3 -- mon (10.0.0.100)
                         |
                       h3, h4

All links are 10 Mbps. With --ring an extra s1-s3 link is added (loop) and OVS
STP is enabled automatically (wait ~35 s for it to converge).

The switches are Open vSwitch (data plane) controlled by a remote Ryu controller
(SDN_Controller.py) on 127.0.0.1:6653.

Usage (root needed, start the controller first):
    sudo python3 topology.py --start-server
    sudo python3 topology.py --start-server --ring
"""
import argparse
import os
import sys
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.topo import Topo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

LINK = dict(bw=10, delay="2ms")


class MonitoringTopo(Topo):
    """Port numbers follow link order; config.TRUNK_PORTS relies on it."""

    def build(self, ring=False):
        s1 = self.addSwitch("s1", cls=OVSKernelSwitch, protocols="OpenFlow13")
        s2 = self.addSwitch("s2", cls=OVSKernelSwitch, protocols="OpenFlow13")
        s3 = self.addSwitch("s3", cls=OVSKernelSwitch, protocols="OpenFlow13")

        h1 = self.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
        h2 = self.addHost("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
        h3 = self.addHost("h3", ip="10.0.0.3/24", mac="00:00:00:00:00:03")
        h4 = self.addHost("h4", ip="10.0.0.4/24", mac="00:00:00:00:00:04")
        mon = self.addHost("mon", ip=C.MON_IP + "/24", mac="00:00:00:00:01:00")

        # host links first ...
        self.addLink(h1, s1, **LINK)    # s1 port 1
        self.addLink(h2, s1, **LINK)    # s1 port 2
        self.addLink(h3, s2, **LINK)    # s2 port 1
        self.addLink(h4, s2, **LINK)    # s2 port 2
        self.addLink(mon, s3, **LINK)   # s3 port 1
        # ... then switch-to-switch links
        self.addLink(s1, s2, **LINK)    # s1 port 3, s2 port 3
        self.addLink(s2, s3, **LINK)    # s2 port 4, s3 port 2
        if ring:
            self.addLink(s1, s3, **LINK)  # s1 port 4, s3 port 3


def start_server(net):
    server = os.path.join(HERE, "monitoring_server.py")
    mon = net.get("mon")
    mon.cmd(f"cd {HERE} && python3 {server} > /tmp/monitoring_server.log 2>&1 &")
    info("*** Monitoring server started on 'mon' (stdout: /tmp/monitoring_server.log, "
         f"dashboard: {os.path.join(C.LOG_DIR, 'dashboard.txt')})\n")


def start_agents(net, interval):
    agent = os.path.join(HERE, "host_agent.py")
    for name in ("h1", "h2", "h3", "h4"):
        net.get(name).cmd(
            f"python3 {agent} --server {C.MON_IP} --port {C.SERVER_PORT} "
            f"--interval {interval} --name {name} > /tmp/agent_{name}.log 2>&1 &")
        info(f"*** Telemetry agent started on {name} (log: /tmp/agent_{name}.log)\n")


def run(args):
    net = Mininet(
        topo=MonitoringTopo(ring=args.ring),
        switch=OVSKernelSwitch,
        link=TCLink,
        controller=None,
        autoSetMacs=False,
        build=False,
    )
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    net.build()
    net.start()

    if args.ring:
        for sw in net.switches:
            sw.cmd(f"ovs-vsctl set bridge {sw.name} stp_enable=true")
        info("*** STP enabled (ring topology), waiting 35 s for convergence...\n")
        time.sleep(35)

    if hasattr(net, "waitConnected"):
        if not net.waitConnected(timeout=15):
            info("*** WARNING: switches did not connect to the controller - is ryu-manager running?\n")

    if args.start_server:
        start_server(net)
        time.sleep(1)
    if not args.no_agents:
        start_agents(net, args.interval)

    info("*** Network ready. Try:  pingall   |   h3 iperf -s -u &   |   "
         "h1 iperf -c 10.0.0.3 -u -b 9M -t 30\n")
    if not args.no_cli:
        CLI(net)

    info("*** Stopping network\n")
    for name in ("h1", "h2", "h3", "h4"):
        net.get(name).cmd("pkill -f host_agent.py")
    net.get("mon").cmd("pkill -f monitoring_server.py")
    net.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mininet topology for the monitoring project")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--interval", type=float, default=C.AGENT_INTERVAL_S, help="agent report interval (s)")
    ap.add_argument("--ring", action="store_true", help="add s1-s3 link (loop) and enable OVS STP")
    ap.add_argument("--no-agents", action="store_true", help="do not auto-start host agents")
    ap.add_argument("--start-server", action="store_true", help="auto-start monitoring_server.py on 'mon'")
    ap.add_argument("--no-cli", action="store_true", help="skip the Mininet CLI")
    setLogLevel("info")
    run(ap.parse_args())
