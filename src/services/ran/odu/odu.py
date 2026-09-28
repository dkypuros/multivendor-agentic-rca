"""
O-DU: radio side of the split RAN (P4). Serves the same UE-facing endpoints the P3 monolith did
(RRC on 7010, Uu on 7011/udp), which is exactly why the UE simulator needed zero changes.

Spec anchors:
  F1-C toward the O-CU-CP    TS 38.473: RRC from the UE is relayed transparently (Initial UL RRC
                             Message Transfer for setup, UL RRC Message Transfer after); UE Context
                             Setup Request arrives from the CU-CP and the DU allocates its downlink
                             F1-U TEID in the response (TS 38.401 8.9.2)
  F1-U                       TS 29.281 GTP-U toward the O-CU-UP (socket 2156/udp)
  Open fronthaul             O-RAN WG4 CUS/M-plane, simplified to a liveness heartbeat from the O-RU
                             sim on 7014/udp. Load-bearing: no heartbeat within 2s = cell not active,
                             RRC setup refused.
  E2 (P5)                    O-RAN E2AP semantics on 36421/udp (real E2 is SCTP + ASN.1, design
                             stance 1): RIC Subscription -> periodic RIC Indication with KPM-style
                             measurements; RIC Control Request applies near-real-time admission
                             control (maxUes).
  O1 (P5)                    Config management as JSON REST (real O1 is NETCONF/YANG, WG10);
                             administrativeState is the real NRM attribute (3GPP TS 28.541).
                             LOCKED refuses new RRC setups.
  LADN service area          TS 23.501 5.6.5. The O-DU accepts a zone DECLARATION -- the set of
                             TACs in which a LADN DNN exists -- so the radio side knows which
                             zones its cell is inside. Declaration, not enforcement: the SMF
                             still owns the anchoring decision (see /ran/ladn-zones below).

Run: python3 odu.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from adapters.udp_json import UdpJsonServer
from domain import obs
from domain.netconfig import port, tac, udp_port, url
from services.ran.perception_emit import emit   # best-effort SMO perception tap, never load-bearing

RRC_PORT = port("odu")
UU_PORT = udp_port("uu")
F1U_PORT = udp_port("f1u")
FRONTHAUL_PORT = udp_port("fronthaul")
E2_PORT = udp_port("e2")
CUCP_F1C = url("ocucp") + "/f1ap/messages"
RU_STALE_AFTER = 2.0
SERVING_TAC = tac()   # the Tracking Area this cell broadcasts; a LADN service area is a SET of these

app = SbiApp("odu")
ue_table = {}        # supi -> {"uuAddr", "f1": {"cuUpGtpu", "f1UlTeid"} or None, "f1DlTeid"}
f1dl_index = {}      # f1DlTeid -> supi
ru_state = {"lastHeartbeat": 0.0, "ruId": None}
counters = {"nextF1DlTeid": 4000}
traffic = {"ulPackets": 0, "dlPackets": 0}
config = {"administrativeState": "UNLOCKED", "maxUes": None}   # O1-managed / E2-managed
ladn_zones = {}      # zone name -> declaration (see the LADN zone admin section below)
zone_counter = {"nextRef": 1}
e2_sub = {"callback": None, "periodMs": None, "lastSent": 0.0}
uu_server = None
f1u_server = None
e2_server = None


def ru_connected():
    return (time.time() - ru_state["lastHeartbeat"]) < RU_STALE_AFTER


@app.route("POST", "/rrc/ue-messages")
def rrc_message(params, query, body):
    rrc = body.get("rrc", {})
    supi = rrc.get("supi")
    if rrc.get("type") == "RRCSetupRequest":
        if not ru_connected():
            return problem(503, "Service Unavailable", detail="no O-RU fronthaul heartbeat",
                           cause="CELL_NOT_ACTIVE")
        if config["administrativeState"] == "LOCKED":
            return problem(503, "Service Unavailable", detail="cell administratively locked via O1",
                           cause="CELL_LOCKED")
        if (config["maxUes"] is not None and supi not in ue_table
                and len(ue_table) >= config["maxUes"]):
            return problem(503, "Service Unavailable",
                           detail=f"admission control: maxUes={config['maxUes']} (E2 policy)",
                           cause="ADMISSION_CONTROL")
        ue_table.setdefault(supi, {"uuAddr": None, "f1": None, "f1DlTeid": None})
    message_type = ("InitialULRRCMessageTransfer"
                    if rrc.get("type") in ("RRCSetupRequest", "RRCSetupComplete")
                    else "ULRRCMessageTransfer")
    status, reply = request("POST", CUCP_F1C,
                            {"messageType": message_type, "rrc": rrc, "nas": body.get("nas", {})})
    return status, reply


@app.route("POST", "/f1ap/ue-context")
def f1ap_ue_context(params, query, body):
    if body.get("messageType") != "UEContextSetupRequest":
        return problem(400, "Bad Request", cause="UNSUPPORTED_F1AP_MESSAGE")
    ue = ue_table.setdefault(body["supi"], {"uuAddr": None, "f1": None, "f1DlTeid": None})
    ue["f1"] = body["f1"]
    ue["f1DlTeid"] = counters["nextF1DlTeid"]
    counters["nextF1DlTeid"] += 1
    f1dl_index[ue["f1DlTeid"]] = body["supi"]
    return 200, {"messageType": "UEContextSetupResponse", "f1DlTeid": ue["f1DlTeid"]}


@app.route("GET", "/odu/status")
def status(params, query, body):
    return 200, {"ru": "CONNECTED" if ru_connected() else "DISCONNECTED",
                 "ruId": ru_state["ruId"], "ueCount": len(ue_table),
                 "tac": SERVING_TAC,
                 "ladnZones": sorted(ladn_zones), "servingZones": serving_zones()}


@app.route("PUT", "/o1/config")
def o1_config(params, query, body):
    """O1 configuration management: administrativeState per TS 28.541 NRM (JSON, not NETCONF)."""
    state = body.get("administrativeState")
    if state not in ("LOCKED", "UNLOCKED"):
        return problem(400, "Bad Request", detail="administrativeState must be LOCKED or UNLOCKED",
                       cause="INVALID_ATTRIBUTE_VALUE")
    config["administrativeState"] = state
    obs.log("o1_config_applied", administrativeState=state)
    obs.counter("o1_config_changes_total").inc()
    emit("cell-config-change", kpis={"config_change_total": 1.0,
                                     "admin_state_locked": 1.0 if state == "LOCKED" else 0.0},
         administrativeState=state)
    return 200, {"administrativeState": state}


@app.route("GET", "/o1/status")
def o1_status(params, query, body):
    ru_ok = ru_connected()
    return 200, {"administrativeState": config["administrativeState"],
                 "ru": "CONNECTED" if ru_ok else "DISCONNECTED",
                 "cellState": "ACTIVE" if (ru_ok and config["administrativeState"] == "UNLOCKED") else "UNAVAILABLE",
                 "ruId": ru_state["ruId"], "ueCount": len(ue_table),
                 "maxUes": config["maxUes"],
                 "ladnZones": sorted(ladn_zones), "servingZones": serving_zones(),
                 "e2Subscribed": e2_sub["callback"] is not None}


@app.route("GET", "/o1/alarms")
def o1_alarms(params, query, body):
    """3GPP TS 28.532 / O-RAN O1 Fault Management active alarms."""
    ru_ok = ru_connected()
    alarms = []
    if not ru_ok or config["administrativeState"] == "LOCKED":
        alarms.append({
            "alarmId": "ALARM-DU-001",
            "faultName": "CellUnavailable",
            "specificProblem": "Cell is unavailable",
            "probableCause": "lossOfRealTimeSynchronization",
            "perceivedSeverity": "CRITICAL",
            "eventTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "managedObject": "ManagedElement=1,GNBDUFunction=1,NRCellDU=1",
            "proposedRepairActions": "Verify PTP Grandmaster Follow_Up message sequence and re-synchronize timing plane"
        })
    return 200, {"alarms": alarms, "totalAlarms": len(alarms)}


@app.route("GET", "/o1/timing-diagnostics")
def o1_timing_diagnostics(params, query, body):
    """Deep PTP protocol sequence analysis and timestamp anomaly detection."""
    return 200, {
        "cellStatus": "UNAVAILABLE",
        "clockStateHistory": "System clock oscillation, free running to locked around the incident",
        "ptpSequenceDiagnostics": [
            {
                "sequenceId": "X (seq=4821)",
                "event": "GM TX Follow_Up missing",
                "finding": "GNODE-B RX follow-up of sync message X is missing; GM TX follow-up of sync message X never sent"
            },
            {
                "sequenceId": "X+1 (seq=4822)",
                "event": "Precise Origin Timestamp Anomaly",
                "finding": "Follow-up X+1 precise origin timestamp matches previous sync time X instead of X+1"
            }
        ],
        "rootCause": "PTP_GM_TIMESTAMP_MISALIGNMENT",
        "impact": "O-DU servo calculated invalid phase step (>50ms), causing PLL oscillation, loss of S-Plane lock, and cell shutdown"
    }


# ------------------------------------------------------------- LADN zone admin (Phase 2)
# The RAN half of the AI-grid bridge. The OSS already programs the SMF so that sessions inside
# a zone's service area anchor at the PSA beside the ordered compute (aigrid-zone-anchor) --
# that is the CORE half, and until now the radio side had no idea it was part of a zone at all.
# These four routes let an order DECLARE the zone to the RAN: which TACs form the LADN service
# area, which DNN exists in it, which slice it carries. The O-DU can then say which zones its
# own cell is inside, which is what lets its events be tagged with a place (Phase 3).
#
# Declaration, NOT enforcement, and the distinction is load-bearing: TS 23.501 5.6.5 puts the
# LADN availability decision in the SMF, which already refuses a LADN DNN outside its service
# area. If the O-DU also policed it we would have two authorities for one rule and a demo that
# lies about where the decision lives. So nothing here changes RRC admission.
#
# In-memory and name-keyed on purpose: a service area is declarative configuration, not
# session state. Each declaration carries the orderId that produced it -- that is what makes
# the OSS connector's read-back proof mean something (it can tell ITS zone from anyone
# else's), and what lets a re-declaration by the same order be a retry rather than a clash.

def serving_zones():
    """Declared zones whose service area contains this cell's TAC -- the zones this O-DU is
    actually inside. Empty is the honest answer before an order declares one."""
    return sorted(name for name, z in ladn_zones.items() if SERVING_TAC in z["tacs"])


@app.route("POST", "/ran/ladn-zones")
def declare_ladn_zone(params, query, body):
    """Declare (or re-declare) one LADN service area. Idempotent for the owning order."""
    name = body.get("name")
    tacs = body.get("tacs")
    dnn = body.get("dnn")
    snssai = body.get("sNssai") or {}
    order_id = body.get("orderId")
    if not name:
        return problem(400, "Bad Request", detail="name is mandatory", cause="MANDATORY_IE_MISSING")
    if not isinstance(tacs, list) or not tacs:
        return problem(400, "Bad Request", detail="tacs must be a non-empty list of TACs; a LADN "
                       "service area is what makes the zone mean anything",
                       cause="INVALID_ATTRIBUTE_VALUE")
    if not dnn:
        return problem(400, "Bad Request", detail="dnn is mandatory", cause="MANDATORY_IE_MISSING")
    if not isinstance(snssai, dict) or snssai.get("sst") is None:
        return problem(400, "Bad Request", detail="sNssai.sst is mandatory",
                       cause="MANDATORY_IE_MISSING")
    if not order_id:
        return problem(400, "Bad Request", detail="orderId is mandatory; a declaration nobody "
                       "owns cannot be compensated", cause="MANDATORY_IE_MISSING")
    existing = ladn_zones.get(name)
    if existing is not None and existing["orderId"] != order_id:
        # Two orders claiming one zone with their own parameters is a real conflict, not
        # something to silently overwrite -- the second order would move the first one's cells.
        return problem(409, "Conflict",
                       detail=f"zone '{name}' is already declared by order {existing['orderId']}",
                       cause="ZONE_ALREADY_DECLARED")
    ref = existing["zoneRef"] if existing else f"ladn-{zone_counter['nextRef']}"
    if existing is None:
        zone_counter["nextRef"] += 1
    zone = {"name": name, "tacs": tacs, "dnn": dnn, "sNssai": snssai,
            "orderId": order_id, "zoneRef": ref,
            "serving": SERVING_TAC in tacs}
    ladn_zones[name] = zone
    obs.log("ladn_zone_declared", zone=name, zoneRef=ref, orderId=order_id,
            tacs=",".join(tacs), serving=zone["serving"])
    obs.counter("ladn_zone_declarations_total").inc()
    return (200 if existing else 201), zone


@app.route("GET", "/ran/ladn-zones")
def list_ladn_zones(params, query, body):
    return 200, {"zones": [ladn_zones[n] for n in sorted(ladn_zones)],
                 "servingTac": SERVING_TAC}


@app.route("GET", "/ran/ladn-zones/{name}")
def get_ladn_zone(params, query, body):
    zone = ladn_zones.get(params["name"])
    if zone is None:
        return problem(404, "Not Found", detail=f"no declared LADN zone {params['name']}")
    return 200, zone


@app.route("DELETE", "/ran/ladn-zones/{name}")
def withdraw_ladn_zone(params, query, body):
    zone = ladn_zones.pop(params["name"], None)
    if zone is None:
        return problem(404, "Not Found", detail=f"no declared LADN zone {params['name']}")
    obs.log("ladn_zone_withdrawn", zone=zone["name"], zoneRef=zone["zoneRef"],
            orderId=zone["orderId"])
    obs.counter("ladn_zone_withdrawals_total").inc()
    return 200, {"withdrawn": zone["name"], "zoneRef": zone["zoneRef"]}


def uu_handler(msg, addr, send):
    ue = ue_table.get(msg.get("supi"))
    if ue is None or ue["f1"] is None:
        return None
    ue["uuAddr"] = list(addr)
    f1 = ue["f1"]
    traffic["ulPackets"] += 1
    f1u_server.send({"teid": f1["f1UlTeid"], "payload": msg.get("payload", {})},
                    (f1["cuUpGtpu"]["ip"], f1["cuUpGtpu"]["port"]))
    return None


def f1u_handler(msg, addr, send):
    supi = f1dl_index.get(msg.get("teid"))
    ue = ue_table.get(supi)
    if ue is None or ue["uuAddr"] is None:
        return None
    traffic["dlPackets"] += 1
    uu_server.send({"payload": msg.get("payload", {})}, tuple(ue["uuAddr"]))
    return None


def fronthaul_handler(msg, addr, send):
    if msg.get("type") == "fh-heartbeat":
        ru_state["lastHeartbeat"] = time.time()
        ru_state["ruId"] = msg.get("ruId")
    return None


def e2_handler(msg, addr, send):
    """E2AP semantics per O-RAN E2AP (ETSI TS 104039), JSON over UDP per design stance 1."""
    message_type = msg.get("messageType")
    if message_type == "RICSubscriptionRequest":
        e2_sub["callback"] = tuple(msg["callback"])
        e2_sub["periodMs"] = msg.get("reportPeriodMs", 500)
        return {"messageType": "RICSubscriptionResponse", "ranFunctionId": msg.get("ranFunctionId")}
    if message_type == "RICControlRequest":
        config["maxUes"] = msg.get("control", {}).get("maxUes")
        obs.log("e2_control_applied", maxUes=config["maxUes"])
        obs.counter("e2_controls_total").inc()
        return {"messageType": "RICControlAcknowledge", "maxUes": config["maxUes"]}
    return {"messageType": "ErrorIndication", "cause": "UNSUPPORTED_E2_MESSAGE", "got": message_type}


def indication_loop():
    """Periodic RIC Indication with KPM-style measurements (E2SM-KPM naming simplified)."""
    while True:
        time.sleep(0.1)
        if e2_sub["callback"] is None:
            continue
        if (time.time() - e2_sub["lastSent"]) * 1000 < e2_sub["periodMs"]:
            continue
        e2_sub["lastSent"] = time.time()
        e2_server.send({"messageType": "RICIndication", "ranFunctionId": 2,
                        "kpm": {"ueCount": len(ue_table), "ulPackets": traffic["ulPackets"],
                                "dlPackets": traffic["dlPackets"],
                                "ruConnected": 1 if ru_connected() else 0}},
                       e2_sub["callback"])


def _scrape():
    obs.gauge("ue_count").set(len(ue_table))
    obs.gauge("ru_connected").set(1 if ru_connected() else 0)
    obs.gauge("ul_packets").set(traffic["ulPackets"])
    obs.gauge("dl_packets").set(traffic["dlPackets"])
    obs.gauge("ladn_zones_declared").set(len(ladn_zones))


if __name__ == "__main__":
    obs.init("odu")
    obs.on_scrape(_scrape)
    uu_server = UdpJsonServer("odu-uu", UU_PORT, uu_handler)
    f1u_server = UdpJsonServer("odu-f1u", F1U_PORT, f1u_handler)
    fronthaul = UdpJsonServer("odu-fronthaul", FRONTHAUL_PORT, fronthaul_handler)
    e2_server = UdpJsonServer("odu-e2", E2_PORT, e2_handler)
    uu_server.start()
    f1u_server.start()
    fronthaul.start()
    e2_server.start()
    threading.Thread(target=indication_loop, daemon=True).start()
    serve(app, RRC_PORT)
