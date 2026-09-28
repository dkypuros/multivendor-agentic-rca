"""
UPF: user plane function. N4 (real PFCP) on UDP 8805, GTP-U on UDP 2152 (real port numbers).

The N4 and N3 wires are REAL BINARY: N4 session programming is genuine PFCP (adapters/pfcp.py,
TS 29.244) and the GTP-U user plane is genuine G-PDU framing (adapters/gtpu.py, TS 29.281), both
carried by adapters/udp_json.py's port-dispatched codec. The handler still sees the same message
dicts — the codec sits under the socket.

Spec anchors:
  N4 association / session establishment  TS 29.244 sections 6.2.6, 7.5.2 (real PFCP TLV on the wire)
  PDR / FAR packet processing model       TS 29.244 section 5.2: uplink PDR matches the GTP-U TEID,
                                          its FAR forwards to the data network; downlink PDR matches
                                          the UE IP, its FAR encapsulates toward the gNB N3 endpoint
  GTP-U                                   TS 29.281 section 5 (real header + TEID; the T-PDU carries
                                          the modeled inner packet as JSON bytes)

The N6 data network is modeled inside this process as an echo application: every echo-request that
exits uplink comes straight back as an echo-reply entering downlink. That keeps P2 self-contained;
a separate DN process arrives with the edge plane (P6 local breakout).

A small HTTP endpoint on 7004 exposes per-session counters. That is lab instrumentation, not an NF
interface; a real UPF reports usage over N4 (TS 29.244 section 5.2.2 URR), which lands in P7 rating.

Run: python3 upf.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, serve
from adapters.udp_json import UdpJsonServer
from domain import obs
from domain.netconfig import port, udp_port
from domain.snssai import key as snssai_key

N4_PORT = udp_port("n4_central")
GTPU_PORT = udp_port("gtpu_central")
INSPECT_PORT = port("upf")

sessions = {}          # seid -> session record with counters
uplink_teid_index = {}  # ulTeid -> session record
global_counters = {"droppedNoSession": 0}


def n4_handler(msg, addr, send):
    message_type = msg.get("messageType")
    if message_type == "AssociationSetupRequest":
        return {"messageType": "AssociationSetupResponse", "cause": "ACCEPTED",
                "nodeId": "upf.owned-stack.local"}
    if message_type == "SessionEstablishmentRequest":
        # sNssai: the SMF's slice tag on the N4 rules (network slicing wave 1; mirrors PFCP's
        # Rel-16 S-NSSAI IE). Absent (pre-slicing SMF) -> {"sst": 1}, the only slice that
        # ever existed before the tag, so legacy sessions stay honestly labeled.
        record = {"seid": msg["seid"], "sNssai": msg.get("sNssai") or {"sst": 1},
                  "pdrs": msg["pdrs"], "fars": msg["fars"],
                  "counters": {"ulPackets": 0, "ulBytes": 0, "dlPackets": 0, "dlBytes": 0}}
        sessions[msg["seid"]] = record
        obs.log("n4_session_established", seid=msg["seid"], snssai=snssai_key(record["sNssai"]))
        for pdr in msg["pdrs"]:
            if pdr["direction"] == "uplink":
                uplink_teid_index[pdr["match"]["teid"]] = record
        return {"messageType": "SessionEstablishmentResponse", "cause": "ACCEPTED",
                "seid": msg["seid"]}
    return {"messageType": "ErrorIndication", "cause": "UNSUPPORTED_MESSAGE", "got": message_type}


def gtpu_handler(msg, addr, send):
    session = uplink_teid_index.get(msg.get("teid"))
    if session is None:
        global_counters["droppedNoSession"] += 1
        return None
    payload = msg.get("payload", {})
    size = len(json.dumps(payload).encode())
    session["counters"]["ulPackets"] += 1
    session["counters"]["ulBytes"] += size

    # N6 echo data network: uplink echo-request turns around as a downlink echo-reply.
    if payload.get("type") == "echo-request":
        reply_payload = {"type": "echo-reply", "id": payload.get("id"), "data": payload.get("data")}
        downlink_far = next(f for f in session["fars"] if isinstance(f.get("destination"), dict))
        destination = downlink_far["destination"]
        session["counters"]["dlPackets"] += 1
        session["counters"]["dlBytes"] += len(json.dumps(reply_payload).encode())
        send({"teid": destination["teid"], "payload": reply_payload},
             (destination["gtpu"]["ip"], destination["gtpu"]["port"]))
    return None


inspect = SbiApp("upf-inspect")


@inspect.route("GET", "/upf/sessions")
def list_sessions(params, query, body):
    return 200, {"sessions": [{"seid": s["seid"], "sNssai": s["sNssai"],
                               "counters": s["counters"]} for s in sessions.values()],
                 "global": global_counters}


def _scrape():
    """Gauges mirror the /upf/sessions inspect data at scrape time (issue #27).
    The GTP-U user plane itself carries NO correlation id: corr rides the control plane only."""
    obs.gauge("sessions_active").set(len(sessions))
    totals = {"ulBytes": 0, "dlBytes": 0, "ulPackets": 0, "dlPackets": 0}
    by_slice = {}
    for s in sessions.values():
        for k in totals:
            totals[k] += s["counters"][k]
        agg = by_slice.setdefault(snssai_key(s["sNssai"]),
                                  {"sessions": 0, "ulBytes": 0, "dlBytes": 0})
        agg["sessions"] += 1
        agg["ulBytes"] += s["counters"]["ulBytes"]
        agg["dlBytes"] += s["counters"]["dlBytes"]
    obs.gauge("ul_bytes").set(totals["ulBytes"])
    obs.gauge("dl_bytes").set(totals["dlBytes"])
    obs.gauge("ul_packets").set(totals["ulPackets"])
    obs.gauge("dl_packets").set(totals["dlPackets"])
    obs.gauge("dropped_no_session").set(global_counters["droppedNoSession"])
    for slice_key, agg in by_slice.items():   # per-slice view (network slicing wave 1)
        obs.gauge("slice_sessions_active", slice=slice_key).set(agg["sessions"])
        obs.gauge("slice_ul_bytes", slice=slice_key).set(agg["ulBytes"])
        obs.gauge("slice_dl_bytes", slice=slice_key).set(agg["dlBytes"])


if __name__ == "__main__":
    obs.init("upf")
    obs.on_scrape(_scrape)
    UdpJsonServer("upf-n4", N4_PORT, n4_handler).start()
    UdpJsonServer("upf-gtpu", GTPU_PORT, gtpu_handler).start()
    serve(inspect, INSPECT_PORT)
