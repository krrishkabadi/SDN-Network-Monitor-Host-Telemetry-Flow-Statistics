"""
config.py - settings shared by every component of the Network Monitoring and
Alert System (topology.py, host_agent.py, monitoring_server.py, SDN_Controller.py).
Keep this file in the same folder as the other scripts.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- files --------------------------------------------------------------
LOG_DIR = os.path.join(BASE_DIR, "logs")
STATS_FILE = os.path.join(LOG_DIR, "sdn_stats.json")        # written by controller, read by server
HISTORICAL_FILE = os.path.join(BASE_DIR, "historical_data.csv")

# the controller (normal user) and the server (root, inside Mininet) both write here
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.chmod(LOG_DIR, 0o777)
except OSError:
    pass

# ---- monitoring server / agents ----------------------------------------
MON_IP = "10.0.0.100"        # IP of the 'mon' host in topology.py
SERVER_PORT = 9999           # UDP port the monitoring server listens on
AGENT_INTERVAL_S = 2.0       # how often agents report

POLL_INTERVAL = 2            # server analysis loop (seconds)
SDN_POLL_INTERVAL = 2        # controller -> switch statistics polling (seconds)
SDN_STALE_AFTER_S = 10       # controller stats older than this are "STALE"
SILENT_AFTER_S = 8           # agent silent this long => host DOWN

# ---- thresholds (monitoring server) -------------------------------------
LINK_CAPACITY_MBPS = 10.0    # every link in topology.py is 10 Mbps
UTIL_WARN_PCT = 60
UTIL_CRIT_PCT = 85
HOST_RATE_WARN_MBPS = 5.0    # a host sending more than this is flagged
RTT_WARN_MS = 100
LOSS_WARN_PCT = 20

# ---- SDN controller -----------------------------------------------------
# Fallback thresholds when historical_data.csv is missing or too short.
DEFAULT_THRESHOLDS = {
    "traffic_mbps": 7.0,
    "active_flows": 20.0,
}
MAX_NEW_PORT_DROPS = 100     # new drops between two polls that raise an alert

# MAC -> name (must match topology.py)
HOSTS = {
    "00:00:00:00:00:01": "h1",
    "00:00:00:00:00:02": "h2",
    "00:00:00:00:00:03": "h3",
    "00:00:00:00:00:04": "h4",
    "00:00:00:00:01:00": "mon",
}

# Inter-switch (trunk) ports per switch DPID, from the link order in topology.py:
#   s1: 1=h1 2=h2 3=s2 4=s3(ring only)    s2: 1=h3 2=h4 3=s1 4=s3
#   s3: 1=mon 2=s2 3=s1(ring only)
TRUNK_PORTS = {1: [3, 4], 2: [3, 4], 3: [2, 3]}
