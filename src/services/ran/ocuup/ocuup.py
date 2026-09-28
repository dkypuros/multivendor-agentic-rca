"""
O-CU-UP: user-plane anchor of the split RAN (P4). PDCP is where it would live; here it is the
F1-U <-> N3 forwarding pivot with per-bearer state.

Spec anchors:
  E1 bearer context management   TS 38.463: BearerContextSetupRequest (CU-UP allocates its F1-U
                                 uplink TEID here, per TS 38.401 8.9.2), then
                                 BearerContextModificationRequest delivering the DU's downlink
                                 F1-U TEID and address
  F1-U / N3                      TS 29.281 GTP-U both sides; one socket (2155) serves both since
                                 TEIDs disambiguate direction (single-host lab simplification)

Run: python3 ocuup.py   (E1 + debug on 127.0.0.1:7013, GTP-U on 2155/udp)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, serve
from adapters.udp_json import UdpJsonServer
from domain.netconfig import port, udp_port

E1_PORT = port("ocuup")
GTPU_PORT = udp_port("gtpu_cuup")

app = SbiApp("ocuup")
bearers = {}         # supi -> bearer record
ul_index = {}        # f1UlTeid (from DU) -> bearer
dl_index = {}        # n3 dlTeid (from UPF) -> bearer
counters = {"nextF1UlTeid": 3000}
gtpu_server = None


@app.route("POST", "/e1ap/messages")
def e1ap_message(params, query, body):
    message_type = body.get("messageType")
    if message_type == "BearerContextSetupRequest":
        bearer = {"supi": body["supi"], "n3": body["n3"],
                  "f1UlTeid": counters["nextF1UlTeid"], "f1DlTeid": None, "duGtpu": None}
        counters["nextF1UlTeid"] += 1
        bearers[body["supi"]] = bearer
        ul_index[bearer["f1UlTeid"]] = bearer
        dl_index[body["n3"]["dlTeid"]] = bearer
        return 200, {"messageType": "BearerContextSetupResponse", "f1UlTeid": bearer["f1UlTeid"]}
    if message_type == "BearerContextModificationRequest":
        bearer = bearers.get(body["supi"])
        if bearer is None:
            return problem(404, "Not Found", cause="BEARER_CONTEXT_NOT_FOUND")
        bearer["f1DlTeid"] = body["f1DlTeid"]
        bearer["duGtpu"] = body["duGtpu"]
        return 200, {"messageType": "BearerContextModificationResponse"}
    return problem(400, "Bad Request", detail=f"unsupported E1 message {message_type}",
                   cause="UNSUPPORTED_E1_MESSAGE")


@app.route("GET", "/ocuup/bearers")
def list_bearers(params, query, body):
    return 200, {"bearers": list(bearers.values())}


def gtpu_handler(msg, addr, send):
    teid = msg.get("teid")
    bearer = ul_index.get(teid)
    if bearer is not None:   # uplink: F1-U in from the DU, N3 out to the UPF
        n3 = bearer["n3"]
        send({"teid": n3["ulTeid"], "payload": msg.get("payload", {})},
             (n3["upfGtpu"]["ip"], n3["upfGtpu"]["port"]))
        return None
    bearer = dl_index.get(teid)
    if bearer is not None and bearer["f1DlTeid"] is not None:   # downlink: N3 in, F1-U out
        send({"teid": bearer["f1DlTeid"], "payload": msg.get("payload", {})},
             (bearer["duGtpu"]["ip"], bearer["duGtpu"]["port"]))
    return None


if __name__ == "__main__":
    gtpu_server = UdpJsonServer("ocuup-gtpu", GTPU_PORT, gtpu_handler)
    gtpu_server.start()
    serve(app, E1_PORT)
