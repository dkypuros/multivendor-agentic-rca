"""
O-CU-CP: RRC termination and NGAP-like N2 signaling for the split RAN (P4).

Spec anchors:
  Split architecture        3GPP TS 38.401 section 6.1.1 (CU-CP / CU-UP / DU), O-RAN WG1 OAD
  F1-C toward the O-DU      TS 38.473: Initial UL RRC Message Transfer / UL RRC Message Transfer in,
                            DL RRC Message Transfer out (collapsed into the synchronous HTTP reply)
  E1 toward the O-CU-UP     TS 38.463: Bearer Context Setup then Bearer Context Modification, the
                            real two-step in which CU-UP allocates the F1-U uplink TEID and the DU's
                            downlink TEID is pushed back to CU-UP afterward (TS 38.401 8.9.2 flow)
  N2 toward the AMF         TS 38.413, unchanged from P3; the RAN-side N3 endpoint offered to the
                            SMF is now the O-CU-UP, not a monolithic gNB

Run: python3 ocucp.py   (F1-C + debug on 127.0.0.1:7012)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.sbi_http import SbiApp, problem, request, serve
from domain.netconfig import host, port, udp_port, url
from services.ran.perception_emit import emit   # best-effort SMO perception tap, never load-bearing

PORT = port("ocucp")
AMF_N2 = url("amf") + "/n2/ue-messages"
CUUP_E1 = url("ocuup") + "/e1ap/messages"
CUUP_N3 = {"ip": host(), "port": udp_port("gtpu_cuup")}
DU_F1AP = url("odu") + "/f1ap/ue-context"
DU_F1U = {"ip": host(), "port": udp_port("f1u")}

app = SbiApp("ocucp")
ue_table = {}   # supi -> {"rrcState", "session": n2SmInfo or None}


def setup_bearer(supi, sm_info):
    """E1 setup -> F1 UE context setup -> E1 modification, per TS 38.401 8.9.2."""
    status, e1 = request("POST", CUUP_E1,
                         {"messageType": "BearerContextSetupRequest", "supi": supi,
                          "n3": sm_info})
    if status != 200:
        raise RuntimeError(f"E1 bearer setup failed: {e1}")
    status, f1 = request("POST", DU_F1AP,
                         {"messageType": "UEContextSetupRequest", "supi": supi,
                          "f1": {"cuUpGtpu": CUUP_N3, "f1UlTeid": e1["f1UlTeid"]}})
    if status != 200:
        raise RuntimeError(f"F1 UE context setup failed: {f1}")
    status, e1mod = request("POST", CUUP_E1,
                            {"messageType": "BearerContextModificationRequest", "supi": supi,
                             "f1DlTeid": f1["f1DlTeid"], "duGtpu": DU_F1U})
    if status != 200:
        raise RuntimeError(f"E1 bearer modification failed: {e1mod}")


@app.route("POST", "/f1ap/messages")
def f1ap_message(params, query, body):
    rrc = body.get("rrc", {})
    supi = rrc.get("supi")

    if rrc.get("type") == "RRCSetupRequest":
        ue_table[supi] = {"rrcState": "RRC_CONNECTED", "session": None}
        return 200, {"rrc": {"type": "RRCSetup", "supi": supi}}

    ue = ue_table.get(supi)
    if ue is None:
        return problem(400, "Bad Request", detail=f"no RRC connection for {supi}",
                       cause="RRC_CONNECTION_MISSING")

    procedure = ("InitialUEMessage" if rrc.get("type") == "RRCSetupComplete"
                 else "UplinkNASTransport")
    n2_body = {"procedure": procedure, "nas": body.get("nas", {})}
    if body.get("nas", {}).get("type") == "PduSessionEstablishmentRequest":
        n2_body["gnbGtpu"] = CUUP_N3   # RAN-side N3 endpoint is the O-CU-UP in the split
    status, n2_reply = request("POST", AMF_N2, n2_body)
    if status != 200:
        return status, n2_reply

    sm_info = n2_reply.get("n2SmInfo")
    if sm_info:
        setup_bearer(supi, sm_info)
        ue["session"] = sm_info
        emit("pdu-session-setup", kpis={"session_setup_total": 1.0,
                                        "ue_count": float(len(ue_table))}, supi=supi)
    return 200, {"rrc": {"type": "DLRRCMessageTransfer", "supi": supi},
                 "nas": n2_reply.get("nas", {})}


@app.route("GET", "/ocucp/ue-contexts/{supi}")
def get_ue(params, query, body):
    ue = ue_table.get(params["supi"])
    if ue is None:
        return problem(404, "Not Found", detail=f"no UE {params['supi']}")
    return 200, ue


if __name__ == "__main__":
    serve(app, PORT)
