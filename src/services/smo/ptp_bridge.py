#!/usr/bin/env python3
"""
ptp_bridge.py — PTP timing-plane bridge: a software model of the ptp4l / cloud-event-proxy surface
that the Red Hat OpenShift PTP Operator exposes (no PTP hardware needed).

Endpoints:
  GET  /ptp                  PTP state & telemetry for SMO O1 timing sensor
  GET  /ptp/cloud-events     O-RAN / Cloud Event Proxy specification event stream
  GET  /ptp/logs             Raw ptp4l / phc2sys daemon log lines
  POST /ptp/inject           Inject a -50 ms master-offset step -> flip lock_state to FREERUN
  POST /ptp/heal             Heal / re-discipline clock -> flip lock_state to LOCKED
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PTP_BRIDGE_PORT", "7091"))
NODE = os.environ.get("PTP_NODE_NAME", "lab-node")   # label used in identities / CloudEvent sources

# Current PTP State (mutable via inject/heal)
PTP_STATE = {
    "offset_ns": 0,
    "lock_state": "LOCKED",
    "gm_port_state": "MASTER",
    "du_port_state": "SLAVE",
    "clock_class": 6,
    "interface": "ens7f0np0",
    "last_change": time.time(),
}

class PtpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip('/')
        if path in ('', '/ptp', '/ptp/status'):
            body = json.dumps({
                "port_state": PTP_STATE["lock_state"],
                "gm_port_state": PTP_STATE["gm_port_state"],
                "du_port_state": PTP_STATE["du_port_state"],
                "gm_identity": f"{NODE}-ens7f0np0",
                "phc_offset_ns": PTP_STATE["offset_ns"],
                "timestamping": "software",
                "interface": PTP_STATE["interface"],
                "clock_class": PTP_STATE["clock_class"],
                "cloud_event_state": PTP_STATE["lock_state"],
                "fidelity_note": "Software model of ptp4l / cloud-event-proxy state for fault-injection demos; no PTP hardware or PTP Operator involved."
            }).encode()
            self._send_json(200, body)
        elif path == '/ptp/cloud-events':
            body = json.dumps({
                "id": str(uuid.uuid4()),
                "source": f"/sync/ptp/status/{NODE}/{PTP_STATE['interface']}",
                "specversion": "1.0",
                "type": "event.ptp.sync.state-change",
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "datacontenttype": "application/json",
                "data": {
                    "version": "1.0",
                    "values": [
                        {
                            "resource": f"/sync/ptp/status/{NODE}/{PTP_STATE['interface']}",
                            "data_type": "notification",
                            "value_type": "enumeration",
                            "value": PTP_STATE["lock_state"]
                        },
                        {
                            "resource": f"/sync/ptp/master-offset/{NODE}/{PTP_STATE['interface']}",
                            "data_type": "metric",
                            "value_type": "decimal64",
                            "value": str(PTP_STATE["offset_ns"])
                        }
                    ]
                }
            }).encode()
            self._send_json(200, body)
        elif path == '/ptp/logs':
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if PTP_STATE["lock_state"] == "LOCKED":
                logs = (
                    f"[{ts}] ptp4l[187568]: [ens7f0np0] master offset         -14 s2 freq   +120 path delay      1250\n"
                    f"[{ts}] phc2sys[187569]: [ens7f0np0] CLOCK_REALTIME phc offset        18 s2 freq   -450 delay   1100\n"
                    f"[{ts}] cloud-event-proxy: state=LOCKED offset=-14ns interface=ens7f0np0\n"
                )
            else:
                logs = (
                    f"[{ts}] ptp4l[187568]: [ens7f0np0] master offset   -50000198 s0 freq  +10000 path delay      1250\n"
                    f"[{ts}] ptp4l[187568]: [ens7f0np0] port 1: SLAVE to UNCALIBRATED on FAULT (offset > 100000ns)\n"
                    f"[{ts}] phc2sys[187569]: [ens7f0np0] phc offset -50000198 s0 (out of holdover range)\n"
                    f"[{ts}] cloud-event-proxy: state=FREERUN offset=-50000198ns interface=ens7f0np0 SIGNAL=ptp_not_locked\n"
                )
            self._send_text(200, logs.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.rstrip('/')
        if path == '/ptp/inject':
            PTP_STATE["offset_ns"] = -50000198
            PTP_STATE["lock_state"] = "FREERUN"
            PTP_STATE["du_port_state"] = "UNCALIBRATED"
            PTP_STATE["last_change"] = time.time()
            body = json.dumps({
                "status": "injected",
                "injected_offset_ns": PTP_STATE["offset_ns"],
                "lock_state": PTP_STATE["lock_state"],
                "cloud_event_proxy": "event.ptp.sync.state-change -> FREERUN",
                "note": "PTP clock jammed by 50ms; lock lost, master offset spike active."
            }).encode()
            self._send_json(200, body)
        elif path == '/ptp/heal':
            PTP_STATE["offset_ns"] = 0
            PTP_STATE["lock_state"] = "LOCKED"
            PTP_STATE["du_port_state"] = "SLAVE"
            PTP_STATE["last_change"] = time.time()
            body = json.dumps({
                "status": "healed",
                "offset_ns": PTP_STATE["offset_ns"],
                "lock_state": PTP_STATE["lock_state"],
                "cloud_event_proxy": "event.ptp.sync.state-change -> LOCKED",
                "note": "PTP clock re-disciplined; lock restored, master offset 0ns."
            }).encode()
            self._send_json(200, body)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PtpHandler)
    print(f"[ptp_bridge] Serving OpenShift PTP status on 0.0.0.0:{PORT}/ptp", flush=True)
    server.serve_forever()
