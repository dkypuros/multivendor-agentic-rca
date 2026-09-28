"""
O-RU simulator (P4): the fronthaul end of the split RAN, reduced to a liveness heartbeat.

Real open fronthaul is O-RAN WG4 CUS-plane (IQ sample streams over eCPRI) and M-plane (NETCONF);
per design stance 1 nothing below MAC is modeled. What is kept: the DU treats the RU as
load-bearing. No heartbeat within 2 seconds and the cell is not active, so RRC setup is refused.
That gives the RIC and SMO phases (P5) a real fault to inject: kill this process, the cell dies.

Run: python3 oru.py   (sends fh-heartbeat to the O-DU at 127.0.0.1:7014 every 0.3s)
"""

import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from domain.netconfig import host, udp_port

DU_FRONTHAUL = (host(), udp_port("fronthaul"))
RU_ID = "oru-1"

if __name__ == "__main__":
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[oru] heartbeating to {DU_FRONTHAUL} as {RU_ID}", flush=True)
    while True:
        sock.sendto(json.dumps({"type": "fh-heartbeat", "ruId": RU_ID}).encode(), DU_FRONTHAUL)
        time.sleep(0.3)
